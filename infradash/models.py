"""Data model shared by the OPNsense / Proxmox collectors, the demo and the UI."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]
LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}


def fmt_bps(bps: float) -> str:
    for unit, div in (("Gb/s", 1e9), ("Mb/s", 1e6), ("kb/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} b/s"


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


# --- raw objects, as reported by the two systems -----------------------------
@dataclass
class Node:
    name: str
    online: bool
    cpu: float          # %
    mem: float          # %
    uptime: float = 0
    ip: str | None = None

    @property
    def id(self) -> str:
        return f"node:{self.name}"


@dataclass
class Nic:
    name: str           # net0
    mac: str            # upper-case aa:bb:...
    bridge: str = ""
    vlan: int | None = None
    ips: list[str] = field(default_factory=list)   # from config / guest agent


@dataclass
class Guest:
    vmid: int
    name: str
    kind: str           # qemu | lxc
    node: str
    status: str         # running | stopped | paused ...
    cpu: float = 0.0    # % of its allocated cores
    mem: float = 0.0    # %
    maxmem: float = 0
    net_in: float = 0.0   # bit/s
    net_out: float = 0.0  # bit/s
    uptime: float = 0
    nics: list[Nic] = field(default_factory=list)
    disks: list[tuple[str, str]] = field(default_factory=list)   # (storage, size)

    @property
    def id(self) -> str:
        return f"guest:{self.vmid}"

    @property
    def label(self) -> str:
        return f"{self.vmid} {self.name}"


@dataclass
class Iface:
    key: str            # lan, opt1, wan
    name: str           # description, e.g. SERVERS
    device: str         # vtnet1, vlan0.20
    cidr: str | None = None   # firewall address with prefix, e.g. 10.0.20.1/24
    vlan: int | None = None
    up: bool = True
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    mac: str = ""       # upper-case; lets the firewall be matched to its own VM

    @property
    def id(self) -> str:
        return f"if:{self.key}"

    @property
    def network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
        try:
            return ipaddress.ip_interface(self.cidr).network if self.cidr else None
        except ValueError:
            return None

    @property
    def address(self) -> str | None:
        return self.cidr.split("/")[0] if self.cidr else None


@dataclass
class Gateway:
    name: str
    status: str         # online | down | loss | delay | unknown
    rtt_ms: float | None = None
    loss_pct: float | None = None
    address: str | None = None

    @property
    def id(self) -> str:
        return f"gw:{self.name}"


@dataclass
class ArpEntry:
    ip: str
    mac: str
    device: str
    hostname: str = ""


@dataclass
class FwEntry:
    ts: float
    action: str         # pass | block | reject
    device: str
    direction: str
    proto: str
    src: str
    sport: str
    dst: str
    dport: str
    label: str = ""
    key: str = ""


@dataclass
class Task:
    upid: str
    node: str
    type: str
    vmid: str | None
    user: str
    start: float
    end: float | None
    status: str         # running | OK | WARNINGS: n | error text


@dataclass
class PveLog:
    ts: float
    node: str
    level: str
    tag: str
    msg: str
    key: str = ""


@dataclass
class FwStats:
    cpu: float | None = None
    mem: float | None = None
    states: int | None = None
    states_limit: int | None = None
    name: str = "opnsense"


@dataclass
class Raw:
    """Everything one poll of both systems returned."""
    cluster: str = ""
    quorate: bool | None = None
    nodes: list[Node] = field(default_factory=list)
    guests: list[Guest] = field(default_factory=list)
    ifaces: list[Iface] = field(default_factory=list)
    gateways: list[Gateway] = field(default_factory=list)
    arp: list[ArpEntry] = field(default_factory=list)
    fw_log: list[FwEntry] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    pve_log: list[PveLog] = field(default_factory=list)
    fw: FwStats | None = None
    storages: list["Storage"] = field(default_factory=list)
    zpools: list["ZPool"] = field(default_factory=list)
    ceph: "Ceph | None" = None
    replication: list["Replication"] = field(default_factory=list)
    ha: "HaStatus | None" = None
    errors: list[str] = field(default_factory=list)
    new_errors: list[str] = field(default_factory=list)   # errors the previous poll didn't have
    sources: dict[str, "SourceStatus"] = field(default_factory=dict)   # "opnsense" / "proxmox"

    def merge(self, other: "Raw") -> None:
        """Fold another system's poll into this one (each system fills its own fields)."""
        for name, default in vars(Raw()).items():
            if name in ("errors", "new_errors", "sources"):
                continue
            value = getattr(other, name)
            if value != default:
                setattr(self, name, value)
        self.errors.extend(other.errors)
        self.sources.update(other.sources)


