"""Builds the two topology views and the inspector text from a Snapshot."""
from __future__ import annotations

import ipaddress
from collections import Counter
from datetime import datetime

from rich.text import Text

from .correlate import Snapshot
import time

from .models import Event, FwEntry, Guest, Issue, Storage, Task, ZPool, fmt_bps, fmt_uptime
from .widgets import TNode, pct_style, sparkline

GUEST_ICON = {"qemu": "▪", "lxc": "▫"}
GIB = 2**30
SEV_MARK = {"crit": ("✖", "bold red"), "warn": ("▲", "yellow")}
GW_WORDS = {"loss": "packet loss", "delay": "high latency", "down": "DOWN"}


def _pct(t: Text, label: str, v: float | None, warn=75, crit=90) -> None:
    if v is None:
        return
    t.append(f" · {label} ", style="dim")
    t.append(f"{v:.0f}%", style=pct_style(v, warn, crit))


def _gw_style(status: str, loss: float | None, rtt: float | None) -> str:
    if status == "down" or (loss or 0) >= 20:
        return "bold red"
    if status in ("loss", "delay") or (loss or 0) >= 2 or (rtt or 0) >= 150:
        return "yellow"
    return "green"


def guest_label(snap: Snapshot, g: Guest, show_node: bool, show_net: bool) -> Text:
    running = g.status == "running"
    t = Text()
    t.append(GUEST_ICON.get(g.kind, "▪") + " ", style="green" if running else "dim")
    t.append(g.label, style="" if running else "dim")
    if show_node:
        t.append(f" @{g.node}", style="dim")
    if not running:
        t.append(f"  {g.status}", style="dim")
    else:
        _pct(t, "cpu", g.cpu)
        _pct(t, "mem", g.mem, 85, 95)
    if show_net:
        nets = [snap.name(f"if:{k}") for k in snap.placement.get(g.id, [])]
        t.append("  → " + (", ".join(nets) if nets else "?"), style="cyan" if nets else "dim")
    return t


def _fw_counts(snap: Snapshot) -> Counter:
    c: Counter = Counter()
    for e in snap.events:
        if e.source == "fw" and e.level != "INFO" and e.iface:
            c[e.iface] += 1
    return c


