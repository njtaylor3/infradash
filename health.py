"""Attention rules. Each rule looks at the current snapshot (plus a little history) and
returns problems worth a human's attention, most severe first."""
from __future__ import annotations

import time
from collections import Counter

from .correlate import Snapshot
from .models import Issue, fmt_uptime

GIB = 2**30
BURST_WINDOW_S = 300
BURST_MIN_HITS = 60


def _sustained(values: list[float], threshold: float, n: int = 3) -> bool:
    return len(values) >= n and all(v >= threshold for v in values[-n:])


def find_issues(snap: Snapshot, cpu_hist: dict[str, list[float]]) -> list[Issue]:
    raw, out = snap.raw, []

    def add(sev, entity, title, detail="", key=None):
        out.append(Issue(sev, entity, title, detail, key or f"{entity}:{title}"))

    # Cluster and nodes
    if raw.quorate is False:
        add("crit", "cluster", "Cluster has lost quorum",
            "Guests can't be started, stopped or migrated until quorum returns.")
    for n in raw.nodes:
        if not n.online:
            add("crit", n.id, f"Node {n.name} is offline")
            continue
        hist = cpu_hist.get(n.name, [])
        if _sustained(hist, 90):
            add("crit", n.id, f"{n.name} CPU above 90%", f"now {n.cpu:.0f}%", key=f"{n.id}:cpu")
        elif _sustained(hist, 75):
            add("warn", n.id, f"{n.name} CPU above 75%", f"now {n.cpu:.0f}%", key=f"{n.id}:cpu")
        if n.mem >= 92:
            add("crit", n.id, f"{n.name} memory at {n.mem:.0f}%", key=f"{n.id}:mem")
        elif n.mem >= 85:
            add("warn", n.id, f"{n.name} memory at {n.mem:.0f}%", key=f"{n.id}:mem")

    for g in raw.guests:
        if g.status != "running":
            continue
        if _sustained(snap.guest_cpu.get(g.id, []), 90, 5):
            add("warn", g.id, f"{g.name} CPU pegged", f"{g.cpu:.0f}% for several polls", key=f"{g.id}:cpu")
        if g.mem >= 95:
            add("warn", g.id, f"{g.name} memory at {g.mem:.0f}%", key=f"{g.id}:mem")

    # Storage
    for s in raw.storages:
        if not s.active:
            add("crit", s.id, f"Storage {s.label} is unavailable")
        elif s.pct >= 90:
            add("crit", s.id, f"Storage {s.label} {s.pct:.0f}% full",
                f"{(s.total - s.used) / GIB:.0f} GiB free", key=f"{s.id}:full")
        elif s.pct >= 80:
            add("warn", s.id, f"Storage {s.label} {s.pct:.0f}% full",
                f"{(s.total - s.used) / GIB:.0f} GiB free", key=f"{s.id}:full")
    for z in raw.zpools:
        if z.health.upper() != "ONLINE":
            add("crit", z.id, f"ZFS pool {z.name} on {z.node} is {z.health}",
                "A disk has failed or is missing — check `zpool status` on the node.", key=f"{z.id}:health")
        if z.pct >= 80:
            add("warn", z.id, f"ZFS pool {z.name} on {z.node} {z.pct:.0f}% allocated",
                "ZFS slows down noticeably above ~80%.", key=f"{z.id}:full")
        if z.frag >= 60:
            add("warn", z.id, f"ZFS pool {z.name} on {z.node} {z.frag:.0f}% fragmented", key=f"{z.id}:frag")
    if raw.ceph:
        c = raw.ceph
        if c.health != "HEALTH_OK":
            msgs = "; ".join(m for _, m in c.checks[:3]) or c.health
            add("crit" if c.health == "HEALTH_ERR" else "warn", "ceph", f"Ceph {c.health}", msgs,
                key="ceph:health")
        if c.osds and c.osds_up < c.osds:
            add("crit", "ceph", f"{c.osds - c.osds_up} of {c.osds} Ceph OSDs down", key="ceph:osd")
        if c.pct >= 75:
            add("crit" if c.pct >= 85 else "warn", "ceph", f"Ceph {c.pct:.0f}% used",
                "Ceph stops writes when an OSD reaches its full ratio.", key="ceph:full")
    now = time.time()
    for r in raw.replication:
        gid = f"guest:{r.vmid}"
        if r.fail_count:
            add("crit" if r.fail_count >= 3 else "warn", gid,
                f"Replication {r.job} ({snap.name(gid)}) failing", f"{r.fail_count}× — {r.error[:100]}",
                key=f"repl:{r.job}")
        elif r.last_sync and now - r.last_sync > 6 * 3600:
            add("warn", gid, f"Replication {r.job} ({snap.name(gid)}) stale",
                f"last sync {fmt_uptime(now - r.last_sync)} ago", key=f"repl:{r.job}")

    # Firewall and network
    if raw.fw and raw.fw.states and raw.fw.states_limit:
        pct = 100 * raw.fw.states / raw.fw.states_limit
        if pct >= 80:
            add("crit" if pct >= 95 else "warn", "fw", f"Firewall state table {pct:.0f}% full")
    for i in raw.ifaces:
        if not i.up:
            add("crit", i.id, f"Interface {i.name} is down")
    for gw in raw.gateways:
        if gw.status == "down":
            add("crit", gw.id, f"Gateway {gw.name} is down", key=f"{gw.id}:state")
        elif (gw.loss_pct or 0) >= 5:
            add("crit" if (gw.loss_pct or 0) >= 20 else "warn", gw.id,
                f"Gateway {gw.name} losing {gw.loss_pct:.0f}% of packets", key=f"{gw.id}:state")
        elif (gw.rtt_ms or 0) >= 150:
            add("warn", gw.id, f"Gateway {gw.name} latency {gw.rtt_ms:.0f}ms", key=f"{gw.id}:state")

    # Repeated blocked sources (a scanner or brute-force attempt)
    hits: Counter = Counter()
    where: dict[str, str | None] = {}
    for e in snap.events:
        if e.source != "fw" or e.level == "INFO" or e.ts < now - BURST_WINDOW_S:
            continue
        hits[e.ref.src] += 1
        where[e.ref.src] = e.iface
    for ip, n in hits.most_common(3):
        if n >= BURST_MIN_HITS:
            owner = snap.ip_owner.get(ip)
            who = snap.name(owner) if owner else ip
            add("warn", owner or where[ip], f"{who} blocked {n}× in 5 min",
                f"on {snap.name(where[ip]) or '?'} — select a matching event and press x to block or kill states",
                key=f"burst:{ip}")

    # Failed tasks in the recent task list
    for t in raw.tasks:
        if t.status not in ("running", "OK") and not t.status.startswith("WARNINGS") and \
                t.end and now - t.end < 24 * 3600:
            ent = f"guest:{t.vmid}" if t.vmid and f"guest:{t.vmid}" in snap.names else f"node:{t.node}"
            add("warn", ent, f"{t.type} failed on {t.node}", t.status[:100], key=f"task:{t.upid}")

    out.sort(key=lambda i: (i.severity != "crit", i.title))
    return out
