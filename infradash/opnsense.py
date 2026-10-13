"""OPNsense REST API client.

Uses the snake_case endpoints introduced in OPNsense 25.7 and falls back to the older
camelCase names on 404, so it works on both. The firewall log is fetched incrementally (only
lines newer than the last one seen), which keeps responses small; see api.py for how a
malformed (chunked) response is recovered.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from datetime import datetime

import httpx

from .api import ApiClient, PollRun
from .cache import LastGood, Rates, Refresher
from .models import ArpEntry, FwEntry, FwStats, Gateway, Iface, Raw

VLAN_DEV_RE = re.compile(r"(?:\.|^vlan0*)(\d{1,4})$")
NUM_RE = re.compile(r"[-+]?\d*\.?\d+")


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = NUM_RE.search(str(v))
    return float(m.group()) if m else None


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_interfaces(data) -> list[Iface]:
    rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
    out = []
    for r in rows:
        key = r.get("identifier")
        if not key:            # devices that aren't assigned to an interface
            continue
        device = r.get("device") or r.get("if") or ""
        cidr = r.get("addr4")
        if not cidr and isinstance(r.get("ipv4"), list) and r["ipv4"]:
            first = r["ipv4"][0]
            cidr = first.get("ipaddr") if isinstance(first, dict) else str(first)
            if cidr and "/" not in cidr and first.get("subnetbits"):
                cidr = f"{cidr}/{first['subnetbits']}"
        vlan = _num(r.get("vlan_tag"))
        if vlan is None:
            m = VLAN_DEV_RE.search(device)
            vlan = float(m.group(1)) if m else None
        status = str(r.get("status", "up")).lower()
        out.append(Iface(
            key=key, name=r.get("description") or key.upper(), device=device,
            cidr=cidr or None, vlan=int(vlan) if vlan is not None else None,
            up=status not in ("down", "no carrier"),
            mac=(r.get("macaddr") or r.get("macaddr_hw") or "").upper(),
        ))
    return out


def parse_arp(data) -> list[ArpEntry]:
    rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
    return [ArpEntry(ip=r.get("ip", ""), mac=(r.get("mac") or "").upper(),
                     device=r.get("intf") or r.get("interface") or "",
                     hostname=r.get("hostname") or "")
            for r in rows if r.get("ip") and r.get("mac") and r.get("expired") is not True]


def parse_traffic(data) -> dict[str, tuple[float, float]]:
    """Interface key -> (bytes received, bytes transmitted) counters."""
    counters = {}
    for key, row in ((data or {}).get("interfaces") or {}).items():
        rx = _num(_first(row, "bytes received", "bytes_received", "inbytes", "rx_bytes"))
        tx = _num(_first(row, "bytes transmitted", "bytes_transmitted", "outbytes", "tx_bytes"))
        if rx is not None and tx is not None:
            counters[key] = (rx, tx)
    return counters


def newest_digest(data) -> str | None:
    """The __digest__ of the newest log row, to ask only for rows after it next time."""
    rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
    newest = None
    for r in rows:
        if r.get("__digest__") and (newest is None or
                                    str(r.get("__timestamp__", "")) > str(newest.get("__timestamp__", ""))):
            newest = r
    return newest["__digest__"] if newest else None


def parse_fw_log(data) -> list[FwEntry]:
    rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
    out = []
    for r in rows:
        ts_raw = r.get("__timestamp__") or ""
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = time.time()
        key = r.get("__digest__") or hashlib.sha1(repr(sorted(r.items())).encode()).hexdigest()
        out.append(FwEntry(
            ts=ts, action=(r.get("action") or "").lower(), device=r.get("interface", ""),
            direction=r.get("dir", ""), proto=(r.get("protoname") or r.get("proto") or "").lower(),
            src=r.get("src", ""), sport=str(r.get("srcport") or ""),
            dst=r.get("dst", ""), dport=str(r.get("dstport") or ""),
            label=r.get("label", ""), key=key,
        ))
    return out


def parse_gateways(data) -> list[Gateway]:
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    out = []
    for g in items:
        status = str(g.get("status") or "unknown").lower()
        status = {"none": "online", "force_down": "down"}.get(status, status)
        out.append(Gateway(name=g.get("name", "?"), status=status, rtt_ms=_num(g.get("delay")),
                           loss_pct=_num(g.get("loss")), address=g.get("address") or g.get("monitor")))
    return out


class OPNsenseClient:
    def __init__(self, cfg: dict, transport: httpx.AsyncBaseTransport | None = None) -> None:
        urls = cfg["url"] if isinstance(cfg["url"], list) else [cfg["url"]]
        urls = [u.rstrip("/") if u.rstrip("/").endswith("/api") else u.rstrip("/") + "/api" for u in urls]
        key = os.environ.get("OPNSENSE_KEY") or cfg.get("key")
        secret = os.environ.get("OPNSENSE_SECRET") or cfg.get("secret")
        if not key or not secret:
            raise ValueError("opnsense: set key/secret in the config or OPNSENSE_KEY/OPNSENSE_SECRET")
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.api = ApiClient(
            "opnsense", urls, headers={"Authorization": f"Basic {basic}"},
            verify=cfg.get("verify_tls", True), timeout=float(cfg.get("timeout", 10)),
            http2=bool(cfg.get("http2", False)), transport=transport,
            denied_hint=" (check the API user's privileges)")
        self.name = cfg.get("name", "opnsense")
        self.log_limit = int(cfg.get("log_limit", 500))
        self.budget = float(cfg.get("slow_call_budget_s", 3))
        self._slow = Refresher()
        self._last = LastGood(float(cfg.get("stale_max_s", 60)))
        self._rates = Rates(scale=8)          # bytes -> bits
        self._log_digest: str | None = None

    async def _get(self, *paths: str, params: dict | None = None):
        return await self.api.get_json(*paths, params=params)

    async def interfaces(self) -> list[Iface]:
        async def fetch():
            return parse_interfaces(await self._get("/interfaces/overview/interfaces_info",
                                                    "/interfaces/overview/interfacesInfo"))
        ifaces = await self._slow.get("ifaces", 60, fetch, self.budget)
        if ifaces is None and (err := self._slow.error("ifaces")):
            raise err
        return [Iface(**vars(i)) for i in ifaces or []]     # copies: traffic rates are set per poll

    async def _traffic(self) -> tuple[float, dict[str, tuple[float, float]]]:
        data = await self._get("/diagnostics/traffic/interface")
        return time.time(), parse_traffic(data)

    async def _fw_log(self):
        params = {"limit": self.log_limit}
        if self._log_digest:
            params["digest"] = self._log_digest
        return await self._get("/diagnostics/firewall/log", params=params)

    async def stats(self, run: PollRun | None = None) -> FwStats:
        st = FwStats(name=self.name)

        async def cpu():
            d = await self._get("/diagnostics/activity/get_activity",
                                "/diagnostics/activity/getActivity")
            for line in d.get("headers", []):
                m = re.search(r"([\d.]+)% idle", line)
                if m:
                    st.cpu = 100 - float(m.group(1))

        async def mem():
            d = await self._get("/diagnostics/system/system_resources",
                                "/diagnostics/system/systemResources")
            m = d.get("memory", {})
            total, used = _num(m.get("total")), _num(m.get("used"))
            if total:
                st.mem = 100 * (used or 0) / total

        async def states():
            d = await self._get("/diagnostics/firewall/pf_states", "/diagnostics/firewall/pfStates")
            cur, lim = _num(d.get("current")), _num(d.get("limit"))
            st.states = int(cur) if cur is not None else None
            st.states_limit = int(lim) if lim else None

        if run is None:
            await asyncio.gather(cpu(), mem(), states(), return_exceptions=True)
        else:
            await asyncio.gather(run("cpu", cpu(), keep=False), run("memory", mem(), keep=False),
                                 run("states", states(), keep=False))
        return st

    async def poll(self) -> Raw:
        raw = Raw()
        run = PollRun(self.api, raw, self._last)
        ifaces, traffic, gws, arp, log, stats = await asyncio.gather(
            run("interfaces", self.interfaces()),
            run("traffic", self._traffic()),
            run("gateways", self._get("/routes/gateway/status")),
            run("arp", self._get("/diagnostics/interface/get_arp", "/diagnostics/interface/getArp")),
            run("firewall log", self._fw_log(), keep=False),
            self.stats(run),
        )
        raw.ifaces = ifaces or []
        if traffic:
            ts, counters = traffic
            for i in raw.ifaces:
                if i.key in counters:
                    i.rx_bps, i.tx_bps = self._rates.update(i.key, ts, counters[i.key])
        raw.gateways = parse_gateways(gws) if gws else []
        raw.arp = parse_arp(arp) if arp else []
        raw.fw_log = parse_fw_log(log) if log else []
        # Only now, with the rows in this poll's result: next time, ask for what came after.
        self._log_digest = newest_digest(log) or self._log_digest
        raw.fw = stats
        run.finish()
        return raw

    # --- actions (only used when actions are enabled in the config) ----------
    async def _post(self, *paths: str, json: dict) -> dict:
        return await self.api.post_json(*paths, json=json) or {}

    async def block_ip(self, alias: str, ip: str) -> str:
        """Add ip to a Host/Network/External alias. A rule must reference the alias for this to block."""
        res = await self._post(f"/firewall/alias_util/add/{alias}", json={"address": ip})
        if res.get("status") not in ("done", "ok", None):
            raise RuntimeError(f"OPNsense answered {res}")
        return f"{ip} added to alias {alias}"

    async def kill_states(self, ip: str) -> str:
        res = await self._post("/diagnostics/firewall/kill_states", "/diagnostics/firewall/killStates",
                               json={"filter": ip})
        if res.get("result") not in ("ok", None):
            raise RuntimeError(f"OPNsense answered {res}")
        dropped = res.get("dropped_states")
        return f"dropped {dropped} states for {ip}" if dropped is not None else f"states for {ip} killed"

    async def close(self) -> None:
        await self._slow.close()
        await self.api.close()