def network_tree(snap: Snapshot) -> list[TNode]:
    raw = snap.raw
    if not raw.fw and not raw.ifaces:
        return []
    root_label = Text("◆ ", style="orange1")
    root_label.append(snap.name("fw"), style="bold orange1")
    if raw.fw:
        _pct(root_label, "cpu", raw.fw.cpu)
        _pct(root_label, "mem", raw.fw.mem, 85, 95)
        if raw.fw.states is not None:
            root_label.append(f" · {raw.fw.states:,} states", style="dim")
    root = TNode("fw", root_label)
    blocks = _fw_counts(snap)
    ifaces = sorted(raw.ifaces, key=lambda i: (not i.key.startswith("wan"), i.name.lower()))
    placed: set[str] = set()
    for i in ifaces:
        lab = Text("⇄ ", style="cyan" if i.up else "bold red")
        lab.append(i.name, style="cyan")
        lab.append(f" {i.device}", style="dim")
        if i.cidr:
            lab.append(f" {i.cidr}")
        if i.vlan is not None:
            lab.append(f" vlan {i.vlan}", style="dim")
        if not i.up:
            lab.append(" DOWN", style="bold red")
        lab.append(f"  ↓{fmt_bps(i.rx_bps)} ↑{fmt_bps(i.tx_bps)}", style="dim")
        if blocks.get(i.id):
            lab.append(f" · {blocks[i.id]} blocked", style="red")
        node = TNode(i.id, lab)
        for gw in raw.gateways:
            if snap.gw_iface.get(gw.id) == i.key:
                gl = Text("◎ ", style=_gw_style(gw.status, gw.loss_pct, gw.rtt_ms))
                gl.append(gw.name)
                if gw.rtt_ms is not None:
                    gl.append(f"  {gw.rtt_ms:.1f}ms", style=_gw_style(gw.status, gw.loss_pct, gw.rtt_ms))
                if gw.loss_pct is not None:
                    gl.append(f" · {gw.loss_pct:.0f}% loss", style="dim")
                gl.append(f" · {GW_WORDS.get(gw.status, gw.status)}", style="dim")
                node.children.append(TNode(gw.id, gl))
        for n in raw.nodes:
            if i.key in snap.placement.get(n.id, []):
                nl = Text("▣ ", style="green" if n.online else "bold red")
                nl.append(n.name, style="deep_sky_blue1")
                nl.append(f" {n.ip}", style="dim")
                if n.online:
                    _pct(nl, "cpu", n.cpu)
                    _pct(nl, "mem", n.mem, 85, 95)
                else:
                    nl.append("  offline", style="bold red")
                node.children.append(TNode(n.id, nl))
        for g in sorted(raw.guests, key=lambda g: g.vmid):
            if i.key in snap.placement.get(g.id, []):
                placed.add(g.id)
                node.children.append(TNode(g.id, guest_label(snap, g, True, False)))
        hosts = snap.hosts.get(i.key, [])
        if hosts:
            names = ", ".join((h.hostname or h.ip) for h in hosts[:4])
            more = f", +{len(hosts) - 4}" if len(hosts) > 4 else ""
            node.children.append(TNode(None, Text(
                f"· {len(hosts)} other host{'s' if len(hosts) != 1 else ''}: {names}{more}", style="dim")))
        root.children.append(node)
    roots = [root]
    unplaced = [g for g in raw.guests if g.id not in placed]
    if unplaced and raw.ifaces:
        u = TNode(None, Text("? guests not seen by the firewall (stopped, or no ARP/VLAN/subnet match)",
                             style="dim"))
        u.children = [TNode(g.id, guest_label(snap, g, True, False))
                      for g in sorted(unplaced, key=lambda g: g.vmid)]
        roots.append(u)
    return roots


def cluster_tree(snap: Snapshot) -> list[TNode]:
    raw = snap.raw
    if not raw.nodes:
        return []
    online = sum(n.online for n in raw.nodes)
    lab = Text("⬢ ", style="deep_sky_blue1")
    lab.append(snap.name("cluster"), style="bold deep_sky_blue1")
    if raw.quorate is not None:
        lab.append("  quorate" if raw.quorate else "  NOT QUORATE",
                   style="green" if raw.quorate else "bold red")
    lab.append(f" · {online}/{len(raw.nodes)} nodes online",
               style="dim" if online == len(raw.nodes) else "bold red")
    running = sum(g.status == "running" for g in raw.guests)
    lab.append(f" · {running}/{len(raw.guests)} guests running", style="dim")
    root = TNode("cluster", lab)
    for n in sorted(raw.nodes, key=lambda n: n.name):
        nl = Text("▣ ", style="green" if n.online else "bold red")
        nl.append(n.name, style="deep_sky_blue1")
        if n.online:
            _pct(nl, "cpu", n.cpu)
            _pct(nl, "mem", n.mem, 85, 95)
            nl.append(f" · up {fmt_uptime(n.uptime)}", style="dim")
        else:
            nl.append("  offline", style="bold red")
        if n.ip:
            nets = [snap.name(f"if:{k}") for k in snap.placement.get(n.id, [])]
            nl.append(f" · {n.ip}" + (f" → {nets[0]}" if nets else ""), style="dim")
        node = TNode(n.id, nl)
        for g in sorted((g for g in raw.guests if g.node == n.name), key=lambda g: g.vmid):
            node.children.append(TNode(g.id, guest_label(snap, g, False, True)))
        for z in (z for z in raw.zpools if z.node == n.name):
            node.children.append(TNode(z.id, zpool_label(z)))
        for st in sorted((st for st in raw.storages if st.node == n.name), key=lambda x: x.name):
            node.children.append(TNode(st.id, storage_label(snap, st)))
        root.children.append(node)
    shared = [st for st in raw.storages if st.node is None]
    if raw.ceph:
        c = raw.ceph
        style = {"HEALTH_OK": "green", "HEALTH_WARN": "yellow"}.get(c.health, "bold red")
        cl = Text("◈ ", style=style)
        cl.append("ceph", style="bold")
        cl.append(f"  {c.health}", style=style)
        if c.osds:
            cl.append(f" · {c.osds_up}/{c.osds} OSDs up", style="dim" if c.osds_up == c.osds else "bold red")
        cl.append(f" · {c.pct:.0f}% raw used", style=pct_style(c.pct, 75, 85))
        if c.pgs_unclean:
            cl.append(f" · {c.pgs_unclean} PGs not clean", style="yellow")
        cnode = TNode("ceph", cl, [TNode(st.id, storage_label(snap, st)) for st in shared
                                   if st.kind in ("rbd", "cephfs")])
        root.children.append(cnode)
        shared = [st for st in shared if st.kind not in ("rbd", "cephfs")]
    root.children += [TNode(st.id, storage_label(snap, st)) for st in shared]
    return [root]


