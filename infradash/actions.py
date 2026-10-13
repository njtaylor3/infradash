"""Builds the list of actions available for whatever is selected, each with a plain-language
description of its consequences. Nothing here talks to an API; providers execute them."""
from __future__ import annotations

from .correlate import Snapshot
from .models import Action, Event, Guest


def _local_disks(snap: Snapshot, g: Guest) -> list[str]:
    shared = {s.name for s in snap.raw.storages if s.node is None}
    return [st for st, _ in g.disks if st not in shared]


def guest_actions(snap: Snapshot, g: Guest) -> list[Action]:
    p = {"node": g.node, "kind": g.kind, "vmid": g.vmid}
    what = "VM" if g.kind == "qemu" else "container"
    acts: list[Action] = []
    if g.status != "running":
        acts.append(Action("pve:start", f"Start {g.name}", f"Start {what} {g.label} on {g.node}?",
                           "Boots the guest with its current configuration.", g.id, p))
    else:
        acts += [
            Action("pve:shutdown", f"Shut down {g.name} (clean)", f"Shut down {what} {g.label}?",
                   "Asks the guest OS to shut down cleanly (ACPI for VMs). A VM that ignores ACPI "
                   "keeps running until Proxmox's timeout.", g.id, p),
            Action("pve:reboot", f"Reboot {g.name}", f"Reboot {what} {g.label}?",
                   "Clean reboot through the guest OS. Services on it are briefly unavailable.", g.id, p),
            Action("pve:stop", f"Stop {g.name} (hard power-off)", f"Hard power-off {what} {g.label}?",
                   "Cuts power immediately, like pulling the plug. Unsaved data and in-flight writes "
                   "inside the guest can be lost. Prefer a clean shutdown.", g.id, p,
                   type_to_confirm=g.name),
        ]
    local = _local_disks(snap, g)
    repl_targets = {r.target for r in snap.raw.replication if r.vmid == g.vmid}
    for n in snap.raw.nodes:
        if n.name == g.node or not n.online:
            continue
        if g.kind == "lxc" and g.status == "running":
            how = (f"Restart migration: the container stops on {g.node}, moves to {n.name} and starts "
                   "again — expect a short outage.")
        elif g.status == "running":
            how = f"Live migration to {n.name}; the VM keeps running."
        else:
            how = f"Offline migration to {n.name}."
        if local:
            how += (f" Disks on local storage ({', '.join(sorted(set(local)))}) are copied over the network"
                    + (" — only the changes since the last replication, since it is replicated there."
                       if n.name in repl_targets else ", which can take a while for large disks.")
                    + f" The target needs a storage with the same name on {n.name}.")
        acts.append(Action("pve:migrate", f"Migrate {g.name} → {n.name}",
                           f"Migrate {what} {g.label} from {g.node} to {n.name}?", how, g.id,
                           {**p, "target": n.name, "running": g.status == "running"}))
    return acts


def ip_actions(snap: Snapshot, ip: str, block_alias: str | None) -> list[Action]:
    owner = snap.ip_owner.get(ip)
    if owner == "fw":
        return []
    who = f"{snap.name(owner)} ({ip})" if owner else ip
    acts = [Action("fw:kill", f"Kill firewall states for {who}", f"Drop all open connections for {who}?",
                   "Removes every firewall state to or from this address. Connections that are still "
                   "allowed by the rules will simply reconnect; blocked ones won't.", owner, {"ip": ip})]
    if block_alias:
        internal = owner is not None
        acts.append(Action(
            "fw:block", f"Block {who} (add to alias {block_alias})",
            f"Add {who} to the firewall alias '{block_alias}'?",
            f"Takes effect only if a block rule references '{block_alias}'. Existing connections stay open "
            "until their states are killed." + (" This is one of your own hosts — blocking it may cut it "
                                               "off from other networks." if internal else ""),
            owner, {"ip": ip}, type_to_confirm=snap.name(owner) if internal else None))
    return acts


def available_actions(snap: Snapshot, selected: str | None, event: Event | None,
                      has_pve: bool, has_opn: bool, block_alias: str | None) -> list[Action]:
    acts: list[Action] = []
    if event is not None and event.source == "fw" and has_opn:
        for ip in (event.ref.src, event.ref.dst):
            acts += ip_actions(snap, ip, block_alias)
    elif selected:
        g = snap.guest(selected)
        if g and has_pve:
            acts += guest_actions(snap, g)
        if g and has_opn:
            for ip in snap.guest_ips.get(g.id, []):
                acts += [a for a in ip_actions(snap, ip, None)]
        if selected.startswith("host:") and has_opn:
            acts += ip_actions(snap, selected[5:], block_alias)
    seen, out = set(), []
    for a in acts:
        key = (a.kind, tuple(sorted(a.params.items())))
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out
