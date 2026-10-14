"""A simulated homelab so the dashboard can run without real hardware.

OPNsense with WAN + four networks, a three-node Proxmox cluster with ten guests, and a few
recurring situations: nightly-style backups that load pve2/pve3 (one in three fails), a VM
that live-migrates between nodes, SSH brute-force bursts on WAN, an IoT camera that keeps
trying SMB into the server VLAN, and an ISP gateway that degrades now and then.

Guest IPs for VMs are deliberately *not* in their config (no guest agent), so the dashboard
has to find them through the firewall's ARP table — exactly as it does on real systems.
"""
from __future__ import annotations

import math
import random
import time

from .models import (ArpEntry, Ceph, FwEntry, FwStats, Gateway, Guest, Iface, Nic, Node, PveLog, Raw,
                     Replication, Storage, Task, ZPool)

WAN_IP = "203.0.113.7"
IFACES = [
    ("wan", "WAN", "vtnet0", f"{WAN_IP}/24", None),
    ("lan", "LAN", "vtnet1", "10.0.10.1/24", None),
    ("opt1", "SERVERS", "vlan0.20", "10.0.20.1/24", 20),
    ("opt2", "IOT", "vlan0.30", "10.0.30.1/24", 30),
    ("opt3", "DMZ", "vlan0.40", "10.0.40.1/24", 40),
]
NODES = [("pve1", "10.0.10.11"), ("pve2", "10.0.10.12"), ("pve3", "10.0.10.13")]
# vmid, name, kind, node, cores, mem GiB, base cpu %, [(vlan, ip)], status
GUESTS = [
    (101, "web01", "qemu", "pve1", 2, 4, 9, [(40, "10.0.40.11")], "running"),
    (102, "db01", "qemu", "pve2", 4, 16, 14, [(20, "10.0.20.12")], "running"),
    (103, "nextcloud", "qemu", "pve2", 4, 8, 11, [(20, "10.0.20.13")], "running"),
    (104, "gitea", "qemu", "pve1", 2, 4, 4, [(20, "10.0.20.14")], "running"),
    (105, "win11", "qemu", "pve3", 4, 8, 0, [(None, "10.0.10.50")], "stopped"),
    (106, "pbs", "qemu", "pve3", 4, 8, 3, [(None, "10.0.10.20")], "running"),
    (107, "homeassistant", "qemu", "pve3", 2, 4, 6, [(30, "10.0.30.10"), (20, "10.0.20.30")], "running"),
    (201, "pihole", "lxc", "pve1", 1, 1, 3, [(20, "10.0.20.53")], "running"),
    (202, "nginx-proxy", "lxc", "pve2", 2, 1, 5, [(40, "10.0.40.10")], "running"),
    (203, "grafana", "lxc", "pve2", 2, 2, 4, [(20, "10.0.20.40")], "running"),
]
OTHER_HOSTS = [
    ("10.0.10.100", "vtnet1", "desktop-anna"), ("10.0.10.101", "vtnet1", "pixel-8"),
    ("10.0.30.21", "vlan0.30", "cam-frontdoor"), ("10.0.30.22", "vlan0.30", "cam-garage"),
    ("10.0.30.40", "vlan0.30", "plug-livingroom"), ("10.0.30.41", "vlan0.30", "lg-tv"),
]
VLAN_DEV = {None: "vtnet1", 20: "vlan0.20", 30: "vlan0.30", 40: "vlan0.40"}
DISKS = {101: [("local-zfs", "32G")], 102: [("ceph-vm", "64G")],
         103: [("ceph-vm", "32G"), ("ceph-vm", "500G")], 104: [("local-zfs", "20G")],
         105: [("local-zfs", "80G")], 106: [("local-zfs", "16G"), ("tank-data", "2T")],
         107: [("local-zfs", "32G")], 201: [("local-zfs", "8G")], 202: [("local-zfs", "4G")],
         203: [("ceph-vm", "8G")]}
TB = 1e12