def storage_label(snap: Snapshot, st: Storage) -> Text:
    t = Text("▤ ", style="green" if st.active else "bold red")
    t.append(st.name, style="" if st.active else "bold red")
    t.append(f" {st.kind}", style="dim")
    if not st.active:
        t.append("  UNAVAILABLE", style="bold red")
        return t
    t.append(f"  {st.pct:.0f}%", style=pct_style(st.pct, 80, 90))
    t.append(f" of {st.total / GIB:,.0f} GiB", style="dim")
    n = len(snap.guests_on(st.id))
    if n:
        t.append(f" · {n} guest{'s' if n != 1 else ''}", style="dim")
    return t


def zpool_label(z: ZPool) -> Text:
    ok = z.health.upper() == "ONLINE"
    t = Text("◍ ", style="green" if ok else "bold red")
    t.append(f"zpool {z.name}")
    t.append(f"  {z.health}", style="green" if ok else "bold red")
    t.append(f" · {z.pct:.0f}%", style=pct_style(z.pct, 80, 90))
    t.append(f" · frag {z.frag:.0f}%", style="dim" if z.frag < 60 else "yellow")
    return t


def legend(view: str) -> Text:
    t = Text(style="dim")
    if view == "network":
        t.append("◆ firewall  ⇄ interface  ◎ gateway  ▣ PVE node  ▪ VM  ▫ CT   ")
    else:
        t.append("⬢ cluster  ▣ node  ▪ VM  ▫ CT  ◍ ZFS pool  ▤ storage  ◈ Ceph   ")
    t.append("─ ", style="bold magenta")
    t.append("path to selection")
    return t


