"""OPNsense REST API client (read-only).

Uses the snake_case endpoints introduced in OPNsense 25.7 and falls back to the older
camelCase names on 404, so it works on both.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from datetime import datetime

import httpx

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
        ))
    return out


def parse_arp(data) -> list[ArpEntry]:
    rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
    return [ArpEntry(ip=r.get("ip", ""), mac=(r.get("mac") or "").upper(),
                     device=r.get("intf") or r.get("interface") or "",
                     hostname=r.get("hostname") or "")
            for r in rows if r.get("ip") and r.get("mac") and r.get("expired") is not True]


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
        url = cfg["url"].rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        key = os.environ.get("OPNSENSE_KEY") or cfg.get("key")
        secret = os.environ.get("OPNSENSE_SECRET") or cfg.get("secret")
        if not key or not secret:
            raise ValueError("opnsense: set key/secret in the config or OPNSENSE_KEY/OPNSENSE_SECRET")
        self.client = httpx.AsyncClient(base_url=url, auth=(key, secret),
                                        verify=cfg.get("verify_tls", True),
                                        timeout=cfg.get("timeout", 10), transport=transport)
        self.name = cfg.get("name", "opnsense")
        self.log_limit = int(cfg.get("log_limit", 500))
        self._ifaces: tuple[float, list[Iface]] = (0.0, [])
        self._prev_traffic: tuple[float, dict[str, tuple[float, float]]] | None = None

    async def _get(self, *paths: str, params: dict | None = None):
        for path in paths:
            r = await self.client.get(path, params=params)
            if r.status_code == 404:
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"{path}: HTTP {r.status_code} (check the API user's privileges)")
            if r.status_code >= 400:
                raise RuntimeError(f"{path}: HTTP {r.status_code}")
            return r.json()
        raise RuntimeError(f"{paths[0]}: endpoint not found on this OPNsense version")

    async def interfaces(self) -> list[Iface]:
        ts, cached = self._ifaces
        if time.time() - ts < 60 and cached:
            return [Iface(**vars(i)) for i in cached]
        data = await self._get("/interfaces/overview/interfaces_info",
                               "/interfaces/overview/interfacesInfo")
        ifaces = parse_interfaces(data)
        self._ifaces = (time.time(), ifaces)
        return [Iface(**vars(i)) for i in ifaces]

    async def traffic(self) -> dict[str, tuple[float, float]]:
        """Interface key -> (rx bit/s, tx bit/s), from counter deltas."""
        data = await self._get("/diagnostics/traffic/interface")
        now = time.time()
        counters = {}
        for key, row in (data.get("interfaces") or {}).items():
            rx = _num(_first(row, "bytes received", "bytes_received", "inbytes", "rx_bytes"))
            tx = _num(_first(row, "bytes transmitted", "bytes_transmitted", "outbytes", "tx_bytes"))
            if rx is not None and tx is not None:
                counters[key] = (rx, tx)
        rates = {}
        if self._prev_traffic:
            t0, prev = self._prev_traffic
            dt = max(0.5, now - t0)
            for key, (rx, tx) in counters.items():
                if key in prev:
                    rates[key] = (max(0.0, rx - prev[key][0]) * 8 / dt,
                                  max(0.0, tx - prev[key][1]) * 8 / dt)
        self._prev_traffic = (now, counters)
        return rates

    async def stats(self) -> FwStats:
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

        await asyncio.gather(cpu(), mem(), states(), return_exceptions=True)
        return st

    async def poll(self, raw: Raw) -> None:
        async def guard(label, coro):
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                raw.errors.append(f"opnsense {label}: {e}")
                return None

        ifaces, traffic, gws, arp, log, stats = await asyncio.gather(
            guard("interfaces", self.interfaces()),
            guard("traffic", self.traffic()),
            guard("gateways", self._get("/routes/gateway/status")),
            guard("arp", self._get("/diagnostics/interface/get_arp",
                                   "/diagnostics/interface/getArp")),
            guard("firewall log", self._get("/diagnostics/firewall/log",
                                            params={"limit": self.log_limit})),
            guard("stats", self.stats()),
        )
        raw.ifaces = ifaces or []
        for i in raw.ifaces:
            i.rx_bps, i.tx_bps = (traffic or {}).get(i.key, (0.0, 0.0))
        raw.gateways = parse_gateways(gws) if gws else []
        raw.arp = parse_arp(arp) if arp else []
        raw.fw_log = parse_fw_log(log) if log else []
        raw.fw = stats or FwStats(name=self.name)

    # --- actions (only used when actions are enabled in the config) ----------
    async def _post(self, *paths: str, json: dict) -> dict:
        for path in paths:
            r = await self.client.post(path, json=json)
            if r.status_code == 404:
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"permission denied (HTTP {r.status_code}) on {path}")
            if r.status_code >= 400:
                raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:120]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"{paths[0]}: endpoint not found on this OPNsense version")

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
        await self.client.aclose()
