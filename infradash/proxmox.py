"""Proxmox VE REST API client (API token auth).

Built to keep working while the cluster is having a bad day: requests fail over to other
nodes when the one in the config is down, anything slow (guest configs, guest agents, ZFS,
replication, Ceph, HA) is refreshed in the background within a time budget, and a short
outage shows the last good data, marked stale, rather than an empty dashboard.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import time
from urllib.parse import quote

import httpx

from .api import ApiClient, ApiError, PollRun
from .cache import LastGood, Rates, Refresher
from .models import (Ceph, Guest, HaService, HaStatus, Nic, Node, PveLog, Raw, Replication, Storage, Task,
                     ZPool)

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


def _ha_mode(status: str) -> str:
    """'tower-1 (active, Tue Oct 13 10:00:00 2026)' -> 'active'."""
    m = re.search(r"\(([^,)]+)", status or "")
    return m.group(1).strip() if m else (status or "")


def parse_ha_status(data) -> HaStatus | None:
    ha = HaStatus()
    for e in data or []:
        kind = e.get("type")
        if kind == "master":
            ha.master, ha.master_state = e.get("node"), _ha_mode(e.get("status", ""))
        elif kind == "lrm":
            ha.lrm[e.get("node", "")] = _ha_mode(e.get("status", ""))
        elif kind == "service":
            sid = e.get("sid") or str(e.get("id", "")).removeprefix("service:")
            ha.services.append(HaService(sid, e.get("node", ""), e.get("state") or e.get("crm_state") or "",
                                         e.get("request_state", "")))
    return ha if (ha.master or ha.lrm or ha.services) else None


def parse_cluster_status(data) -> tuple[str, bool | None, dict[str, Node]]:
    cluster, quorate, nodes = "", None, {}
    for s in data or []:
        if s.get("type") == "cluster":
            cluster, quorate = s.get("name", ""), bool(s.get("quorate"))
        elif s.get("type") == "node":
            nodes[s["name"]] = Node(s["name"], bool(s.get("online", 1)), 0.0, 0.0, ip=s.get("ip"))
    return cluster, quorate, nodes


def parse_resources(data, nodes: dict[str, Node]) -> tuple[list[Guest], list[Storage]]:
    """Fills `nodes` in place; guest network rates are left at zero for the caller."""
    guests: list[Guest] = []
    storages: list[Storage] = []
    shared_seen: set[str] = set()
    for r in data or []:
        kind = r.get("type")
        if kind == "node":
            n = nodes.setdefault(r["node"], Node(r["node"], r.get("status") == "online", 0, 0))
            n.online = r.get("status") == "online"
            n.cpu = 100 * float(r.get("cpu") or 0)
            n.mem = 100 * float(r.get("mem") or 0) / float(r.get("maxmem") or 1)
            n.uptime = float(r.get("uptime") or 0)
        elif kind in ("qemu", "lxc") and not r.get("template"):
            guests.append(Guest(
                vmid=int(r["vmid"]), name=r.get("name") or str(r["vmid"]), kind=kind,
                node=r.get("node", ""), status=r.get("status", "unknown"),
                cpu=100 * float(r.get("cpu") or 0),
                mem=100 * float(r.get("mem") or 0) / float(r.get("maxmem") or 1),
                maxmem=float(r.get("maxmem") or 0), uptime=float(r.get("uptime") or 0)))
        elif kind == "storage":
            name = r.get("storage") or str(r.get("id", "")).split("/")[-1]
            shared = bool(r.get("shared"))
            if shared:
                if name in shared_seen:
                    continue
                shared_seen.add(name)
            storages.append(Storage(
                name=name, node=None if shared else r.get("node"), kind=r.get("plugintype", ""),
                used=float(r.get("disk") or 0), total=float(r.get("maxdisk") or 0),
                active=r.get("status", "available") == "available"))
    return guests, storages


def net_counters(data) -> dict[int, tuple[float, float]]:
    return {int(r["vmid"]): (float(r.get("netin") or 0), float(r.get("netout") or 0))
            for r in data or [] if r.get("type") in ("qemu", "lxc") and not r.get("template")}


def parse_tasks(data, now: float, limit: int = 60) -> list[Task]:
    out = []
    for t in sorted(data or [], key=lambda t: -t.get("starttime", 0))[:limit]:
        tid = str(t.get("id") or "")
        out.append(Task(
            upid=t["upid"], node=t.get("node", ""), type=t.get("type", ""),
            vmid=tid if tid.isdigit() else None, user=t.get("user", ""),
            start=float(t.get("starttime", now)), end=t.get("endtime"),
            status=t.get("status") or ("running" if not t.get("endtime") else "OK")))
    return out


def parse_cluster_log(data, now: float) -> list[PveLog]:
    return [PveLog(ts=float(e.get("time", now)), node=e.get("node", ""),
                   level=PRI_LEVEL.get(int(e.get("pri", 6)), "INFO"), tag=e.get("tag", ""),
                   msg=e.get("msg", ""),
                   key=str(e.get("uid") or f"{e.get('time')}{e.get('node')}{e.get('msg')}"))
            for e in data or []]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def node_urls(base: str, node_addrs: dict[str, list[str]], names: list[str]) -> list[str]:
    """API URLs for the other cluster nodes, reachable the same way as the configured one.

    If the configured URL uses an IP, each node's address on that same subnet is used (the
    cluster's own addresses may be on a network you can't reach, like a dedicated Corosync
    VLAN). If it uses a hostname that starts with a node's name, the other node names are
    swapped in (tower-1.home.arpa -> tower-2.home.arpa)."""
    url = httpx.URL(base)
    host, out = url.host, []
    if _is_ip(host):
        ip = ipaddress.ip_address(host)
        for name in names:
            for cidr in node_addrs.get(name, []):
                try:
                    iface = ipaddress.ip_interface(cidr)
                except ValueError:
                    continue
                if ip in iface.network and iface.ip != ip:
                    out.append(str(url.copy_with(host=str(iface.ip))))
    else:
        first, dot, rest = host.partition(".")
        if first in names:
            out += [str(url.copy_with(host=f"{n}{dot}{rest}")) for n in names if n != first]
    return [u.rstrip("/") for u in out]


class ProxmoxClient:
    def __init__(self, cfg: dict, transport: httpx.AsyncBaseTransport | None = None) -> None:
        urls = cfg["url"] if isinstance(cfg["url"], list) else [cfg["url"]]
        urls = [f"{u.rstrip('/')}/api2/json" for u in urls]
        token_id = os.environ.get("PVE_TOKEN_ID") or cfg.get("token_id")
        secret = os.environ.get("PVE_TOKEN_SECRET") or cfg.get("token_secret")
        if not token_id or not secret:
            raise ValueError("proxmox: set token_id/token_secret or PVE_TOKEN_ID/PVE_TOKEN_SECRET")
        self.api = ApiClient(
            "proxmox", urls, headers={"Authorization": f"PVEAPIToken={token_id}={secret}"},
            verify=cfg.get("verify_tls", True), timeout=float(cfg.get("timeout", 10)),
            max_connections=int(cfg.get("max_connections", 8)), transport=transport,
            denied_hint=" (check the token's permissions)")
        self.name = cfg.get("name", "")
        self.use_agent = cfg.get("guest_agent", True)
        self.discover = bool(cfg.get("discover_nodes", True))
        self.config_ttl = float(cfg.get("config_refresh_s", 300))
        self.budget = float(cfg.get("slow_call_budget_s", 3))
        self._slow = Refresher()
        self._last = LastGood(float(cfg.get("stale_max_s", 60)))
        self._net = Rates(scale=8)              # bytes -> bits
        self._ceph_supported: bool | None = None

    async def _get(self, path: str, params: dict | None = None):
        data = await self.api.get_json(path, params=params)
        return data.get("data") if isinstance(data, dict) else None

    async def _timed(self, path: str, params: dict | None = None):
        return time.time(), await self._get(path, params)

    # --- guest details ----------------------------------------------------------
    async def _config(self, g: Guest) -> dict:
        async def fetch():
            return await self._get(f"/nodes/{g.node}/{g.kind}/{g.vmid}/config") or {}
        # Keyed by node too, so a migrated guest's config is re-read from its new node.
        return await self._slow.get(("config", g.vmid, g.node), self.config_ttl, fetch, self.budget) or {}

    async def _agent(self, g: Guest) -> dict[str, list[str]]:
        async def fetch():
            ips: dict[str, list[str]] = {}
            try:
                data = await self._get(f"/nodes/{g.node}/qemu/{g.vmid}/agent/network-get-interfaces")
            except ApiError:
                return ips      # agent not running / not permitted — ARP usually fills the gap
            for iface in (data or {}).get("result", []):
                mac = (iface.get("hardware-address") or "").upper()
                for a in iface.get("ip-addresses", []):
                    if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                        ips.setdefault(mac, []).append(a["ip-address"])
            return ips
        return await self._slow.get(("agent", g.vmid), 120, fetch, self.budget) or {}

    async def _enrich(self, g: Guest) -> None:
        cfg = await self._config(g)
        if not cfg:
            return
        g.nics = parse_config_nics(cfg)
        g.disks = parse_config_disks(cfg)
        if self.use_agent and g.kind == "qemu" and g.status == "running" and agent_enabled(cfg):
            by_mac = await self._agent(g)
            for nic in g.nics:
                nic.ips = list(dict.fromkeys(nic.ips + by_mac.get(nic.mac, [])))

    # --- poll ---------------------------------------------------------------------
    async def poll(self) -> Raw:
        raw = Raw()
        run = PollRun(self.api, raw, self._last)
        status, resources, tasks, log = await asyncio.gather(
            run("cluster status", self._get("/cluster/status")),
            run("resources", self._timed("/cluster/resources")),
            run("tasks", self._get("/cluster/tasks")),
            run("cluster log", self._get("/cluster/log", {"max": 100}), keep=False),
        )
        now = time.time()
        raw.cluster, raw.quorate, nodes = parse_cluster_status(status)
        guests, storages = [], []
        if resources:
            fetched, data = resources
            guests, storages = parse_resources(data, nodes)
            counters = net_counters(data)
            for g in guests:
                if g.vmid in counters:
                    g.net_in, g.net_out = self._net.update(g.vmid, fetched, counters[g.vmid])
        raw.storages = storages
        online = [n.name for n in nodes.values() if n.online]
        await asyncio.gather(*(self._enrich(g) for g in guests),
                             self._storage_health(online, raw, run),
                             self._ha(raw, run),
                             self._discover(nodes, run))
        raw.nodes = list(nodes.values())
        raw.guests = guests
        if not raw.cluster:
            raw.cluster = self.name or (raw.nodes[0].name if len(raw.nodes) == 1 else "proxmox")
        raw.tasks = parse_tasks(tasks, now)
        raw.pve_log = parse_cluster_log(log, now)
        run.finish()
        return raw

    async def _ha(self, raw: Raw, run: PollRun) -> None:
        async def fetch():
            return parse_ha_status(await self._get("/cluster/ha/status/current"))
        raw.ha = await self._slow.get("ha", 10, fetch, self.budget)
        err = self._slow.error("ha")
        if err and not (isinstance(err, ApiError) and (err.denied or err.transport)):
            run.note("ha status", err)

    async def _discover(self, nodes: dict[str, Node], run: PollRun) -> None:
        """Learn the other nodes' API addresses, to fail over to when ours goes down."""
        if not self.discover or len(nodes) < 2:
            return
        base = self.api.urls[0]
        names = sorted(nodes)

        async def addrs(name):
            async def fetch():
                data = await self._get(f"/nodes/{name}/network") or []
                return [i["cidr"] for i in data if i.get("cidr")] + \
                       [f"{i['address']}/{i['netmask']}" for i in data if i.get("address") and i.get("netmask")]
            return name, await self._slow.get(("net", name), 3600, fetch, self.budget) or []

        found: dict[str, list[str]] = {}
        if _is_ip(httpx.URL(base).host):    # hostnames need no lookup: see node_urls
            found = dict(await asyncio.gather(*(addrs(n) for n in names if nodes[n].online)))
        self.api.add_fallbacks(node_urls(base, found, names))

    async def _storage_health(self, nodes: list[str], raw: Raw, run: PollRun) -> None:
        async def zfs(node):
            async def fetch():
                data = await self._get(f"/nodes/{node}/disks/zfs") or []
                return [ZPool(node, p["name"], p.get("health", "UNKNOWN"), float(p.get("size") or 0),
                              float(p.get("alloc") or 0), float(p.get("frag") or 0)) for p in data]
            pools = await self._slow.get(("zfs", node), 60, fetch, self.budget) or []
            if err := self._slow.error(("zfs", node)):
                run.note(f"zfs on {node}", err)
            return pools

        async def repl(node):
            async def fetch():
                data = await self._get(f"/nodes/{node}/replication") or []
                return [Replication(j.get("id", ""), int(j.get("guest", 0)), node, j.get("target", ""),
                                    j.get("last_sync") or None, int(j.get("fail_count") or 0),
                                    j.get("error", "")) for j in data]
            return await self._slow.get(("repl", node), 60, fetch, self.budget) or []

        async def ceph():
            if self._ceph_supported is False:
                return None

            async def fetch():
                try:
                    st = parse_ceph(await self._get("/cluster/ceph/status") or {})
                except ApiError as e:
                    if self._ceph_supported is None and not e.transport:
                        self._ceph_supported = False    # not installed (or not permitted): stay quiet
                        return None
                    raise
                self._ceph_supported = True
                return st
            st = await self._slow.get("ceph", 15, fetch, self.budget)
            err = self._slow.error("ceph")
            if err and self._ceph_supported and not (isinstance(err, ApiError) and err.transport):
                # It worked before, so this is a real problem worth surfacing.
                st = Ceph("HEALTH_ERR", [("HEALTH_ERR", f"ceph status unavailable: {err}")])
            return st

        results = await asyncio.gather(*(zfs(n) for n in nodes), *(repl(n) for n in nodes), ceph())
        for pools in results[: len(nodes)]:
            raw.zpools.extend(pools)
        for jobs in results[len(nodes): 2 * len(nodes)]:
            raw.replication.extend(jobs)
        raw.ceph = results[-1]

    # --- actions (only used when actions are enabled in the config) ----------
    async def _post(self, path: str, data: dict | None = None) -> str:
        try:
            res = await self.api.post_json(path, data=data or {})
        except ApiError as e:
            if e.denied:
                raise RuntimeError(f"permission denied (HTTP {e.status}) — the token needs "
                                   "VM.PowerMgmt / VM.Migrate on this guest") from e
            raise
        return str((res or {}).get("data") or "")

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
        await self._slow.close()
        await self.api.close()
