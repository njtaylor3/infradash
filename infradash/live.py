"""Live provider: polls OPNsense and Proxmox concurrently into one Raw snapshot."""
from __future__ import annotations

import asyncio

from .models import Raw
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

    async def poll(self) -> Raw:
        raw = Raw()
        await asyncio.gather(*(c.poll(raw) for c in (self.opn, self.pve) if c))
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
