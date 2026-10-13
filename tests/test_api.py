"""The shared HTTP layer, against real sockets: these are transport-level failures that a
mock transport can't produce."""
import asyncio
import json
import socket
import time

import pytest

from infradash.api import ApiClient, ApiError

BODY = json.dumps({"rows": [{"n": i} for i in range(50)]}).encode()


class Server:
    """A tiny HTTP server whose misbehaviour each test picks."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests: list[str] = []      # request lines
        self.connections = 0

    async def __aenter__(self):
        self.srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.srv.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.srv.close()

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                line = head.split(b"\r\n", 1)[0].decode()
                self.requests.append(line)
                if not await self.behaviour(self, line, writer):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def ok(body=BODY, status="200 OK"):
    return (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode() + body


def malformed_chunked(body=BODY):
    # The chunk-size line undercounts the data that follows, as the firewall log responses did.
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(body) - 7:x}\r\n".encode() + body + b"\r\n0\r\n\r\n")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_malformed_chunk_footer_recovers_over_http10_and_stays_there():
    async def behaviour(srv, line, writer):
        if line.endswith("HTTP/1.0"):
            # HTTP/1.0: no chunking, the body runs to the close.
            writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + BODY)
            await writer.drain()
            return False
        writer.write(malformed_chunked())
        await writer.drain()
        return False

    async def main():
        async with Server(behaviour) as srv:
            api = ApiClient("opnsense", [srv.url])
            try:
                first = await api.get_json("/log", params={"limit": 5})
                second = await api.get_json("/log", params={"limit": 5})
            finally:
                await api.close()
            return srv, first, second

    srv, first, second = asyncio.run(main())
    assert first == second == json.loads(BODY)
    http11 = [r for r in srv.requests if r.endswith("HTTP/1.1")]
    assert len(http11) == 2                       # the original and one retry, then never again
    assert srv.requests[-1] == "GET /log?limit=5 HTTP/1.0"


def test_server_dropping_a_connection_is_retried():
    async def behaviour(srv, line, writer):
        if len(srv.requests) == 1:
            return False                          # read the request, close without answering
        writer.write(ok())
        await writer.drain()
        return True

    async def main():
        async with Server(behaviour) as srv:
            api = ApiClient("proxmox", [srv.url])
            try:
                return await api.get_json("/cluster/status")
            finally:
                await api.close()

    assert asyncio.run(main()) == json.loads(BODY)


def test_post_is_not_retried():
    async def behaviour(srv, line, writer):
        return False

    async def main():
        async with Server(behaviour) as srv:
            api = ApiClient("proxmox", [srv.url])
            try:
                with pytest.raises(ApiError) as err:
                    await api.post_json("/nodes/a/qemu/100/status/stop")
            finally:
                await api.close()
            return srv, err.value

    srv, err = asyncio.run(main())
    assert len(srv.requests) == 1 and err.transport


def test_fails_over_to_the_next_url_and_sticks_with_it():
    async def behaviour(srv, line, writer):
        writer.write(ok())
        await writer.drain()
        return True

    async def main():
        async with Server(behaviour) as srv:
            dead = f"http://127.0.0.1:{free_port()}"
            api = ApiClient("proxmox", [dead, srv.url])
            try:
                await api.get_json("/a")
                await api.get_json("/b")
            finally:
                await api.close()
            return srv, api

    srv, api = asyncio.run(main())
    assert [r.split()[1] for r in srv.requests] == ["/a", "/b"]
    assert api.status.endpoint == srv.url


def test_everything_down_fails_fast_with_a_transport_error():
    async def main():
        api = ApiClient("proxmox", [f"http://127.0.0.1:{free_port()}", f"http://127.0.0.1:{free_port()}"])
        try:
            t0 = time.monotonic()
            with pytest.raises(ApiError) as first:
                await api.get_json("/x")
            with pytest.raises(ApiError):
                await api.get_json("/y")          # both are marked down: one probe, no waiting
            return first.value, time.monotonic() - t0
        finally:
            await api.close()

    err, took = asyncio.run(main())
    assert err.transport and "unreachable" in str(err)
    assert took < 2


def test_http_errors_are_api_errors():
    async def behaviour(srv, line, writer):
        path = line.split()[1]
        if path == "/denied":
            writer.write(ok(b'{"error": "no"}', "403 Forbidden"))
        elif path == "/old":
            writer.write(ok(b"", "404 Not Found"))
        elif path == "/new":
            writer.write(ok(b'{"v": 2}'))
        else:
            writer.write(ok(b"<html>not json</html>"))
        await writer.drain()
        return True

    async def main():
        async with Server(behaviour) as srv:
            api = ApiClient("opnsense", [srv.url], denied_hint=" (check privileges)")
            try:
                with pytest.raises(ApiError) as denied:
                    await api.get_json("/denied")
                with pytest.raises(ApiError) as bad:
                    await api.get_json("/html")
                fallback = await api.get_json("/new", "/old")
                renamed = await api.get_json("/old", "/new")
            finally:
                await api.close()
            return denied.value, bad.value, fallback, renamed

    denied, bad, fallback, renamed = asyncio.run(main())
    assert denied.denied and "check privileges" in str(denied)
    assert "invalid JSON" in str(bad) and isinstance(bad, RuntimeError)
    assert fallback == renamed == {"v": 2}
