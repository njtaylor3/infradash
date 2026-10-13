"""Join the two systems: which guest owns which IP, which firewall network each guest
and node sits on, and which entities every log line / task touches."""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import (ArpEntry, Event, FwEntry, Gateway, Guest, Iface, Issue, MetricPanel, Node,
                     PveLog, Raw, Storage, Task, ZPool)

VMID_RE = re.compile(r"\b(?:VM|CT|vmid|guest)\s*(\d{3,9})\b|UPID:[^:]+:[^:]+:[^:]+:[^:]+:[^:]+:(\d+):")


def _ip_in(ip: str, net) -> bool:
    try:
        return net is not None and ipaddress.ip_address(ip) in net
    except ValueError:
        return False


@dataclass
class Snapshot:
    raw: Raw
    metrics: list[MetricPanel]
    events: list[Event]                           # newest first
    ip_owner: dict[str, str] = field(default_factory=dict)
    placement: dict[str, list[str]] = field(default_factory=dict)  # entity -> iface keys
    names: dict[str, str] = field(default_factory=dict)
    guest_ips: dict[str, list[str]] = field(default_factory=dict)
    hosts: dict[str, list[ArpEntry]] = field(default_factory=dict)  # iface key -> other hosts
    gw_iface: dict[str, str | None] = field(default_factory=dict)
    guest_cpu: dict[str, list[float]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    # --- lookups -------------------------------------------------------------
    def node(self, eid: str) -> Node | None:
        return next((n for n in self.raw.nodes if n.id == eid), None)

    def guest(self, eid: str) -> Guest | None:
        return next((g for g in self.raw.guests if g.id == eid), None)

    def iface(self, eid: str) -> Iface | None:
        return next((i for i in self.raw.ifaces if i.id == eid), None)

    def gateway(self, eid: str) -> Gateway | None:
        return next((g for g in self.raw.gateways if g.id == eid), None)

    def storage(self, eid: str) -> Storage | None:
        return next((s for s in self.raw.storages if s.id == eid), None)

    def zpool(self, eid: str) -> ZPool | None:
        return next((z for z in self.raw.zpools if z.id == eid), None)

    def guests_on(self, eid: str) -> list[Guest]:
        """Guests with at least one disk on storage eid."""
        st = self.storage(eid)
        if not st:
            return []
        return [g for g in self.raw.guests
                if any(name == st.name and (st.node is None or st.node == g.node) for name, _ in g.disks)]

    def storage_of(self, g: Guest, name: str) -> Storage | None:
        return next((s for s in self.raw.storages
                     if s.name == name and (s.node is None or s.node == g.node)), None)

    def name(self, eid: str | None) -> str:
        return self.names.get(eid, eid or "") if eid else ""

    def entity_order(self) -> list[str]:
        out = ["fw"] if self.raw.fw else []
        out += [i.id for i in self.raw.ifaces] + [g.id for g in self.raw.gateways]
        out += [n.id for n in self.raw.nodes]
        out += [g.id for g in sorted(self.raw.guests, key=lambda g: g.vmid)]
        out += (["ceph"] if self.raw.ceph else []) + [z.id for z in self.raw.zpools]
        out += [st.id for st in self.raw.storages]
        return out

    def related(self, eid: str | None) -> set[str]:
        """Entities on the path from the roots of both trees to eid."""
        if not eid:
            return set()
        out = {eid}
        if self.raw.fw:
            out.add("fw")
        out.update(f"if:{k}" for k in self.placement.get(eid, []))
        if eid.startswith("guest:"):
            g = self.guest(eid)
            if g:
                out.update({"cluster", f"node:{g.node}"})
        elif eid.startswith("node:"):
            out.add("cluster")
        elif eid.startswith("st:"):
            st = self.storage(eid)
            out.add("cluster")
            if st and st.node:
                out.add(f"node:{st.node}")
            elif st and st.kind in ("rbd", "cephfs") and self.raw.ceph:
                out.add("ceph")
            out.update(g.id for g in self.guests_on(eid))
        elif eid.startswith("zpool:"):
            out.update({"cluster", f"node:{eid[6:].split('/')[0]}"})
        elif eid == "ceph":
            out.add("cluster")
        elif eid.startswith("gw:") and self.gw_iface.get(eid):
            out.add(f"if:{self.gw_iface[eid]}")
        return out

    def events_for(self, eid: str | None) -> list[Event]:
        if not eid:
            return self.events
        if eid == "fw":
            return [e for e in self.events if e.source == "fw"]
        if eid.startswith("if:"):
            return [e for e in self.events if e.iface == eid]
        if eid.startswith("gw:"):
            key = self.gw_iface.get(eid)
            return [e for e in self.events if key and e.iface == f"if:{key}"]
        if eid.startswith("st:"):
            ids = {eid} | {g.id for g in self.guests_on(eid)}
            return [e for e in self.events if e.source != "fw" and ids & set(e.entities)]
        if eid.startswith("zpool:"):
            node = f"node:{eid[6:].split('/')[0]}"
            return [e for e in self.events if eid in e.entities or
                    (node in e.entities and ("zfs" in e.message.lower() or "zpool" in e.message.lower()))]
        if eid == "ceph":
            return [e for e in self.events if "ceph" in e.entities or "ceph" in e.message.lower()]
        return [e for e in self.events if eid in e.entities]


def build_snapshot(raw: Raw, fw_entries: list[FwEntry], pve_logs: list[PveLog],
                   metrics: list[MetricPanel], guest_cpu: dict[str, list[float]],
                   extra_events: list[Event] | None = None) -> Snapshot:
    snap = Snapshot(raw, metrics, [], guest_cpu=guest_cpu)
    dev_to_if = {i.device: i.key for i in raw.ifaces}
    nets = {i.key: i.network for i in raw.ifaces}

    # Names
    snap.names["fw"] = raw.fw.name if raw.fw else "firewall"
    snap.names["cluster"] = raw.cluster or "cluster"
    for i in raw.ifaces:
        snap.names[i.id] = i.name
    for g in raw.gateways:
        snap.names[g.id] = g.name
    for n in raw.nodes:
        snap.names[n.id] = n.name
    for g in raw.guests:
        snap.names[g.id] = g.name
    for st in raw.storages:
        snap.names[st.id] = st.label
    for z in raw.zpools:
        snap.names[z.id] = f"{z.node}/{z.name}"
    if raw.ceph:
        snap.names["ceph"] = "ceph"

    # Who owns which IP / MAC
    ip_owner: dict[str, str] = {}
    for i in raw.ifaces:
        if i.address:
            ip_owner[i.address] = "fw"
    for n in raw.nodes:
        if n.ip:
            ip_owner[n.ip] = n.id
    mac_owner: dict[str, str] = {}
    guest_ips: dict[str, list[str]] = defaultdict(list)
    for g in raw.guests:
        for nic in g.nics:
            mac_owner[nic.mac.upper()] = g.id
            for ip in nic.ips:
                ip_owner[ip] = g.id
                guest_ips[g.id].append(ip)
    arp_by_mac: dict[str, list[ArpEntry]] = defaultdict(list)
    for a in raw.arp:
        mac = a.mac.upper()
        arp_by_mac[mac].append(a)
        owner = mac_owner.get(mac)
        if owner:
            ip_owner.setdefault(a.ip, owner)
            if a.ip not in guest_ips[owner]:
                guest_ips[owner].append(a.ip)
        elif a.ip not in ip_owner:
            ip_owner[a.ip] = f"host:{a.ip}"
            snap.names[f"host:{a.ip}"] = a.hostname or a.ip
            key = dev_to_if.get(a.device)
            if key:
                snap.hosts.setdefault(key, []).append(a)
                snap.placement[f"host:{a.ip}"] = [key]
    snap.ip_owner, snap.guest_ips = ip_owner, dict(guest_ips)

    # Which firewall network each guest NIC / node is on: ARP > VLAN tag > subnet.
    vlan_to_if = {i.vlan: i.key for i in raw.ifaces if i.vlan is not None}
    for g in raw.guests:
        keys: list[str] = []
        for nic in g.nics:
            key = next((dev_to_if[a.device] for a in arp_by_mac.get(nic.mac.upper(), [])
                        if a.device in dev_to_if), None)
            if key is None and nic.vlan is not None:
                key = vlan_to_if.get(nic.vlan)
            if key is None:
                key = next((k for ip in nic.ips for k, net in nets.items() if _ip_in(ip, net)), None)
            if key and key not in keys:
                keys.append(key)
        snap.placement[g.id] = keys
    for n in raw.nodes:
        if n.ip:
            snap.placement[n.id] = [k for k, net in nets.items() if _ip_in(n.ip, net)][:1]
    for gw in raw.gateways:
        key = next((k for k, net in nets.items() if gw.address and _ip_in(gw.address, net)), None)
        if key is None:
            key = next((i.key for i in raw.ifaces if i.key.startswith("wan")), None)
        snap.gw_iface[gw.id] = key
        snap.placement[gw.id] = [key] if key else []

    snap.events = _events(snap, fw_entries, pve_logs, dev_to_if)
    if extra_events:
        snap.events = sorted(snap.events + extra_events, key=lambda e: e.ts, reverse=True)
    return snap


def _endpoint(snap: Snapshot, ip: str, port: str) -> str:
    owner = snap.ip_owner.get(ip)
    who = snap.names.get(owner, ip) if owner and owner != "fw" else ip
    if owner == "fw":
        who = f"{snap.names['fw']}({ip})"
    return f"{who}:{port}" if port else who


def _events(snap: Snapshot, fw_entries, pve_logs, dev_to_if) -> list[Event]:
    events: list[Event] = []
    for f in fw_entries:
        ents = [snap.ip_owner.get(ip) for ip in (f.src, f.dst)]
        ents = [e for e in ents if e]
        ents.sort(key=lambda e: e == "fw")         # real hosts before the firewall itself
        key = dev_to_if.get(f.device)
        iface_name = snap.names.get(f"if:{key}", f.device) if key else f.device
        msg = (f"{f.action.upper():<6} {f.direction or ''} {iface_name} {f.proto} "
               f"{_endpoint(snap, f.src, f.sport)} → {_endpoint(snap, f.dst, f.dport)}")
        if f.label:
            msg += f"  · {f.label}"
        level = "INFO" if f.action == "pass" else "WARN"
        events.append(Event(f.ts, "fw", level, msg, ents, f"if:{key}" if key else None, f))

    for t in snap.raw.tasks:
        ents = [f"guest:{t.vmid}"] if t.vmid and f"guest:{t.vmid}" in snap.names else []
        ents.append(f"node:{t.node}")
        if t.status == "running":
            level, state = "INFO", "running…"
        elif t.status == "OK":
            level, state = "INFO", "OK"
        elif t.status.startswith("WARNINGS"):
            level, state = "WARN", t.status
        else:
            level, state = "ERROR", t.status
        dur = f" in {int((t.end or t.start) - t.start)}s" if t.end else ""
        target = f" {t.vmid} ({snap.name(ents[0])})" if t.vmid and len(ents) > 1 else (
            f" {t.vmid}" if t.vmid else "")
        msg = f"{t.type}{target} on {t.node} by {t.user}: {state}{dur}"
        events.append(Event(t.end or t.start, "task", level, msg, ents, None, t))

    for p in pve_logs:
        ents = []
        m = VMID_RE.search(p.msg)
        vmid = m and (m.group(1) or m.group(2))
        if vmid and f"guest:{vmid}" in snap.names:
            ents.append(f"guest:{vmid}")
        ents.append(f"node:{p.node}")
        events.append(Event(p.ts, "pve", p.level, f"{p.tag}: {p.msg}", ents, None, p))

    events.sort(key=lambda e: e.ts, reverse=True)
    return events
