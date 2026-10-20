"""The OPNsense and Proxmox clients against fake APIs (httpx.MockTransport)."""
import asyncio
import time

import httpx

from infradash.opnsense import OPNsenseClient, newest_digest
from infradash.proxmox import ProxmoxClient, node_urls, parse_ha_status


def transport(routes, calls=None, down=None):
    """routes: path -> JSON (or a callable(request) returning JSON / an awaitable)."""
    async def handler(request: httpx.Request):
        if calls is not None:
            calls.append(request)
        if down and down():
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        for prefix in ("/api2/json", "/api"):
            path = path.removeprefix(prefix)
        if path not in routes:
            return httpx.Response(404, json={})
        body = routes[path]
        if callable(body):
            body = body(request)
            if asyncio.iscoroutine(body):
                body = await body
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


# --- OPNsense -----------------------------------------------------------------------
LOG = [
    {"__timestamp__": "2026-10-13T10:00:02", "__digest__": "d2", "action": "block", "interface": "vtnet0",
     "src": "198.51.100.9", "dst": "203.0.113.7", "dstport": "22", "protoname": "tcp"},
    {"__timestamp__": "2026-10-13T10:00:01", "__digest__": "d1", "action": "pass", "interface": "vtnet1",
     "src": "10.77.20.5", "dst": "1.1.1.1", "dstport": "53", "protoname": "udp"},
]


