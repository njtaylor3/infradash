"""Proxmox VE REST API client (read-only, API token auth)."""
from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import quote

import httpx

from .models import Ceph, Guest, Nic, Node, PveLog, Raw, Replication, Storage, Task, ZPool

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
NET_KEY_RE = re.compile(r"^net\d+$")
DISK_KEY_RE = re.compile(r"^(scsi|virtio|sata|ide|mp)\d+$|^(efidisk0|tpmstate0|rootfs)$")
PRI_LEVEL = {0: "ERROR", 1: "ERROR", 2: "ERROR", 3: "ERROR", 4: "WARN", 5: "INFO", 6: "INFO", 7: "DEBUG"}


def parse_nic(name: str, spec: str) -> Nic | None:
    """qemu: 'virtio=BC:24:11:..,bridge=vmbr0,tag=20'   lxc: 'name=eth0,hwaddr=..,ip=10.0.0.5/24'"""
    kv = {}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        kv[k.strip()] = v.strip()
    mac = kv.get("hwaddr") or kv.get("macaddr") or next(
        (v for v in kv.values() if MAC_RE.match(v)), None)
    if not mac:
        return None
    ips = []
    ip = kv.get("ip", "")
    if ip and ip not in ("dhcp", "manual"):
        ips.append(ip.split("/")[0])
    tag = kv.get("tag")
    return Nic(name=name, mac=mac.upper(), bridge=kv.get("bridge", ""),
               vlan=int(tag) if tag and tag.isdigit() else None, ips=ips)


def parse_config_nics(config: dict) -> list[Nic]:
    nics = [parse_nic(k, v) for k, v in sorted(config.items()) if NET_KEY_RE.match(k)]
    return [n for n in nics if n]


def parse_config_disks(config: dict) -> list[tuple[str, str]]:
    """'local-zfs:vm-101-disk-0,size=32G' -> ('local-zfs', '32G'); CD-ROMs and passthrough skipped."""
    out = []
    for k, v in sorted(config.items()):
        if not DISK_KEY_RE.match(k) or not isinstance(v, str) or "media=cdrom" in v:
            continue
        vol = v.split(",")[0]
        if ":" not in vol or vol.startswith("/"):
            continue
        size = next((p[5:] for p in v.split(",") if p.startswith("size=")), "")
        out.append((vol.split(":")[0], size))
    return out


def parse_ceph(d: dict) -> Ceph:
    health = d.get("health", {})
    checks = [(c.get("severity", ""), (c.get("summary") or {}).get("message", name))
              for name, c in (health.get("checks") or {}).items()]
    osdmap = d.get("osdmap", {})
    osdmap = osdmap.get("osdmap", osdmap)          # older releases nest it
    pgmap = d.get("pgmap", {})
    states = pgmap.get("pgs_by_state", [])
    clean = sum(s.get("count", 0) for s in states if s.get("state_name") == "active+clean")
    pgs = int(pgmap.get("num_pgs") or sum(s.get("count", 0) for s in states))
    return Ceph(health=health.get("status", "UNKNOWN"), checks=checks,
                osds=int(osdmap.get("num_osds", 0)), osds_up=int(osdmap.get("num_up_osds", 0)),
                osds_in=int(osdmap.get("num_in_osds", 0)),
                used=float(pgmap.get("bytes_used", 0)), total=float(pgmap.get("bytes_total", 0)),
                pgs=pgs, pgs_unclean=max(0, pgs - clean) if states else 0)


def agent_enabled(config: dict) -> bool:
    a = str(config.get("agent", "0"))
    return a.startswith("1") or "enabled=1" in a