# --- inspector -----------------------------------------------------------------
def _is_internet(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _fw_summary(snap: Snapshot, events: list[Event], eid: str | None) -> Text:
    fw = [e for e in events if e.source == "fw"]
    t = Text()
    if not fw:
        t.append("  no firewall log entries in memory\n", style="dim")
        return t
    passed = sum(e.level == "INFO" for e in fw)
    t.append(f"  {passed} passed", style="green")
    t.append(" · ")
    t.append(f"{len(fw) - passed} blocked\n", style="red" if len(fw) > passed else "dim")
    blocked: Counter = Counter()
    peers: Counter = Counter()
    for e in fw:
        f: FwEntry = e.ref
        src_owner = snap.ip_owner.get(f.src)
        if e.level != "INFO":
            who = snap.name(src_owner) if src_owner else f.src
            blocked[f"{f.proto}/{f.dport or '-'} from {who}"] += 1
        else:
            mine_src = eid is not None and src_owner == eid
            other_ip = f.dst if mine_src else f.src
            other = snap.ip_owner.get(other_ip)
            if other == eid:
                continue
            peers["internet" if not other and _is_internet(other_ip)
                  else (snap.name(other) if other else other_ip)] += 1
    if blocked:
        t.append("  top blocked: ", style="dim")
        t.append(", ".join(f"{k} ×{v}" for k, v in blocked.most_common(4)) + "\n", style="red")
    if peers:
        t.append("  talks to:    ", style="dim")
        t.append(", ".join(f"{k} ×{v}" for k, v in peers.most_common(5)) + "\n")
    return t


def _tasks(events: list[Event]) -> Text:
    t = Text()
    tasks = [e for e in events if e.source == "task"][:6]
    for e in tasks:
        task: Task = e.ref
        style = {"ERROR": "red", "WARN": "yellow"}.get(e.level, "")
        t.append(f"  {datetime.fromtimestamp(task.start):%H:%M:%S} ", style="dim")
        t.append(e.message + "\n", style=style)
    return t or Text("  none in the recent task list\n", style="dim")


def _issue_lines(issues: list[Issue], show_entity: bool, snap: Snapshot) -> Text:
    t = Text()
    now = time.time()
    for i in issues:
        mark, style = SEV_MARK[i.severity]
        t.append(f" {mark} ", style=style)
        t.append(i.title, style=style)
        if show_entity and i.entity:
            t.append(f"  [{snap.name(i.entity)}]", style="dim")
        age = now - i.since
        t.append(f"  for {int(age)}s\n" if age < 60 else f"  for {fmt_uptime(age)}\n", style="dim")
        if i.detail:
            t.append(f"     {i.detail}\n", style="dim")
    return t


def attention(snap: Snapshot) -> Text:
    t = Text()
    crit = sum(i.severity == "crit" for i in snap.issues)
    warn = len(snap.issues) - crit
    if not snap.issues:
        t.append("✓ Nothing needs attention.\n\n", style="bold green")
    else:
        t.append("Attention  ", style="bold")
        t.append(f"{crit} critical", style="bold red" if crit else "dim")
        t.append(" · ")
        t.append(f"{warn} warning{'s' if warn != 1 else ''}\n", style="yellow" if warn else "dim")
        t.append_text(_issue_lines(snap.issues, True, snap))
        t.append("\n")
    t.append("n jumps to the next issue · Enter on a guest or event inspects it · s/S cycles everything\n"
             "v switches network ↔ cluster view · x opens actions for the selection or a firewall event\n",
             style="dim")
    return t


def inspector(snap: Snapshot, eid: str | None, task_log: tuple[str, list[str]] | None) -> Text:
    t = Text()
    if not eid:
        return attention(snap)
    mine = [i for i in snap.issues if i.entity == eid]
    if mine:
        t.append_text(_issue_lines(mine, False, snap))
    events = snap.events_for(eid)
    if eid.startswith("guest:") and (g := snap.guest(eid)):
        t.append(f"{GUEST_ICON.get(g.kind, '▪')} {g.label}", style="bold")
        t.append(f"  ({'VM' if g.kind == 'qemu' else 'container'}) {g.status} on {g.node}")
        if g.uptime:
            t.append(f" · up {fmt_uptime(g.uptime)}", style="dim")
        t.append("\n")
        hist = snap.guest_cpu.get(eid, [])
        t.append("  cpu ", style="dim")
        t.append(sparkline(hist, 24, 100.0), style="deep_sky_blue1")
        t.append(f" {g.cpu:.0f}%", style=pct_style(g.cpu))
        t.append(f"   mem {g.mem:.0f}% of {g.maxmem / 2**30:.1f} GiB", style=pct_style(g.mem, 85, 95))
        t.append(f"   net ↓{fmt_bps(g.net_in)} ↑{fmt_bps(g.net_out)}\n", style="dim")
        t.append("Interfaces\n", style="bold")
        arp_ips = set(snap.guest_ips.get(eid, []))
        for nic in g.nics:
            t.append(f"  {nic.name} {nic.mac} {nic.bridge}", style="dim")
            if nic.vlan is not None:
                t.append(f" tag {nic.vlan}", style="dim")
            ips = list(dict.fromkeys(nic.ips + [ip for ip in arp_ips
                                                if any(a.ip == ip and a.mac.upper() == nic.mac.upper()
                                                       for a in snap.raw.arp)]))
            nets = [snap.name(f"if:{k}") for k in snap.placement.get(eid, [])]
            t.append(f"  {', '.join(ips) or 'no IP seen'}")
            t.append(f"  → {', '.join(nets) or 'unknown network'}\n", style="cyan")
        if g.disks:
            t.append("Disks\n", style="bold")
            for name, size in g.disks:
                st = snap.storage_of(g, name)
                t.append(f"  {name} {size}", style="")
                if st:
                    t.append(f"  ({st.kind}, {st.pct:.0f}% used)", style=pct_style(st.pct, 80, 90))
                t.append("\n")
        for r in (r for r in snap.raw.replication if r.vmid == g.vmid):
            ago = fmt_uptime(time.time() - r.last_sync) + " ago" if r.last_sync else "never"
            t.append(f"  replication {r.job} → {r.target} · last sync {ago}",
                     style="red" if r.fail_count else "dim")
            t.append(f" · {r.fail_count} failures\n" if r.fail_count else " · OK\n",
                     style="red" if r.fail_count else "green")
    elif eid.startswith("node:") and (n := snap.node(eid)):
        guests = [g for g in snap.raw.guests if g.node == n.name]
        t.append(f"▣ {n.name}", style="bold")
        t.append(f"  {'online' if n.online else 'OFFLINE'} · {n.ip or 'no ip'}"
                 f" · up {fmt_uptime(n.uptime)}\n", style="" if n.online else "bold red")
        t.append(f"  cpu {n.cpu:.0f}%", style=pct_style(n.cpu))
        t.append(f"   mem {n.mem:.0f}%", style=pct_style(n.mem, 85, 95))
        t.append(f"   guests {sum(g.status == 'running' for g in guests)}/{len(guests)} running\n",
                 style="dim")
        top = sorted((g for g in guests if g.status == "running"), key=lambda g: -g.cpu)[:4]
        if top:
            t.append("  busiest: " + ", ".join(f"{g.name} {g.cpu:.0f}%" for g in top) + "\n",
                     style="dim")
    elif eid.startswith("if:") and (i := snap.iface(eid)):
        t.append(f"⇄ {i.name}", style="bold cyan")
        t.append(f"  {i.device} · {i.cidr or 'no address'}"
                 + (f" · vlan {i.vlan}" if i.vlan is not None else "") + "\n")
        t.append(f"  ↓{fmt_bps(i.rx_bps)}  ↑{fmt_bps(i.tx_bps)}\n", style="dim")
        members = [snap.name(e) for e, keys in snap.placement.items()
                   if i.key in keys and not e.startswith("gw:")]
        t.append(f"  {len(members)} known hosts: ", style="dim")
        t.append(", ".join(members[:12]) + (" …" if len(members) > 12 else "") + "\n")
    elif eid.startswith("gw:") and (gw := snap.gateway(eid)):
        style = _gw_style(gw.status, gw.loss_pct, gw.rtt_ms)
        t.append(f"◎ {gw.name}", style="bold")
        t.append(f"  {GW_WORDS.get(gw.status, gw.status)}", style=style)
        t.append(f" · {gw.address or ''} · rtt {gw.rtt_ms if gw.rtt_ms is not None else '?'}ms"
                 f" · loss {gw.loss_pct if gw.loss_pct is not None else '?'}%"
                 f" · via {snap.name('if:' + (snap.gw_iface.get(eid) or ''))}\n")
    elif eid == "fw" and snap.raw.fw:
        fw = snap.raw.fw
        t.append(f"◆ {fw.name}", style="bold orange1")
        t.append(f"  cpu {fw.cpu or 0:.0f}% · mem {fw.mem or 0:.0f}%"
                 + (f" · {fw.states:,} states" if fw.states is not None else "") + "\n")
    elif eid.startswith("st:") and (st := snap.storage(eid)):
        t.append(f"▤ {st.label}", style="bold")
        t.append(f"  {st.kind} · {'shared' if st.node is None else 'local to ' + st.node}\n", style="dim")
        t.append(f"  {st.pct:.0f}% used", style=pct_style(st.pct, 80, 90))
        t.append(f" · {st.used / GIB:,.0f} of {st.total / GIB:,.0f} GiB · "
                 f"{(st.total - st.used) / GIB:,.0f} GiB free\n", style="dim")
        guests = snap.guests_on(eid)
        t.append(f"  {len(guests)} guests with disks here: ", style="dim")
        t.append(", ".join(f"{g.name} ({', '.join(sz for n, sz in g.disks if n == st.name)})"
                           for g in guests) + "\n")
    elif eid.startswith("zpool:") and (z := snap.zpool(eid)):
        t.append_text(zpool_label(z))
        t.append(f"  on {z.node}\n", style="dim")
        t.append(f"  {z.alloc / GIB:,.0f} of {z.size / GIB:,.0f} GiB allocated\n", style="dim")
        if z.health.upper() != "ONLINE":
            t.append(f"  Run `zpool status {z.name}` on {z.node} to see which device failed.\n", style="yellow")
    elif eid == "ceph" and snap.raw.ceph:
        c = snap.raw.ceph
        t.append("◈ ceph", style="bold")
        t.append(f"  {c.health}\n", style={"HEALTH_OK": "green", "HEALTH_WARN": "yellow"}.get(c.health, "bold red"))
        for sev, msg in c.checks:
            t.append(f"  {msg}\n", style="red" if sev == "HEALTH_ERR" else "yellow")
        t.append(f"  OSDs {c.osds_up}/{c.osds} up, {c.osds_in} in · PGs {c.pgs - c.pgs_unclean}/{c.pgs} clean"
                 f" · {c.used / GIB:,.0f} of {c.total / GIB:,.0f} GiB raw used\n", style="dim")
        for st in (st for st in snap.raw.storages if st.node is None and st.kind in ("rbd", "cephfs")):
            names = ", ".join(g.name for g in snap.guests_on(st.id))
            t.append(f"  pool {st.name}: {st.pct:.0f}% · {names or 'no guests'}\n")
    elif eid.startswith("host:"):
        ip = eid[5:]
        arp = next((a for a in snap.raw.arp if a.ip == ip), None)
        t.append(f"· {snap.name(eid)}", style="bold")
        t.append(f"  {ip} · {arp.mac if arp else ''} · on "
                 f"{snap.name('if:' + snap.placement.get(eid, ['?'])[0])}\n", style="dim")
    else:
        t.append(f"{eid} is no longer present.\n", style="dim")

    if eid.startswith(("st:", "zpool:", "ceph")):
        if events:
            t.append("Related events\n", style="bold")
            for e in events[:6]:
                t.append(f"  {datetime.fromtimestamp(e.ts):%H:%M:%S} {e.message[:100]}\n",
                         style={"ERROR": "red", "WARN": "yellow"}.get(e.level, "dim"))
        return t
    t.append("Firewall\n", style="bold")
    t.append_text(_fw_summary(snap, events, None if eid in ("fw",) or eid.startswith(("if:", "gw:"))
                              else eid))
    if eid.startswith(("guest:", "node:")):
        t.append("Recent tasks\n", style="bold")
        t.append_text(_tasks(events))
    if task_log:
        upid, lines = task_log
        t.append(f"Task log {upid[:40]}…\n", style="bold")
        for line in lines[-15:]:
            t.append(f"  {line}\n", style="dim")
    return t
