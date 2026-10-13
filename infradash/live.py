"""Live provider: polls OPNsense and Proxmox concurrently into one Raw snapshot.

The two systems are polled independently: one being slow or down never costs the dashboard
the other one's data."""
from __future__ import annotations

import asyncio

from .models import Raw, SourceStatus
from .opnsense import OPNsenseClient
from .proxmox import ProxmoxClient


class LiveProvider:
    def __init__(self, cfg: dict) -> None:
        self.opn = OPNsenseClient(cfg["opnsense"]) if cfg.get("opnsense") else None
        self.pve = ProxmoxClient(cfg["proxmox"]) if cfg.get("proxmox") else None
        if not self.opn and not self.pve:
            raise ValueError("configure at least one of 'opnsense:' or 'proxmox:'")
        parts = [p for p in (self.opn and "OPNsense", self.pve and "Proxmox") if p]
        self.name = " + ".join(parts)
        act = cfg.get("actions") or {}
        self.actions_enabled = bool(act.get("enabled", False))
        self.block_alias = act.get("block_alias") if self.opn else None
        self.action_log = act.get("log_file", "~/.local/state/infradash/actions.log")
        self.has_pve, self.has_opn = self.pve is not None, self.opn is not None
        # A backstop: each client already bounds its slow calls, so this only trips if an
        # API accepts connections and then stops answering.
        self.poll_timeout = float(cfg.get("poll_timeout_s", 20))

    async def poll(self) -> Raw:
        raw = Raw()
        clients = [c for c in (self.opn, self.pve) if c]
        results = await asyncio.gather(*(asyncio.wait_for(c.poll(), self.poll_timeout) for c in clients),
                                       return_exceptions=True)
        for c, res in zip(clients, results):
            if isinstance(res, Raw):
                raw.merge(res)
                continue
            if isinstance(res, asyncio.CancelledError):
                raise res
            why = f"no answer within {self.poll_timeout:g}s" if isinstance(res, TimeoutError) else (
                f"poll failed: {type(res).__name__}: {res}")
            st = SourceStatus(**vars(c.api.status))
            st.ok, st.error, st.stale = False, why, False
            raw.sources[st.name] = st
            raw.errors.append(f"{st.name}: {why}")
        return raw

    async def run_action(self, a) -> str:
        if not self.actions_enabled:
            raise RuntimeError("actions are disabled in the config")
        p = a.params
        if a.kind == "pve:migrate" and self.pve:
            upid = await self.pve.migrate(p["node"], p["kind"], p["vmid"], p["target"], p["running"])
            return f"task started ({upid})"
        if a.kind.startswith("pve:") and self.pve:
            upid = await self.pve.guest_power(p["node"], p["kind"], p["vmid"], a.kind[4:])
            return f"task started ({upid})"
        if a.kind == "fw:block" and self.opn and self.block_alias:
            return await self.opn.block_ip(self.block_alias, p["ip"])
        if a.kind == "fw:kill" and self.opn:
            return await self.opn.kill_states(p["ip"])
        raise RuntimeError(f"unsupported action {a.kind}")

    async def task_log(self, node: str, upid: str) -> list[str]:
        return await self.pve.task_log(node, upid) if self.pve else []

    async def close(self) -> None:
        for c in (self.opn, self.pve):
            if c:
                await c.close()