class ProxmoxClient:
    def __init__(self, cfg: dict, transport: httpx.AsyncBaseTransport | None = None) -> None:
        url = cfg["url"].rstrip("/")
        token_id = os.environ.get("PVE_TOKEN_ID") or cfg.get("token_id")
        secret = os.environ.get("PVE_TOKEN_SECRET") or cfg.get("token_secret")
        if not token_id or not secret:
            raise ValueError("proxmox: set token_id/token_secret or PVE_TOKEN_ID/PVE_TOKEN_SECRET")
        self.client = httpx.AsyncClient(
            base_url=f"{url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={token_id}={secret}"},
            verify=cfg.get("verify_tls", True), timeout=cfg.get("timeout", 10), transport=transport)
        self.name = cfg.get("name", "")
        self.use_agent = cfg.get("guest_agent", True)
        self.config_ttl = float(cfg.get("config_refresh_s", 300))
        self._configs: dict[int, tuple[float, str, dict]] = {}     # vmid -> (ts, node, config)
        self._agent_ips: dict[int, tuple[float, dict[str, list[str]]]] = {}
        self._prev_net: dict[int, tuple[float, float, float]] = {}  # vmid -> (ts, in, out)
        self._sem = asyncio.Semaphore(8)
        self._zfs: dict[str, tuple[float, list[ZPool]]] = {}
        self._repl: dict[str, tuple[float, list[Replication]]] = {}
        self._ceph: tuple[float, Ceph | None] = (0.0, None)
        self._ceph_supported: bool | None = None

    async def _get(self, path: str, params: dict | None = None):
        async with self._sem:
            r = await self.client.get(path, params=params)
        if r.status_code in (401, 403):
            raise RuntimeError(f"{path}: HTTP {r.status_code} (check the token's permissions)")
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:80]}")
        return r.json().get("data")

    async def _config(self, g: Guest) -> dict:
        cached = self._configs.get(g.vmid)
        if cached and cached[1] == g.node and time.time() - cached[0] < self.config_ttl:
            return cached[2]
        cfg = await self._get(f"/nodes/{g.node}/{g.kind}/{g.vmid}/config") or {}
        self._configs[g.vmid] = (time.time(), g.node, cfg)
        return cfg

    async def _agent(self, g: Guest) -> dict[str, list[str]]:
        cached = self._agent_ips.get(g.vmid)
        if cached and time.time() - cached[0] < 120:
            return cached[1]
        ips: dict[str, list[str]] = {}
        try:
            data = await self._get(f"/nodes/{g.node}/qemu/{g.vmid}/agent/network-get-interfaces")
            for iface in (data or {}).get("result", []):
                mac = (iface.get("hardware-address") or "").upper()
                for a in iface.get("ip-addresses", []):
                    if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                        ips.setdefault(mac, []).append(a["ip-address"])
        except RuntimeError:
            pass   # agent not running / not permitted — ARP will usually fill the gap
        self._agent_ips[g.vmid] = (time.time(), ips)
        return ips

    async def _enrich(self, g: Guest) -> None:
        try:
            cfg = await self._config(g)
        except RuntimeError:
            return
        g.nics = parse_config_nics(cfg)
        g.disks = parse_config_disks(cfg)
        if self.use_agent and g.kind == "qemu" and g.status == "running" and agent_enabled(cfg):
            by_mac = await self._agent(g)
            for nic in g.nics:
                nic.ips = list(dict.fromkeys(nic.ips + by_mac.get(nic.mac, [])))

    async def poll(self, raw: Raw) -> None:
        async def guard(label, coro):
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                raw.errors.append(f"proxmox {label}: {e}")
                return None

        status, resources, tasks, log = await asyncio.gather(
            guard("cluster status", self._get("/cluster/status")),
            guard("resources", self._get("/cluster/resources")),
            guard("tasks", self._get("/cluster/tasks")),
            guard("cluster log", self._get("/cluster/log", {"max": 100})),
        )
        now = time.time()
        nodes: dict[str, Node] = {}
        for s in status or []:
            if s.get("type") == "cluster":
                raw.cluster, raw.quorate = s.get("name", ""), bool(s.get("quorate"))
            elif s.get("type") == "node":
                nodes[s["name"]] = Node(s["name"], bool(s.get("online", 1)), 0.0, 0.0, ip=s.get("ip"))
        guests: list[Guest] = []
        for r in resources or []:
            if r.get("type") == "node":
                n = nodes.setdefault(r["node"], Node(r["node"], r.get("status") == "online", 0, 0))
                n.online = r.get("status") == "online"
                n.cpu = 100 * float(r.get("cpu") or 0)
                n.mem = 100 * float(r.get("mem") or 0) / float(r.get("maxmem") or 1)
                n.uptime = float(r.get("uptime") or 0)
            elif r.get("type") in ("qemu", "lxc") and not r.get("template"):
                vmid = int(r["vmid"])
                g = Guest(vmid=vmid, name=r.get("name") or str(vmid), kind=r["type"],
                          node=r.get("node", ""), status=r.get("status", "unknown"),
                          cpu=100 * float(r.get("cpu") or 0),
                          mem=100 * float(r.get("mem") or 0) / float(r.get("maxmem") or 1),
                          maxmem=float(r.get("maxmem") or 0), uptime=float(r.get("uptime") or 0))
                nin, nout = float(r.get("netin") or 0), float(r.get("netout") or 0)
                prev = self._prev_net.get(vmid)
                if prev and now > prev[0]:
                    dt = now - prev[0]
                    g.net_in = max(0.0, nin - prev[1]) * 8 / dt
                    g.net_out = max(0.0, nout - prev[2]) * 8 / dt
                self._prev_net[vmid] = (now, nin, nout)
                guests.append(g)
        shared_seen: set[str] = set()
        for r in resources or []:
            if r.get("type") != "storage":
                continue
            name = r.get("storage") or str(r.get("id", "")).split("/")[-1]
            shared = bool(r.get("shared"))
            if shared:
                if name in shared_seen:
                    continue
                shared_seen.add(name)
            raw.storages.append(Storage(
                name=name, node=None if shared else r.get("node"), kind=r.get("plugintype", ""),
                used=float(r.get("disk") or 0), total=float(r.get("maxdisk") or 0),
                active=r.get("status", "available") == "available"))
        online = [n.name for n in nodes.values() if n.online]
        await asyncio.gather(*(self._enrich(g) for g in guests),
                             self._storage_health(online, raw))
        raw.nodes = list(nodes.values())
        raw.guests = guests
        if not raw.cluster:
            raw.cluster = self.name or (raw.nodes[0].name if len(raw.nodes) == 1 else "proxmox")

        for t in sorted(tasks or [], key=lambda t: -t.get("starttime", 0))[:60]:
            tid = str(t.get("id") or "")
            raw.tasks.append(Task(
                upid=t["upid"], node=t.get("node", ""), type=t.get("type", ""),
                vmid=tid if tid.isdigit() else None, user=t.get("user", ""),
                start=float(t.get("starttime", now)), end=t.get("endtime"),
                status=t.get("status") or ("running" if not t.get("endtime") else "OK"),
            ))
        for e in log or []:
            raw.pve_log.append(PveLog(
                ts=float(e.get("time", now)), node=e.get("node", ""),
                level=PRI_LEVEL.get(int(e.get("pri", 6)), "INFO"), tag=e.get("tag", ""),
                msg=e.get("msg", ""), key=str(e.get("uid") or f"{e.get('time')}{e.get('node')}{e.get('msg')}"),
            ))

    async def _storage_health(self, nodes: list[str], raw: Raw) -> None:
        now = time.time()

        async def zfs(node):
            ts, pools = self._zfs.get(node, (0.0, []))
            if now - ts > 60:
                try:
                    data = await self._get(f"/nodes/{node}/disks/zfs") or []
                    pools = [ZPool(node, p["name"], p.get("health", "UNKNOWN"), float(p.get("size") or 0),
                                   float(p.get("alloc") or 0), float(p.get("frag") or 0)) for p in data]
                except RuntimeError as e:
                    raw.errors.append(f"proxmox zfs on {node}: {e}")
                self._zfs[node] = (now, pools)
            return pools

        async def repl(node):
            ts, jobs = self._repl.get(node, (0.0, []))
            if now - ts > 60:
                try:
                    data = await self._get(f"/nodes/{node}/replication") or []
                    jobs = [Replication(j.get("id", ""), int(j.get("guest", 0)), node, j.get("target", ""),
                                        j.get("last_sync") or None, int(j.get("fail_count") or 0),
                                        j.get("error", "")) for j in data]
                except RuntimeError:
                    jobs = []
                self._repl[node] = (now, jobs)
            return jobs

        async def ceph():
            if self._ceph_supported is False:
                return None
            ts, st = self._ceph
            if now - ts > 15:
                try:
                    st = parse_ceph(await self._get("/cluster/ceph/status") or {})
                    self._ceph_supported = True
                except RuntimeError as e:
                    if self._ceph_supported is None:
                        # Ceph not installed (or not permitted): stop asking, stay quiet.
                        self._ceph_supported = False
                        st = None
                    else:
                        # It worked before, so this is a real problem worth surfacing.
                        st = Ceph("HEALTH_ERR", [("HEALTH_ERR", f"ceph status unavailable: {e}")])
                self._ceph = (now, st)
            return st

        results = await asyncio.gather(*(zfs(n) for n in nodes), *(repl(n) for n in nodes), ceph())
        for pools in results[: len(nodes)]:
            raw.zpools.extend(pools)
        for jobs in results[len(nodes): 2 * len(nodes)]:
            raw.replication.extend(jobs)
        raw.ceph = results[-1]

    # --- actions (only used when actions are enabled in the config) ----------
    async def _post(self, path: str, data: dict | None = None) -> str:
        async with self._sem:
            r = await self.client.post(path, data=data or {})
        if r.status_code in (401, 403):
            raise RuntimeError(f"permission denied (HTTP {r.status_code}) — the token needs "
                               "VM.PowerMgmt / VM.Migrate on this guest")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        return str(r.json().get("data") or "")

    async def guest_power(self, node: str, kind: str, vmid: int, op: str) -> str:
        assert op in ("start", "shutdown", "reboot", "stop")
        return await self._post(f"/nodes/{node}/{kind}/{vmid}/status/{op}")

    async def migrate(self, node: str, kind: str, vmid: int, target: str, running: bool) -> str:
        data = {"target": target}
        if kind == "qemu":
            data["with-local-disks"] = 1
            if running:
                data["online"] = 1
        elif running:
            data["restart"] = 1          # containers can't live-migrate
        return await self._post(f"/nodes/{node}/{kind}/{vmid}/migrate", data)

    async def task_log(self, node: str, upid: str) -> list[str]:
        data = await self._get(f"/nodes/{node}/tasks/{quote(upid, safe='')}/log", {"limit": 500})
        return [line.get("t", "") for line in data or []]

    async def close(self) -> None:
        await self.client.aclose()
