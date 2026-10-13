"""Turns successive raw polls into a Snapshot with history and de-duplicated logs."""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque

from .correlate import Snapshot, build_snapshot
from .health import find_issues
from .models import Action, Event, MetricPanel

HISTORY = 90
UNCLAIMED_MAX_AGE_S = 30     # a finished poll nobody collected is still worth showing until then


class _Dedup:
    """Bounded insertion-ordered set of items keyed by .key."""

    def __init__(self, maxlen: int) -> None:
        self.items: deque = deque()
        self.keys: set[str] = set()
        self.maxlen = maxlen

    def extend(self, items) -> None:
        for it in sorted(items, key=lambda x: x.ts):
            if it.key in self.keys:
                continue
            self.items.append(it)
            self.keys.add(it.key)
            if len(self.items) > self.maxlen:
                self.keys.discard(self.items.popleft().key)


class Collector:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.name = provider.name
        self.hist: dict[str, dict[str, deque]] = {k: {} for k in ("cpu", "mem", "net", "gw", "st")}
        self.issue_since: dict[str, float] = {}
        self.action_events: list[Event] = []
        self.action_log = getattr(provider, "action_log", None)
        self.guest_cpu: dict[str, deque] = {}
        self.fw = _Dedup(4000)
        self.pve = _Dedup(1000)
        self.fw_guest: str | None = None        # the firewall's own VM, once it has been seen
        self._errors_before: set[str] = set()
        self._inflight: asyncio.Task | None = None
        self._inflight_done_at = 0.0

    def _push(self, panel: str, row: str, value: float | None) -> None:
        if value is None:
            return
        self.hist[panel].setdefault(row, deque(maxlen=HISTORY)).append(value)

    async def poll(self) -> Snapshot:
        """Poll once. Polls never overlap and are never abandoned halfway: if the caller is
        cancelled (the UI starts a new refresh while one is still running), the poll carries
        on, and the next call collects its result instead of starting another. Cancelling a
        poll midway would drop its HTTP connections and lose its counter samples."""
        task = self._inflight
        if task is not None and task.done() and \
                time.monotonic() - self._inflight_done_at > UNCLAIMED_MAX_AGE_S:
            if not task.cancelled():
                task.exception()        # retrieve it, so asyncio doesn't warn that nobody did
            task = None
        if task is None:
            task = self._inflight = asyncio.create_task(self._poll())
            task.add_done_callback(self._poll_done)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight is task:
                self._inflight = None

    def _poll_done(self, task: asyncio.Task) -> None:
        self._inflight_done_at = time.monotonic()

    async def _poll(self) -> Snapshot:
        raw = await self.provider.poll()
        errors = set(raw.errors)
        raw.new_errors = [e for e in raw.errors if e not in self._errors_before]
        self._errors_before = errors
        if raw.fw:
            self._push("cpu", raw.fw.name, raw.fw.cpu)
            self._push("mem", raw.fw.name, raw.fw.mem)
        for n in raw.nodes:
            self._push("cpu", n.name, n.cpu if n.online else 0.0)
            self._push("mem", n.name, n.mem if n.online else 0.0)
        for i in raw.ifaces:
            self._push("net", i.name, (i.rx_bps + i.tx_bps) / 1e6)
        for g in raw.gateways:
            self._push("gw", g.name, g.rtt_ms if g.rtt_ms is not None else 0.0)
        for st in sorted(raw.storages, key=lambda x: x.label):
            self._push("st", st.label, st.pct)
        for g in raw.guests:
            self.guest_cpu.setdefault(g.id, deque(maxlen=40)).append(
                g.cpu if g.status == "running" else 0.0)
        self.fw.extend(raw.fw_log)
        self.pve.extend(raw.pve_log)

        def series(k):
            return {row: list(vals) for row, vals in self.hist[k].items()}
        metrics = [
            MetricPanel("CPU", "%", series("cpu"), warn=75, crit=90),
            MetricPanel("Memory", "%", series("mem"), warn=80, crit=92),
            MetricPanel("Interface traffic", "Mb/s", series("net")),
            MetricPanel("Gateway latency", "ms", series("gw"), warn=60, crit=150),
            MetricPanel("Storage used", "%", dict(sorted(series("st").items(),
                                                         key=lambda kv: -(kv[1][-1] if kv[1] else 0))),
                        warn=80, crit=90),
        ]
        snap = build_snapshot(raw, list(self.fw.items), list(self.pve.items), metrics,
                              {k: list(v) for k, v in self.guest_cpu.items()}, self.action_events,
                              fw_guest_hint=self.fw_guest)
        self.fw_guest = snap.fw_guest
        now = time.time()
        issues = find_issues(snap, {k: list(v) for k, v in self.hist["cpu"].items()})
        live = {i.key for i in issues}
        for i in issues:
            i.since = self.issue_since.setdefault(i.key, now)
        for k in list(self.issue_since):
            if k not in live:
                del self.issue_since[k]
        snap.issues = issues
        return snap

    # --- actions ------------------------------------------------------------------
    @property
    def actions_enabled(self) -> bool:
        return bool(getattr(self.provider, "actions_enabled", False))

    async def run_action(self, action: Action) -> str:
        """Execute an already-confirmed action, record it as an event and in the audit log."""
        try:
            result = await self.provider.run_action(action)
            level, msg = "INFO", f"{action.label}: {result}"
        except Exception as e:  # noqa: BLE001
            level, msg = "ERROR", f"{action.label} FAILED: {e}"
        ents = [action.entity] if action.entity else []
        self.action_events.append(Event(time.time(), "you", level, msg, ents, None, action))
        self.action_events = self.action_events[-200:]
        if self.action_log:
            try:
                path = os.path.expanduser(self.action_log)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {level} {action.kind} "
                             f"{action.params} {msg}\n")
            except OSError:
                pass
        if level == "ERROR":
            raise RuntimeError(msg)
        return msg

    async def task_log(self, node: str, upid: str) -> list[str]:
        return await self.provider.task_log(node, upid)

    async def close(self) -> None:
        if self._inflight and not self._inflight.done():
            self._inflight.cancel()
            await asyncio.gather(self._inflight, return_exceptions=True)
        await self.provider.close()
