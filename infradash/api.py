"""HTTP plumbing shared by the OPNsense and Proxmox clients.

Every failure comes out as ApiError (a RuntimeError), never as a raw httpx exception, so one
bad request can't escape a caller's error handling and take a whole poll down with it.

What it does about flaky connections:

- Idle keep-alive connections are dropped after a couple of seconds. pveproxy closes idle
  connections after a few seconds too, and reusing one just as the server closes it fails
  with "Server disconnected without sending a response".
- Idempotent requests are retried once on a fresh connection after a transport error.
- Several base URLs can be given (e.g. every node of a Proxmox cluster). A URL that refuses
  connections is skipped for a while and the next one is used, so the API stays reachable
  while the node you normally talk to is down.
- Responses are requested uncompressed, and a GET whose HTTP/1.1 response is malformed
  (a "malformed chunk footer" from a server that miscounts chunk sizes) is retried over
  HTTP/1.0, which has no chunked encoding at all. Paths that needed this stay on HTTP/1.0.
"""
from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Iterable

import httpx

from .models import SourceStatus

DOWN_FOR_S = 10.0          # how long a URL that refused connections is skipped
KEEPALIVE_S = 2.0          # below pveproxy's idle timeout, so we never reuse a closing socket
MAX_BODY = 64 * 2**20


