import asyncio
import time

from infradash.collector import Collector
from infradash.demo import DemoProvider
from infradash.health import find_issues
from infradash.correlate import build_snapshot
from infradash.models import (Guest, HaService, HaStatus, Iface, Nic, Node, Raw, SourceStatus)


class SlowDemo(DemoProvider):
    def __init__(self, delay):
        super().__init__()
        self.delay, self.polls = delay, 0

    async def poll(self):
        self.polls += 1
        await asyncio.sleep(self.delay)
        return await super().poll()


def test_cancelled_caller_does_not_abandon_the_poll():
    async def main():
        prov = SlowDemo(0.3)
        col = Collector(prov)
        caller = asyncio.create_task(col.poll())
        await asyncio.sleep(0.1)
        caller.cancel()                     # what the UI's exclusive worker does on the next tick
        snap = await col.poll()             # picks up the same poll, doesn't start another
        await col.close()
        return prov.polls, snap

    polls, snap = asyncio.run(main())
    assert polls == 1 and snap.raw.guests


def test_new_errors_only_lists_errors_that_just_appeared():
    class Erroring(DemoProvider):
        n = 0

        async def poll(self):
            raw = await super().poll()
            self.n += 1
            raw.errors = ["proxmox zfs on tower-3: HTTP 403"] + (["opnsense arp: HTTP 500"] if self.n == 2 else [])
            return raw

    async def main():
        col = Collector(Erroring())
        a, b = await col.poll(), await col.poll()
        await col.close()
        return a.raw.new_errors, b.raw.new_errors

    first, second = asyncio.run(main())
    assert first == ["proxmox zfs on tower-3: HTTP 403"] and second == ["opnsense arp: HTTP 500"]


def snapshot(raw, hint=None):
    return build_snapshot(raw, [], [], [], {}, fw_guest_hint=hint)


def keep_raw(status="running"):
    keep = Guest(100, "keep", "qemu", "tower-2", status, nics=[Nic("net0", "BC:24:11:00:00:02", "vmbr0")])
    return Raw(nodes=[Node("tower-2", True, 5, 40)], guests=[keep])


def test_firewall_unreachable_names_its_stopped_vm():
    raw = keep_raw()
    raw.ifaces = [Iface("lan", "LAN", "vtnet1", "10.77.10.1/24", mac="BC:24:11:00:00:02")]
    assert snapshot(raw).fw_guest == "guest:100"

    down = keep_raw("stopped")             # no interfaces: the firewall isn't answering
    down.sources["opnsense"] = SourceStatus("opnsense", ok=False, error="unreachable", last_ok=time.time() - 90)
    snap = snapshot(down, hint="guest:100")
    issue = next(i for i in find_issues(snap, {}) if i.key == "source:opnsense")
    assert issue.severity == "crit" and issue.entity == "guest:100"
    assert "keep is stopped on tower-2" in issue.detail and "last answered 1m ago" in issue.detail


def test_ha_rules():
    raw = keep_raw()
    raw.nodes.append(Node("tower-3", True, 5, 40))
    raw.ha = HaStatus(master=None, lrm={"tower-3": "old timestamp - dead?"}, services=[
        HaService("vm:100", "tower-2", "error"), HaService("ct:201", "tower-3", "fence"),
        HaService("ct:202", "tower-3", "fence")])
    issues = {i.key: i for i in find_issues(snapshot(raw), {})}
    assert issues["ha:vm:100"].severity == "crit" and issues["ha:vm:100"].entity == "guest:100"
    assert issues["ha:fence:tower-3"].entity == "node:tower-3"
    assert sum(k.startswith("ha:fence") for k in issues) == 1
    assert "ha:master" in issues and "ha:lrm:tower-3" in issues


def test_demo_polls_cleanly():
    async def main():
        col = Collector(DemoProvider())
        snaps = [await col.poll() for _ in range(3)]
        await col.close()
        return snaps

    snaps = asyncio.run(main())
    assert all(s.raw.guests and s.raw.ifaces and s.events for s in snaps)
