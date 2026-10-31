"""GET a public web page without letting the URL reach inside the network.

Every address the host resolves to must be public (no loopback, private,
link-local, reserved or multicast range), and the connection goes to the
address that was checked, so a second DNS answer cannot swap in an internal
one. TLS still verifies the certificate against the URL's host name. Redirects
are not followed: a redirect is an answer, not a fetch.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urlsplit

TIMEOUT_S = 30
MAX_BYTES = 5_000_000
_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Response:
    status_code: int
    text: str
    headers: dict[str, str]


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> str:
    """The address to connect to; raises ValueError when any answer is not public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"{host} does not resolve ({e})") from None
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise ValueError(f"{host} does not resolve")
    blocked = [a for a in addrs if not _public(a)]
    if blocked:
        raise ValueError(f"{host} resolves to a non-public address ({blocked[0]})")
    return addrs[0]


class _Pinned(http.client.HTTPSConnection):
    """An HTTP(S) connection to a fixed address, speaking to it as `host`."""

    def __init__(self, host: str, port: int, addr: str, tls: bool, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addr, self._tls = addr, tls

    def connect(self) -> None:
        sock = socket.create_connection((self._addr, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host) if self._tls else sock


def get(url: str, *, timeout: float = TIMEOUT_S, max_bytes: int = MAX_BYTES) -> Response:
    """GET `url`. Raises ValueError for a URL that may not be fetched."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _PORTS:
        raise ValueError(f"only http(s) URLs are fetched, not {scheme or 'a bare path'}")
    host = parts.hostname
    if not host:
        raise ValueError("the URL names no host")
    port = parts.port or _PORTS[scheme]
    addr = resolve_public(host, port)
    conn = _Pinned(host, port, addr, scheme == "https", timeout)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request("GET", path, headers={
            "Host": parts.netloc.rsplit("@", 1)[-1],
            "User-Agent": "meeseek/1 (+https://github.com/)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        })
        resp = conn.getresponse()
        body = resp.read(max_bytes)
        headers = {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()
    msg = Message()
    msg["content-type"] = headers.get("content-type", "text/html")
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return Response(status_code=resp.status, text=text, headers=headers)