class ApiError(RuntimeError):
    """A request failed. `status` is the HTTP status, or None for transport failures."""

    def __init__(self, path: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{path}: {message}")
        self.path, self.status = path, status

    @property
    def transport(self) -> bool:
        return self.status is None

    @property
    def denied(self) -> bool:
        return self.status in (401, 403)


def _describe(e: Exception) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class ApiClient:
    def __init__(self, name: str, urls: Iterable[str], *, headers: dict[str, str] | None = None,
                 verify: bool | str = True, timeout: float = 10.0, connect_timeout: float = 3.0,
                 max_connections: int = 8, http2: bool = False, denied_hint: str = "",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.urls = [u.rstrip("/") for u in urls]
        if not self.urls:
            raise ValueError(f"{name}: no url configured")
        self.headers = {"Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})}
        self.verify = verify
        self.timeout = httpx.Timeout(timeout, connect=min(connect_timeout, timeout))
        self.status = SourceStatus(name, endpoint=self.urls[0])
        self.denied_hint = denied_hint
        self._http2 = http2
        self._transport = transport
        self._limits = httpx.Limits(max_connections=max_connections,
                                    max_keepalive_connections=max_connections,
                                    keepalive_expiry=KEEPALIVE_S)
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._down_until: dict[str, float] = {}
        self._active = self.urls[0]
        self._http10_paths: set[str] = set()
        self.last_answer = 0.0          # time.monotonic() of the last HTTP response of any kind
        self._sem = asyncio.Semaphore(max_connections)

    # --- endpoints ----------------------------------------------------------------
    def add_fallbacks(self, urls: Iterable[str]) -> None:
        """Extra base URLs to try after the configured ones (e.g. discovered cluster nodes)."""
        for u in urls:
            u = u.rstrip("/")
            if u not in self.urls:
                self.urls.append(u)

    def _client(self, base: str) -> httpx.AsyncClient:
        c = self._clients.get(base)
        if c is None:
            c = httpx.AsyncClient(base_url=base, headers=self.headers, verify=self.verify,
                                  timeout=self.timeout, limits=self._limits, http2=self._http2,
                                  transport=self._transport)
            self._clients[base] = c
        return c

    def _candidates(self) -> list[str]:
        """The active URL first, then the rest in order, skipping ones that recently refused.
        If all of them did, the one that comes back soonest is tried anyway."""
        now = time.monotonic()
        order = [self._active] + [u for u in self.urls if u != self._active]
        up = [u for u in order if self._down_until.get(u, 0.0) <= now]
        if up:
            return up
        probe = min(order, key=lambda u: self._down_until.get(u, 0.0))
        return [probe]

    # --- requests -----------------------------------------------------------------
    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None, data: dict | None = None) -> httpx.Response:
        """Send one request and return the response, whatever its status code."""
        idempotent = method == "GET"
        last: Exception | None = None
        for base in self._candidates():
            http10 = idempotent and path in self._http10_paths
            for attempt in range(2 if idempotent else 1):
                try:
                    r = await self._send(base, method, path, params, json, data, http10)
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    # Nothing was sent, so moving on to the next URL is safe for any method.
                    self._down_until[base] = time.monotonic() + DOWN_FOR_S
                    last = e
                    break
                except httpx.ProtocolError as e:
                    last = e
                    if not idempotent or http10:
                        raise ApiError(path, _describe(e)) from e
                    if attempt == 0:
                        continue            # e.g. a keep-alive connection closed under us
                    # A fresh HTTP/1.1 connection got a malformed response too: the server's
                    # framing is broken for this path, so read it without chunked encoding.
                    try:
                        r = await self._send(base, method, path, params, None, None, http10=True)
                    except httpx.TransportError as e2:
                        raise ApiError(path, f"{_describe(e)}; HTTP/1.0 retry: {_describe(e2)}") from e2
                    self._http10_paths.add(path)
                except httpx.TransportError as e:   # read/write errors, timeouts
                    last = e
                    if idempotent and attempt == 0 and not isinstance(e, httpx.TimeoutException):
                        continue
                    raise ApiError(path, _describe(e)) from e
                return self._answered(base, r)
        tried = ", ".join(self.urls)
        raise ApiError(path, f"unreachable ({tried}): {_describe(last) if last else 'no route'}")

    async def _send(self, base: str, method: str, path: str, params, json, data,
                    http10: bool) -> httpx.Response:
        async with self._sem:
            if http10:
                return await self._get_http10(base, path, params)
            return await self._client(base).request(method, path, params=params, json=json, data=data)

    def _answered(self, base: str, r: httpx.Response) -> httpx.Response:
        self.last_answer = time.monotonic()
        self._down_until.pop(base, None)
        if base != self._active:
            self._active = base
        self.status.endpoint = base
        return r

    async def get_json(self, *paths: str, params: dict | None = None):
        """GET the first of `paths` that exists (alternatives are older names of the same
        endpoint) and return its decoded JSON."""
        for path in paths:
            r = await self.request("GET", path, params=params)
            if r.status_code == 404 and path != paths[-1]:
                continue
            return self._decode(path, r)
        raise AssertionError("unreachable")

    async def post_json(self, *paths: str, json: dict | None = None, data: dict | None = None):
        for path in paths:
            r = await self.request("POST", path, json=json, data=data)
            if r.status_code == 404 and path != paths[-1]:
                continue
            return self._decode(path, r)
        raise AssertionError("unreachable")

    def _decode(self, path: str, r: httpx.Response):
        if r.status_code in (401, 403):
            raise ApiError(path, f"HTTP {r.status_code}{self.denied_hint}", r.status_code)
        if r.status_code == 404:
            raise ApiError(path, "endpoint not found on this version", 404)
        if r.status_code >= 400:
            raise ApiError(path, f"HTTP {r.status_code} {r.text[:160]}".rstrip(), r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError as e:
            raise ApiError(path, f"invalid JSON in response ({len(r.content)} bytes): {e}",
                           r.status_code) from e

    # --- HTTP/1.0 fallback --------------------------------------------------------
    def _ssl_context(self) -> ssl.SSLContext:
        if self.verify is False:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if isinstance(self.verify, str):
            return ssl.create_default_context(cafile=self.verify)
        return ssl.create_default_context()

    async def _get_http10(self, base: str, path: str, params: dict | None) -> httpx.Response:
        """A bare HTTP/1.0 GET: the body runs to the end of the connection, so a server's
        chunked-encoding bugs can't corrupt it. Connects directly (no proxy). Raises the
        same httpx exceptions as a normal request."""
        url = httpx.URL(base + path, params=params)
        request = httpx.Request("GET", url)
        port = url.port or (443 if url.scheme == "https" else 80)
        ctx = self._ssl_context() if url.scheme == "https" else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(url.host, port, ssl=ctx, server_hostname=url.host if ctx else None),
                self.timeout.connect)
        except TimeoutError as e:
            raise httpx.ConnectTimeout(str(e) or "connect timed out", request=request) from e
        except OSError as e:
            raise httpx.ConnectError(str(e), request=request) from e
        try:
            lines = [f"GET {url.raw_path.decode('ascii')} HTTP/1.0", f"Host: {url.netloc.decode('ascii')}",
                     "Connection: close", *(f"{k}: {v}" for k, v in self.headers.items())]
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(MAX_BODY), self.timeout.read)
        except TimeoutError as e:
            raise httpx.ReadTimeout(str(e) or "read timed out", request=request) from e
        except OSError as e:
            raise httpx.ReadError(str(e), request=request) from e
        finally:
            writer.close()
        head, sep, body = raw.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if not sep or len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise httpx.RemoteProtocolError(f"not an HTTP response: {status_line[:60]!r}", request=request)
        headers = [tuple(h.split(":", 1)) for h in header_lines if ":" in h]
        headers = [(k.strip(), v.strip()) for k, v in headers
                   if k.strip().lower() not in ("transfer-encoding", "content-encoding", "content-length")]
        return httpx.Response(int(parts[1]), headers=headers, content=body, request=request)

    async def close(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()


class PollRun:
    """One poll of one API. Wraps each request so that a failure is recorded instead of
    raised, a failed section falls back to its last good value for a while, and at the end
    the source's status says whether the API was reachable at all."""

    def __init__(self, api: ApiClient, raw, last_good) -> None:
        self.api, self.raw, self.last_good = api, raw, last_good
        self.name = api.status.name
        self.errors: list[str] = []
        self.started = time.monotonic()
        self.unreachable = 0        # requests that got no HTTP answer at all
        self.why_unreachable = ""
        self.stale = False

    async def __call__(self, label: str, coro, *, keep: bool = True):
        try:
            value = await coro
        except Exception as e:  # noqa: BLE001
            if isinstance(e, ApiError) and e.transport:
                self.unreachable += 1
                self.why_unreachable = self.why_unreachable or str(e)
            self.errors.append(f"{self.name} {label}: {e}")
            value = self.last_good.get(label) if keep else None
            if value is not None:
                self.stale = True
            return value
        if keep and value is not None:
            self.last_good.put(label, value)
        return value

    def note(self, label: str, error: Exception) -> None:
        """Record an error from a request made outside __call__ (e.g. a background refresh)."""
        self.errors.append(f"{self.name} {label}: {error}")

    def finish(self) -> None:
        st = self.api.status
        st.ok = self.api.last_answer >= self.started or not self.unreachable
        st.stale = self.stale
        if st.ok:
            st.last_ok = time.time()
        if not st.ok:
            # One line instead of one per endpoint, all saying the same thing.
            st.error = self.why_unreachable.split(": ", 1)[-1]
            when = f"; showing data from {time.time() - st.last_ok:.0f}s ago" if (
                self.stale and st.last_ok) else ""
            self.raw.errors.append(f"{self.name}: {st.error}{when}")
        else:
            st.error = self.errors[0] if self.errors else ""
            self.raw.errors.extend(self.errors)
        self.raw.sources[self.name] = SourceStatus(**vars(st))
