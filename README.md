# infradash

A terminal dashboard for an **OPNsense** firewall and a **Proxmox VE** cluster, built the
same way as termdash: metric sparklines on top, a live topology map, a guest table with an
inspector, and an event stream at the bottom — with the two systems joined together so you
can follow a problem from a firewall log line to the VM, the node it runs on, and its tasks.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m infradash --demo            # simulated homelab, no hardware needed
python -m infradash -c config.yaml    # your firewall and cluster
```

The demo has a firewall with WAN, LAN and three VLANs and a three-node cluster with ten
guests. Every few minutes a backup loads two nodes (one run in three fails), a VM
live-migrates between nodes, SSH brute-force bursts hit WAN, an IoT camera keeps trying SMB
into the server VLAN, and the ISP gateway degrades.

## What's on screen

**Metrics:** CPU and memory for the firewall and each node, traffic per firewall interface,
gateway latency, and how full each storage is.

**Attention:** with nothing selected, the inspector lists everything that needs a look, worst
first, with how long each has been going on; the header always shows the count. `n` jumps
to the next issue. The rules cover a lost quorum, offline or overloaded nodes, guests pegged
on CPU or memory, storage over 80/90%, ZFS pools that aren't ONLINE or are over 80% allocated
or heavily fragmented, Ceph health warnings, down OSDs and Ceph usage, failing or stale ZFS
replication, a firewall state table filling up, interfaces or gateways down, gateway packet
loss or latency, a single source being blocked over and over (a scan or brute force), and
tasks that failed in the last day.

**Topology map** (`v` toggles between two views of the same relationships):
the network view is firewall → interfaces/VLANs → gateways, nodes, guests and other hosts
on each network; the cluster view is cluster → nodes → guests, ZFS pools and storages, plus
Ceph with its pools. Selecting a storage highlights and lists the guests with disks on it.

**Guests:** every VM and container with live CPU (plus trend), memory, network rate and
network placement. Selecting a node or an interface narrows the table to what's on it.

**Inspector:** for the selected guest, node, interface, gateway, host or the firewall —
NICs, MACs, VLAN tags and IPs; firewall passes/blocks, top blocked ports and sources, who
it talks to; recent Proxmox tasks; and the full task log when you open a task event.

**Events:** firewall log, Proxmox cluster log and Proxmox tasks in one stream, with IPs
replaced by guest and host names.

## How the two systems are joined

| Link | How it's worked out |
|---|---|
| Guest → IP | NIC MAC from the Proxmox config, matched against the firewall's ARP table; plus static IPs in container configs and QEMU guest agent data |
| Guest/node → firewall network | ARP entry's interface first, then the NIC's VLAN tag vs. VLAN interfaces, then IP-in-subnet |
| Firewall log → guest/host | Source and destination IPs resolved through the above |
| Task / cluster log → guest | Task `id`, or the VMID in the message / UPID |
| Gateway → interface | Gateway address inside an interface subnet, else the WAN interface |
| Guest → storage | Disk lines in the guest config (`scsi0: local-zfs:vm-101-disk-0,size=32G`) |

Guests that are stopped, or that the firewall can't see (e.g. on a bridge it isn't part of),
appear in a separate "not seen by the firewall" group rather than being guessed.

## Keys

| Key | Action |
|---|---|
| ↑ ↓ / Enter | move / select (guest row, or the guest/host/interface an event is about; task events also load the task log) |
| Tab | move focus between map, guests, inspector, events |
| `s` / `S` | cycle the selection through firewall, interfaces, gateways, nodes, guests |
| `v` | network view ↔ cluster view |
| `n` | jump to the next issue in the attention list |
| `x` | actions for the selected guest/host, or for the focused firewall event |
| `l` | minimum event level (default WARN: blocks, warnings, failed tasks) |
| `f` | event source: all / fw / pve / task / you (actions you took) |
| `Esc` | clear selection · `r` refresh · `p` pause · `q` quit |

## Actions

Actions are off until you set `actions: enabled: true`, and the header says "read-only"
while they are. Press `x` on a selected guest or host, or with the cursor on a firewall
event, to see what you can do:

| On | Actions |
|---|---|
| Guest | start · clean shutdown · reboot · hard power-off · migrate to another node · kill its firewall states |
| Firewall event / host | kill firewall states for an address · add an address to the block alias |

Each one opens a dialog that says in plain words what will happen (for a migration: live or
restart, and whether local ZFS disks will be copied or only the delta sent because the guest
is replicated there). Cancel has focus by default. A hard power-off, or blocking one of your
own hosts, also requires typing its name. The event table keeps its cursor on the same event
while new ones arrive, so what you act on is what you were looking at. Every action, and
whether it succeeded, appears in the event stream as source `you` and is appended to the
log file.

Blocking needs a one-time setup on OPNsense: create a **Host(s)** alias named as in
`block_alias`, and a block rule on WAN with that alias as source, above your pass rules.
Adding to the alias doesn't close existing connections; kill the address's states for that.

## Credentials (read-only)

**Proxmox** — an API token with the built-in `PVEAuditor` role is enough for everything
except guest-agent IP lookups:

```bash
pveum user add infradash@pve
pveum acl modify / --users infradash@pve --roles PVEAuditor
pveum user token add infradash@pve dash --privsep 1
pveum acl modify / --tokens 'infradash@pve!dash' --roles PVEAuditor
```

Guest-agent IPs need an extra guest-agent/monitor privilege on the VMs; without it (or with
`guest_agent: false`) IPs still come from the firewall's ARP table.

**OPNsense** — create a user under System → Access → Users, generate an API key for it
(this downloads the key and secret), and give it access to the read-only pages the
dashboard uses: interface overview, gateway status, ARP table, firewall log, traffic and
system activity/health. The client uses the snake_case API paths from OPNsense 25.7 and
falls back to the older camelCase paths automatically.

**For actions**, give separate credentials write access rather than widening the read-only
ones. On Proxmox, a role with `VM.PowerMgmt` (start/stop/reboot) and `VM.Migrate`; migrating
disks on local storage may also need `Datastore.AllocateSpace` on the target storage:

```bash
pveum role add InfraDashOps --privs "VM.Audit VM.PowerMgmt VM.Migrate"
pveum acl modify /vms --tokens 'infradash@pve!dash' --roles InfraDashOps
```

On OPNsense, the API user additionally needs access to edit firewall aliases and to the
firewall states page.

If a privilege is missing, the dashboard keeps running: the header shows an API-error count,
a notification names the endpoint that returned 401/403, and that panel stays empty.

## Code layout

- `infradash/opnsense.py`, `infradash/proxmox.py` — API clients and response parsers
- `infradash/correlate.py` — the join: IP/MAC ownership, network placement, event entities
- `infradash/health.py` — the attention rules
- `infradash/actions.py`, `infradash/dialogs.py` — available actions and the confirmation dialogs
- `infradash/collector.py` — metric history and log de-duplication across polls
- `infradash/views.py` — topology trees and the inspector
- `infradash/demo.py` — the simulated homelab
- `infradash/app.py` — the Textual UI