@dataclass
class SourceStatus:
    """How the connection to one API is doing."""
    name: str                       # opnsense | proxmox
    endpoint: str = ""              # base URL in use (after any failover)
    ok: bool = True                 # the last poll reached the API
    last_ok: float | None = None    # when it last did
    error: str = ""
    stale: bool = False             # this poll shows cached data because the API didn't answer


# --- storage ------------------------------------------------------------------
@dataclass
class Storage:
    name: str
    node: str | None        # None for shared storage (Ceph RBD/CephFS, NFS, PBS...)
    kind: str               # zfspool, rbd, cephfs, dir, nfs, pbs ...
    used: float
    total: float
    active: bool = True

    @property
    def id(self) -> str:
        return f"st:{self.name}" if self.node is None else f"st:{self.node}/{self.name}"

    @property
    def label(self) -> str:
        return self.name if self.node is None else f"{self.node}/{self.name}"

    @property
    def pct(self) -> float:
        return 100 * self.used / self.total if self.total else 0.0


@dataclass
class ZPool:
    node: str
    name: str
    health: str             # ONLINE, DEGRADED, FAULTED ...
    size: float
    alloc: float
    frag: float

    @property
    def id(self) -> str:
        return f"zpool:{self.node}/{self.name}"

    @property
    def pct(self) -> float:
        return 100 * self.alloc / self.size if self.size else 0.0


@dataclass
class Ceph:
    health: str             # HEALTH_OK | HEALTH_WARN | HEALTH_ERR
    checks: list[tuple[str, str]] = field(default_factory=list)   # (severity, message)
    osds: int = 0
    osds_up: int = 0
    osds_in: int = 0
    used: float = 0
    total: float = 0
    pgs: int = 0
    pgs_unclean: int = 0

    @property
    def pct(self) -> float:
        return 100 * self.used / self.total if self.total else 0.0


@dataclass
class Replication:
    job: str                # 102-0
    vmid: int
    source: str
    target: str
    last_sync: float | None
    fail_count: int = 0
    error: str = ""


@dataclass
class HaService:
    sid: str                # vm:100 | ct:201
    node: str
    state: str              # started | stopped | error | fence | recovery | freeze | migrate ...
    request: str = ""       # requested state

    @property
    def vmid(self) -> int | None:
        tail = self.sid.rpartition(":")[2]
        return int(tail) if tail.isdigit() else None


@dataclass
class HaStatus:
    master: str | None = None           # node running the CRM master
    master_state: str = ""              # active | old timestamp - dead? | ...
    lrm: dict[str, str] = field(default_factory=dict)    # node -> active | idle | maintenance mode ...
    services: list[HaService] = field(default_factory=list)


# --- derived objects ----------------------------------------------------------
@dataclass
class Event:
    ts: float
    source: str                 # fw | pve | task
    level: str
    message: str
    entities: list[str]         # resolved entity ids, most specific first
    iface: str | None = None    # if:<key> the packet crossed (firewall events)
    ref: object = None          # FwEntry | Task | PveLog


@dataclass
class MetricPanel:
    title: str
    unit: str
    series: dict[str, list[float]]
    warn: float | None = None
    crit: float | None = None


@dataclass
class Issue:
    severity: str           # crit | warn
    entity: str | None
    title: str
    detail: str = ""
    key: str = ""
    since: float = 0.0


@dataclass
class Action:
    kind: str               # pve:start pve:shutdown pve:reboot pve:stop pve:migrate fw:block fw:kill
    label: str
    question: str
    detail: str
    entity: str | None = None
    params: dict = field(default_factory=dict)
    type_to_confirm: str | None = None   # word the user must type for dangerous actions
