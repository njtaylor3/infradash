"""Turns successive raw polls into a Snapshot with history and de-duplicated logs."""
from __future__ import annotations

import os
import time
from collections import deque

from .correlate import Snapshot, build_snapshot
from .health import find_issues
from .models import Action, Event, MetricPanel

HISTORY = 400   # samples; wide braille graphs show two per column


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
        self.hist: dict[str, dict[str, deque]] = {
            k: {} for k in ("cpu", "mem", "net", "gw", "st", "rx", "tx", "agg", "gmem")}
        self.issue_since: dict[str, float] = {}
        self.action_events: list[Event] = []
        self.action_log = getattr(provider, "action_log", None)
        self.guest_cpu: dict[str, deque] = {}
        self.fw = _Dedup(4000)
        self.pve = _Dedup(1000)

    def _push(self, panel: str, row: str, value: float | None) -> None:
        if value is None:
            return
        self.hist[panel].setdefault(row, deque(maxlen=HISTORY)).append(value)

    async def poll(self) -> Snapshot:
        raw = await self.provider.poll()
        if raw.fw:
            self._push("cpu", raw.fw.name, raw.fw.cpu)
            self._push("mem", raw.fw.name, raw.fw.mem)
        for n in raw.nodes:
            self._push("cpu", n.name, n.cpu if n.online else 0.0)
            self._push("mem", n.name, n.mem if n.online else 0.0)
        for i in raw.ifaces:
            self._push("net", i.name, (i.rx_bps + i.tx_bps) / 1e6)
            self._push("rx", i.key, i.rx_bps)
            self._push("tx", i.key, i.tx_bps)
        online = [n for n in raw.nodes if n.online]
        if online:
            self._push("agg", "cpu", sum(n.cpu for n in online) / len(online))
            self._push("agg", "mem", sum(n.mem for n in online) / len(online))
        for g in raw.gateways:
            self._push("gw", g.name, g.rtt_ms if g.rtt_ms is not None else 0.0)
        for st in sorted(raw.storages, key=lambda x: x.label):
            self._push("st", st.label, st.pct)
        for g in raw.guests:
            self.guest_cpu.setdefault(g.id, deque(maxlen=HISTORY)).append(
                g.cpu if g.status == "running" else 0.0)
            self._push("gmem", g.id, g.mem if g.status == "running" else 0.0)
        self.fw.extend(raw.fw_log)
        self.pve.extend(raw.pve_log)

        def series(k):
            return {row: list(vals)[-90:] for row, vals in self.hist[k].items()}
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
                              {k: list(v)[-90:] for k, v in self.guest_cpu.items()}, self.action_events)
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
        await self.provider.close()

    def series(self, panel: str, row: str) -> list[float]:
        """Full-length history for one row, for the btop-style graphs."""
        if panel == "gcpu":
            return list(self.guest_cpu.get(row, []))
        return list(self.hist.get(panel, {}).get(row, []))