def opn_routes():
    counter = {"n": 0}

    def traffic(_):
        counter["n"] += 1
        b = counter["n"] * 1_000_000
        return {"interfaces": {"lan": {"bytes received": b, "bytes transmitted": b // 2}}}

    return {
        "/interfaces/overview/interfaces_info": {"rows": [
            {"identifier": "wan", "description": "WAN", "device": "vtnet0", "addr4": "203.0.113.7/24",
             "macaddr": "bc:24:11:00:00:01", "status": "up"},
            {"identifier": "lan", "description": "LAN", "device": "vtnet1", "addr4": "10.77.10.1/24",
             "macaddr": "bc:24:11:00:00:02", "status": "up"}]},
        "/diagnostics/traffic/interface": traffic,
        "/routes/gateway/status": {"items": [{"name": "WAN_GW", "status": "none", "delay": "12.3 ms",
                                              "loss": "0.0 %", "address": "203.0.113.1"}]},
        "/diagnostics/interface/getArp": {"rows": []},        # only the old name exists
        "/diagnostics/firewall/log": lambda req: LOG if "digest" not in req.url.params else [],
        "/diagnostics/activity/get_activity": {"headers": ["CPU:  3.0% user,  0.0% nice, 90.0% idle"]},
        "/diagnostics/system/system_resources": {"memory": {"total": "1000", "used": "250"}},
        "/diagnostics/firewall/pf_states": {"current": "120", "limit": "1000"},
    }


def test_opnsense_poll_and_incremental_log():
    calls = []
    cfg = {"url": "https://fw.example", "key": "k", "secret": "s"}

    async def main():
        c = OPNsenseClient(cfg, transport=transport(opn_routes(), calls))
        try:
            first = await c.poll()
            await asyncio.sleep(0.6)
            second = await c.poll()
        finally:
            await c.close()
        return first, second

    first, second = asyncio.run(main())
    assert [i.key for i in first.ifaces] == ["wan", "lan"] and first.ifaces[0].mac == "BC:24:11:00:00:01"
    assert len(first.fw_log) == 2 and second.fw_log == []
    log_calls = [r for r in calls if r.url.path.endswith("/firewall/log")]
    assert "digest" not in log_calls[0].url.params and log_calls[1].url.params["digest"] == "d2"
    assert all(r.headers["accept-encoding"] == "identity" for r in calls)
    assert first.fw.cpu == 10 and first.fw.mem == 25 and first.fw.states == 120
    lan = next(i for i in second.ifaces if i.key == "lan")
    assert lan.rx_bps > 0 and lan.tx_bps > 0
    assert first.sources["opnsense"].ok and not first.errors


def test_opnsense_outage_shows_last_data_with_one_error():
    state = {"down": False}
    cfg = {"url": "https://fw.example", "key": "k", "secret": "s"}

    async def main():
        c = OPNsenseClient(cfg, transport=transport(opn_routes(), down=lambda: state["down"]))
        c.api.urls.append("https://fw2.example")     # failover gets tried too, and also fails
        try:
            await c.poll()
            state["down"] = True
            return await c.poll()
        finally:
            await c.close()

    raw = asyncio.run(main())
    st = raw.sources["opnsense"]
    assert not st.ok and st.stale
    assert len(raw.errors) == 1 and "unreachable" in raw.errors[0] and "showing data" in raw.errors[0]
    assert raw.gateways and raw.ifaces          # last good data, flagged stale


def test_newest_digest_ignores_row_order():
    assert newest_digest({"rows": list(reversed(LOG))}) == "d2"
    assert newest_digest({"rows": [{"__timestamp__": "x"}]}) is None


# --- Proxmox ------------------------------------------------------------------------
def pve_routes(agent_delay=0.0):
    async def agent(_):
        await asyncio.sleep(agent_delay)
        return {"data": {"result": [{"hardware-address": "bc:24:11:aa:00:01",
                                     "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.77.20.11"}]}]}}

    return {
        "/cluster/status": {"data": [
            {"type": "cluster", "name": "tower", "quorate": 1},
            {"type": "node", "name": "tower-1", "online": 1, "ip": "10.77.50.11"},
            {"type": "node", "name": "tower-2", "online": 1, "ip": "10.77.50.12"},
            {"type": "node", "name": "tower-3", "online": 1, "ip": "10.77.50.13"}]},
        "/cluster/resources": {"data": [
            {"type": "node", "node": "tower-1", "status": "online", "cpu": 0.1, "mem": 4, "maxmem": 16},
            {"type": "node", "node": "tower-2", "status": "online", "cpu": 0.2, "mem": 8, "maxmem": 16},
            {"type": "node", "node": "tower-3", "status": "online", "cpu": 0.3, "mem": 12, "maxmem": 16},
            {"type": "qemu", "vmid": 100, "name": "keep", "node": "tower-2", "status": "running",
             "cpu": 0.05, "mem": 1, "maxmem": 2, "netin": 1000, "netout": 2000},
            {"type": "qemu", "vmid": 9000, "name": "tmpl", "node": "tower-1", "template": 1},
            {"type": "storage", "storage": "local-zfs", "node": "tower-1", "plugintype": "zfspool",
             "disk": 10, "maxdisk": 100, "status": "available", "id": "storage/tower-1/local-zfs"}]},
        "/cluster/tasks": {"data": [{"upid": "UPID:tower-1:1:2:3:qmstart:100:root@pam:", "node": "tower-1",
                                     "type": "qmstart", "id": "100", "user": "root@pam",
                                     "starttime": time.time() - 10, "endtime": time.time() - 5,
                                     "status": "OK"}]},
        "/cluster/log": {"data": [{"time": time.time(), "node": "tower-1", "pri": 6, "tag": "pvedaemon",
                                   "msg": "starting VM 100", "uid": "1"}]},
        "/nodes/tower-2/qemu/100/config": {"data": {"agent": "1", "net0": "virtio=BC:24:11:AA:00:01,bridge=vmbr0,tag=20",
                                                    "scsi0": "local-zfs:vm-100-disk-0,size=8G"}},
        "/nodes/tower-2/qemu/100/agent/network-get-interfaces": agent,
        "/cluster/ceph/status": httpx.Response(500, json={"data": None, "message": "binary not installed"}),
        "/cluster/ha/status/current": {"data": [
            {"id": "quorum", "type": "quorum", "node": "tower-1", "status": "OK"},
            {"id": "master", "type": "master", "node": "tower-1", "status": "tower-1 (active, Tue Oct 13)"},
            {"id": "lrm:tower-2", "type": "lrm", "node": "tower-2", "status": "tower-2 (active, Tue Oct 13)"},
            {"id": "service:vm:100", "type": "service", "sid": "vm:100", "node": "tower-2", "state": "started"}]},
        **{f"/nodes/tower-{i}/network": {"data": [
            {"iface": "vmbr0", "cidr": f"192.168.8.{10 + i}/24"},
            {"iface": "vmbr1", "cidr": f"10.77.50.{10 + i}/24"}]} for i in (1, 2, 3)},
        **{f"/nodes/tower-{i}/disks/zfs": {"data": [{"name": "rpool", "health": "ONLINE", "size": 100,
                                                     "alloc": 20, "frag": 3}]} for i in (1, 2, 3)},
        **{f"/nodes/tower-{i}/replication": {"data": []} for i in (1, 2, 3)},
    }


PVE_CFG = {"url": "https://192.168.8.11:8006", "token_id": "dash@pve!t", "token_secret": "x"}


def test_proxmox_poll_ha_and_node_discovery():
    async def main():
        c = ProxmoxClient(PVE_CFG, transport=transport(pve_routes()))
        try:
            raw = await c.poll()
            return raw, c.api.urls
        finally:
            await c.close()

    raw, urls = asyncio.run(main())
    assert raw.cluster == "tower" and raw.quorate and len(raw.nodes) == 3
    keep = raw.guests[0]
    assert [g.vmid for g in raw.guests] == [100]
    assert keep.nics[0].vlan == 20 and keep.nics[0].ips == ["10.77.20.11"] and keep.disks == [("local-zfs", "8G")]
    assert raw.ceph is None and not raw.errors                    # no Ceph: quiet
    assert raw.ha.master == "tower-1" and raw.ha.services[0].vmid == 100
    # Failover candidates come from the home-LAN bridge, not the unrouted Corosync VLAN.
    assert urls == ["https://192.168.8.11:8006/api2/json", "https://192.168.8.12:8006/api2/json",
                    "https://192.168.8.13:8006/api2/json"]


def test_slow_guest_agent_does_not_hold_up_the_poll():
    async def main():
        c = ProxmoxClient({**PVE_CFG, "slow_call_budget_s": 0.2}, transport=transport(pve_routes(agent_delay=1.0)))
        try:
            t0 = time.monotonic()
            first = await c.poll()
            took = time.monotonic() - t0
            await asyncio.sleep(1.0)                # the agent call finishes in the background
            second = await c.poll()
            return took, first, second
        finally:
            await c.close()

    took, first, second = asyncio.run(main())
    assert took < 0.8
    assert first.guests[0].nics[0].ips == [] and second.guests[0].nics[0].ips == ["10.77.20.11"]


def test_proxmox_outage_keeps_last_data_marked_stale():
    state = {"down": False}

    async def main():
        c = ProxmoxClient({**PVE_CFG, "discover_nodes": False},
                          transport=transport(pve_routes(), down=lambda: state["down"]))
        try:
            await c.poll()
            state["down"] = True
            return await c.poll()
        finally:
            await c.close()

    raw = asyncio.run(main())
    st = raw.sources["proxmox"]
    assert not st.ok and st.stale and len(raw.errors) == 1
    assert len(raw.nodes) == 3 and raw.guests[0].nics      # last good data and cached config


def test_node_urls_by_hostname():
    assert node_urls("https://tower-1.home.arpa:8006/api2/json", {}, ["tower-1", "tower-2"]) == \
        ["https://tower-2.home.arpa:8006/api2/json"]
    assert node_urls("https://pve.home.arpa:8006/api2/json", {}, ["tower-1", "tower-2"]) == []


def test_parse_ha_status():
    ha = parse_ha_status([
        {"type": "master", "node": "tower-1", "status": "tower-1 (old timestamp - dead?, Tue Oct 13)"},
        {"type": "lrm", "node": "tower-3", "status": "tower-3 (wait_for_agent_lock, Tue Oct 13)"},
        {"type": "service", "id": "service:ct:201", "node": "tower-3", "state": "fence"}])
    assert ha.master_state == "old timestamp - dead?" and ha.lrm == {"tower-3": "wait_for_agent_lock"}
    assert ha.services[0].sid == "ct:201" and ha.services[0].vmid == 201
    assert parse_ha_status([{"type": "quorum", "node": "x"}]) is None


def test_live_provider_isolates_a_failing_or_hanging_system():
    from infradash.live import LiveProvider

    async def main():
        live = LiveProvider({"opnsense": {"url": "https://fw", "key": "k", "secret": "s"},
                             "proxmox": PVE_CFG, "poll_timeout_s": 0.5})
        await live.opn.close()
        await live.pve.close()
        live.opn = OPNsenseClient({"url": "https://fw", "key": "k", "secret": "s"},
                                  transport=transport(opn_routes()))
        live.pve = ProxmoxClient(PVE_CFG, transport=transport(pve_routes()))

        async def hang():
            await asyncio.sleep(5)
        live.pve.poll = hang
        try:
            return await live.poll()
        finally:
            await live.close()

    raw = asyncio.run(main())
    assert raw.ifaces and raw.sources["opnsense"].ok
    assert not raw.sources["proxmox"].ok and raw.errors == ["proxmox: no answer within 0.5s"]