def _mac(i: int) -> str:
    return "BC:24:11:%02X:%02X:%02X" % (0x10 + i, (i * 37) % 256, (i * 91) % 256)


def _upid(node, typ, vmid, user, start):
    return (f"UPID:{node}:{random.randint(0x1000, 0xFFFFF):08X}:{random.randint(0, 0xFFFFFFF):08X}:"
            f"{int(start):08X}:{typ}:{vmid or ''}:{user}:")


class DemoProvider:
    name = "demo"
    actions_enabled = True
    block_alias = "infradash_block"
    action_log = None
    has_pve = has_opn = True

    def __init__(self) -> None:
        self.t0 = time.time() - 30
        self.last = self.t0
        self.guests: list[Guest] = []
        self.meta: dict[int, tuple] = {}
        for idx, (vmid, name, kind, node, cores, mem, base, nets, status) in enumerate(GUESTS):
            nics = []
            for n, (vlan, ip) in enumerate(nets):
                # LXC static IPs are in the config; VM IPs must be discovered via ARP.
                nics.append(Nic(f"net{n}", _mac(idx * 4 + n), "vmbr0", vlan,
                                [ip] if kind == "lxc" else []))
            self.guests.append(Guest(vmid, name, kind, node, status, maxmem=mem * 2**30,
                                     uptime=random.randint(2, 40) * 86400, nics=nics,
                                     disks=list(DISKS.get(vmid, []))))
            self.meta[vmid] = (cores, base, nets)
        self.tasks: list[Task] = []
        self.task_logs: dict[str, list[str]] = {}
        self.pve_log: list[PveLog] = []
        self.fw_log: list[FwEntry] = []
        self.backup: dict | None = None
        self.migration: dict | None = None
        self.backup_runs = 0
        self._seq = 0
        self.pending: list[tuple[float, object]] = []     # (when, callback) for simulated actions
        self.blocked: set[str] = set()
        self.states_offset = 0
        self.repl = {"last": self.t0, "last_try": self.t0, "fails": 0, "error": ""}
        self.zfs_fault_logged = self.osd_down_logged = False
        # a little history so the task list isn't empty at start
        for i, (node, typ, vmid) in enumerate([("pve1", "vncproxy", "104"), ("pve3", "aptupdate", None),
                                               ("pve2", "qmstart", "102")]):
            st = self.t0 - 600 + i * 120
            self._add_task(node, typ, vmid, "root@pam", st, st + random.randint(2, 30), "OK",
                           [f"{typ} finished"])

    # --- helpers -------------------------------------------------------------
    def _key(self) -> str:
        self._seq += 1
        return f"d{self._seq}"

    def _log(self, ts, node, level, tag, msg) -> None:
        self.pve_log.append(PveLog(ts, node, level, tag, msg, self._key()))

    def _fw(self, ts, action, dev, src, sport, dst, dport, proto="tcp", label="") -> None:
        self.fw_log.append(FwEntry(ts, action, dev, "in", proto, src, str(sport), dst, str(dport),
                                   label, self._key()))

    def _add_task(self, node, typ, vmid, user, start, end, status, lines) -> Task:
        t = Task(_upid(node, typ, vmid, user, start), node, typ, vmid, user, start, end, status)
        self.tasks.append(t)
        self.task_logs[t.upid] = list(lines)
        self.tasks = self.tasks[-40:]
        return t

    def guest(self, vmid: int) -> Guest:
        return next(g for g in self.guests if g.vmid == vmid)

    # --- scenario ------------------------------------------------------------
    def _scenario(self, now: float, el: float) -> None:
        # Backups every 3 minutes, 70 s long, from pve3's PBS; every third run has an error.
        if self.backup is None and el % 180 < 5 and el > 20:
            self.backup_runs += 1
            t = self._add_task("pve3", "vzdump", None, "root@pam", now, None, "running",
                               ["INFO: starting new backup job: vzdump 102 103 --storage pbs --mode snapshot"])
            self.backup = {"task": t, "fail": self.backup_runs % 3 == 0, "step": 0}
            self._log(now, "pve3", "INFO", "pvescheduler", f"starting task {t.upid}")
        if self.backup:
            b, t = self.backup, self.backup["task"]
            age = now - t.start
            lines = self.task_logs[t.upid]
            if age > 5 and b["step"] == 0:
                lines += ["INFO: Starting Backup of VM 102 (qemu)", "INFO: backup mode: snapshot"]
                b["step"] = 1
            if age > 35 and b["step"] == 1:
                lines += ["INFO: Finished Backup of VM 102 (00:00:30)", "INFO: Starting Backup of VM 103 (qemu)"]
                b["step"] = 2
                self._log(now, "pve2", "WARN", "corosync", "[TOTEM ] Retransmit List: 3c 3d")
            if age > 70:
                if b["fail"]:
                    lines += ["ERROR: Backup of VM 103 failed - job failed with err -5 - Input/output error",
                              "INFO: Failed at " + time.strftime("%Y-%m-%d %H:%M:%S")]
                    t.status = "job errors"
                    self._log(now, "pve3", "ERROR", "vzdump", "backup job failed for VM 103")
                else:
                    lines += ["INFO: Finished Backup of VM 103 (00:00:35)", "INFO: Backup job finished successfully"]
                    t.status = "OK"
                t.end = now
                self._log(now, "pve3", "INFO", "pvescheduler", f"end task {t.upid} {t.status}")
                self.backup = None

        # gitea live-migrates between pve1 and pve2 every 4 minutes.
        if self.migration is None and el % 240 < 5 and el > 90:
            g = self.guest(104)
            target = "pve2" if g.node == "pve1" else "pve1"
            t = self._add_task(g.node, "qmigrate", "104", "root@pam", now, None, "running",
                               [f"starting migration of VM 104 to node '{target}' (10.0.10.1{target[-1]})",
                                "starting VM 104 on remote node"])
            self.migration = {"task": t, "target": target, "vmid": 104, "dur": 25}
            self._log(now, g.node, "INFO", "pvedaemon", f"<root@pam> starting task {t.upid}")
        if self.migration and now - self.migration["task"].start > self.migration["dur"]:
            t, target = self.migration["task"], self.migration["target"]
            self.task_logs[t.upid] += ["migration active, transferred 3.8 GiB of 4.0 GiB",
                                       "migration finished successfully"]
            t.status, t.end = "OK", now
            self.guest(self.migration["vmid"]).node = target
            self._log(now, t.node, "INFO", "pvedaemon", f"<root@pam> end task {t.upid} OK")
            self.migration = None

        if random.random() < 0.03:
            self._log(now, random.choice(NODES)[0], "INFO", "pvedaemon",
                      "<root@pam> successful auth for user 'root@pam'")

    def _traffic(self, now: float, dt: float, el: float) -> None:
        def burst(rate):
            n = rate * dt
            return int(n) + (random.random() < n - int(n))

        attack = el % 150 < 30
        for _ in range(burst(12 if attack else 0.8)):
            src = "198.51.100.66" if attack and random.random() < 0.9 else \
                f"192.0.2.{random.randint(2, 250)}"
            port = 22 if attack else random.choice([22, 23, 445, 3389, 8080, 5900])
            self._fw(now - random.random() * dt, "block", "vtnet0", src, random.randint(1024, 65000),
                     WAN_IP, port, label="Block list (infradash_block)" if src in self.blocked
                     else "Default deny / state violation rule")
        for _ in range(burst(2.5)):
            self._fw(now - random.random() * dt, "pass", "vtnet0", f"198.51.100.{random.randint(2, 250)}",
                     random.randint(1024, 65000), "10.0.40.10", 443, label="NAT HTTPS → nginx-proxy")
        for _ in range(burst(1.5)):
            self._fw(now - random.random() * dt, "pass", "vlan0.40", "10.0.40.11",
                     random.randint(30000, 60000), "10.0.20.12", 5432, label="DMZ web → db")
        for _ in range(burst(0.35)):
            cam = random.choice(["10.0.30.21", "10.0.30.22"])
            self._fw(now - random.random() * dt, "block", "vlan0.30", cam, random.randint(30000, 60000),
                     "10.0.20.13", 445, label="Block IoT → SERVERS")
        for _ in range(burst(0.25)):
            self._fw(now - random.random() * dt, "block", "vlan0.30", "10.0.30.40",
                     random.randint(30000, 60000), "8.8.8.8", 53, "udp", "Force DNS via Pi-hole")
        for _ in range(burst(0.8)):
            self._fw(now - random.random() * dt, "pass", "vlan0.20", "10.0.20.53",
                     random.randint(30000, 60000), "1.1.1.1", 853, label="Pi-hole DoT upstream")
        for _ in range(burst(0.5)):
            self._fw(now - random.random() * dt, "pass", "vtnet1", "10.0.10.100",
                     random.randint(30000, 60000), "10.0.40.10", 443, label="LAN → DMZ web")
        for _ in range(burst(0.3)):
            self._fw(now - random.random() * dt, "pass", "vlan0.20", "10.0.20.13",
                     random.randint(30000, 60000), "192.0.2.80", 443, label="SERVERS → internet")
        for _ in range(burst(0.4)):
            self._fw(now - random.random() * dt, "pass", "vlan0.20", "10.0.20.30",
                     random.randint(30000, 60000), "10.0.20.40", 3000, label="")
        self.fw_log = [f for f in self.fw_log if f.ts > now - 600][-2000:]
        self.pve_log = self.pve_log[-200:]

    # --- provider interface ----------------------------------------------------
    async def poll(self) -> Raw:
        now = time.time()
        dt, self.last = max(0.2, now - self.last), now
        el = now - self.t0
        self._scenario(now, el)
        self._traffic(now, dt, el)
        for when, fn in [p for p in self.pending if p[0] <= now]:
            fn(now)
        self.pending = [p for p in self.pending if p[0] > now]
        storages, zpools, ceph, repl = self._storage(now, el)
        backing_up = {102, 103} if self.backup else set()
        migrating = {104} if self.migration else set()

        load: dict[str, float] = {n: 0.0 for n, _ in NODES}
        for g in self.guests:
            cores, base, _ = self.meta[g.vmid]
            if g.status != "running":
                g.cpu = g.mem = g.net_in = g.net_out = 0.0
                continue
            boost = 35 if g.vmid in backing_up else (25 if g.vmid in migrating else 0)
            if g.vmid == 202 and el % 150 < 30:
                boost += 15
            g.cpu = min(100.0, max(0.5, base + boost + random.gauss(0, base * 0.25)))
            g.mem = min(98.0, 35 + base * 2 + 6 * math.sin(el / 50 + g.vmid) + (12 if boost else 0))
            g.net_in = (0.4 + base / 4) * 1e6 * random.uniform(0.6, 1.4)
            g.net_out = (0.3 + base / 5) * 1e6 * random.uniform(0.6, 1.4) + (380e6 if g.vmid in backing_up else 0)
            g.uptime += dt
            load[g.node] += g.cpu * cores / 16
        if self.backup:
            load["pve3"] += 25
        nodes = [Node(name, True, min(100, 4 + load[name] + random.gauss(0, 1.5)),
                      50 + 15 * math.sin(el / 90 + i) + (10 if self.backup and name != "pve1" else 0),
                      uptime=19 * 86400 + el, ip=ip)
                 for i, (name, ip) in enumerate(NODES)]

        degraded = max(0.0, math.sin(2 * math.pi * el / 200 - 1.0))
        bad = degraded > 0.75
        rtt = 9 + random.gauss(0, 1) + (150 * (degraded - 0.75) / 0.25 if bad else 0)
        loss = (25 * (degraded - 0.75) / 0.25) if bad else 0.0
        gw = Gateway("WAN_DHCP", "loss" if loss > 5 else ("delay" if rtt > 60 else "online"),
                     round(rtt, 1), round(loss, 1), "203.0.113.1")

        bk = 380e6 if self.backup else 0
        rates = {"wan": (22e6 + 8e6 * math.sin(el / 40), 6e6), "lan": (3e6 + bk, 9e6),
                 "opt1": (12e6, 14e6 + bk), "opt2": (2e6, 1.5e6), "opt3": (18e6, 15e6)}
        ifaces = []
        for key, name, dev, cidr, vlan in IFACES:
            rx, tx = rates[key]
            ifaces.append(Iface(key, name, dev, cidr, vlan, True,
                                max(0, rx * random.uniform(0.8, 1.2) * (0.5 if key == "wan" and bad else 1)),
                                max(0, tx * random.uniform(0.8, 1.2))))
        arp = [ArpEntry(ip, "02:00:00:00:%02X:%02X" % (i, i * 7 % 256), dev, host)
               for i, (ip, dev, host) in enumerate(OTHER_HOSTS)]
        for name, ip in NODES:
            arp.append(ArpEntry(ip, "A0:36:9F:00:00:%s" % name[-1].zfill(2), "vtnet1", name))
        for g in self.guests:
            if g.status != "running":
                continue
            for nic, (vlan, ip) in zip(g.nics, self.meta[g.vmid][2]):
                arp.append(ArpEntry(ip, nic.mac, VLAN_DEV[vlan], ""))

        guests = [Guest(**{**vars(g), "nics": [Nic(**vars(n)) for n in g.nics]}) for g in self.guests]
        tasks = [Task(**vars(t)) for t in self.tasks]
        self.states_offset = min(0, self.states_offset + 40)       # killed states slowly come back
        states = max(0, int(3200 + 900 * math.sin(el / 70) + (2500 if el % 150 < 30 else 0)
                            + self.states_offset))
        fw = FwStats(cpu=min(100, 6 + (18 if el % 150 < 30 else 0) + random.gauss(0, 2)),
                     mem=31 + random.gauss(0, 0.5), states=states, states_limit=200_000, name="opnsense")
        return Raw(cluster="homelab", quorate=True, nodes=nodes, guests=guests, ifaces=ifaces,
                   gateways=[gw], arp=arp, fw_log=list(self.fw_log), tasks=tasks,
                   pve_log=list(self.pve_log), fw=fw, storages=storages, zpools=zpools,
                   ceph=ceph, replication=repl)

    def _storage(self, now: float, el: float):
        """Local ZFS on every node, a data pool on pve3 that loses a disk every 5 minutes,
        a Ceph pool that loses an OSD now and then, and a replication job that fails during backups."""
        grow = min(0.06, el / 20000)
        zfs_pct = {"pve1": 0.55, "pve2": 0.84 + grow, "pve3": 0.48}
        storages, zpools = [], []
        for name, _ in NODES:
            storages.append(Storage("local", name, "dir", 0.3 * 100e9, 100e9))
            storages.append(Storage("local-zfs", name, "zfspool", zfs_pct[name] * 0.9 * TB, 0.9 * TB))
            zpools.append(ZPool(name, "rpool", "ONLINE", 0.96 * TB, zfs_pct[name] * 0.9 * TB + 20e9, 9 + 6 * (name == "pve2")))
        tank_bad = 120 <= el % 300 < 200
        storages.append(Storage("tank-data", "pve3", "zfspool", 0.71 * 7.2 * TB, 7.2 * TB))
        zpools.append(ZPool("pve3", "tank", "DEGRADED" if tank_bad else "ONLINE", 8 * TB, 0.71 * 7.2 * TB, 14))
        if tank_bad and not self.zfs_fault_logged:
            self._log(now, "pve3", "WARN", "zed", "eid=4711 class=statechange pool='tank' vdev=sdc vdev_state=FAULTED")
            self.zfs_fault_logged = True
        elif not tank_bad and self.zfs_fault_logged:
            self._log(now, "pve3", "INFO", "zed", "eid=4712 class=resilver_finish pool='tank'")
            self.zfs_fault_logged = False

        osd_down = 150 <= el % 270 < 205
        used = 0.41 * 12 * TB
        storages.append(Storage("ceph-vm", None, "rbd", used / 3, 12 * TB / 3))   # 3 replicas
        checks = []
        if osd_down:
            checks = [("HEALTH_WARN", "1 osds down"),
                      ("HEALTH_WARN", "Degraded data redundancy: 1203/45120 objects degraded (2.667%), 28 pgs degraded")]
            if not self.osd_down_logged:
                self._log(now, "pve2", "WARN", "ceph-osd", "osd.4 heartbeat_check: no reply from 10.0.10.12:6804")
                self.osd_down_logged = True
        elif self.osd_down_logged:
            self._log(now, "pve2", "INFO", "ceph-osd", "osd.4 boot, marked up")
            self.osd_down_logged = False
        ceph = Ceph("HEALTH_WARN" if osd_down else "HEALTH_OK", checks, 6, 5 if osd_down else 6, 6,
                    used, 12 * TB, 128, 28 if osd_down else 0)

        r = self.repl
        if now - r["last_try"] >= 60:
            r["last_try"] = now
            if self.backup:
                r["fails"] += 1
                r["error"] = ("command 'zfs snapshot rpool/data/subvol-201-disk-0@__replicate_201-0__' "
                              "failed: dataset is busy")
            else:
                r["fails"], r["error"], r["last"] = 0, "", now
        last = r["last"]
        repl = [Replication("201-0", 201, "pve1", "pve2", last, r["fails"], r["error"])]
        return storages, zpools, ceph, repl

    # --- simulated actions ----------------------------------------------------------
    async def run_action(self, a) -> str:
        p, now = a.params, time.time()
        if a.kind.startswith("pve:"):
            g = self.guest(p["vmid"])
            if a.kind == "pve:migrate":
                if self.migration:
                    raise RuntimeError("another migration is already running")
                t = self._add_task(g.node, "qmigrate" if g.kind == "qemu" else "vzmigrate", str(g.vmid),
                                   "infradash@pve!dash", now, None, "running",
                                   [f"starting migration of {g.label} to node '{p['target']}'"])
                self.migration = {"task": t, "target": p["target"], "vmid": g.vmid, "dur": 15}
                return f"task started ({t.upid})"
            op = a.kind[4:]
            typ = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot", "stop": "qmstop"}[op]
            if g.kind == "lxc":
                typ = typ.replace("qm", "vz")
            t = self._add_task(g.node, typ, str(g.vmid), "infradash@pve!dash", now, None, "running", [f"{typ} {g.vmid}"])
            delay = {"start": 3, "shutdown": 6, "reboot": 8, "stop": 1}[op]

            def done(ts, g=g, t=t, op=op):
                if op in ("shutdown", "stop"):
                    g.status = "stopped"
                elif op == "start":
                    g.status, g.uptime = "running", 0
                else:
                    g.uptime = 0
                t.status, t.end = "OK", ts
                self.task_logs[t.upid].append("TASK OK")
            self.pending.append((now + delay, done))
            return f"task started ({t.upid})"
        if a.kind == "fw:block":
            self.blocked.add(p["ip"])
            return f"{p['ip']} added to alias {self.block_alias}"
        if a.kind == "fw:kill":
            dropped = random.randint(40, 400) if p["ip"].startswith(("198.51.100.", "192.0.2.")) else random.randint(2, 30)
            self.states_offset -= dropped
            return f"dropped {dropped} states for {p['ip']}"
        raise RuntimeError(f"unsupported action {a.kind}")

    async def task_log(self, node: str, upid: str) -> list[str]:
        return list(self.task_logs.get(upid, []))

    async def close(self) -> None:
        pass
