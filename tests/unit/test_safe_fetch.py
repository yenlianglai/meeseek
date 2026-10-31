"""A URL cannot make meeseek fetch from inside the network."""

import socket

import pytest

from meeseek.tools import safe_fetch


def _resolves_to(monkeypatch, *addrs):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo",
                        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs])


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8787/inbox_api/items",
                                 "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/",
                                 "http://[::1]/", "http://192.168.1.1/"])
def test_internal_addresses_are_refused(url):
    with pytest.raises(ValueError):
        safe_fetch.get(url)


def test_a_public_name_that_also_resolves_inside_is_refused(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34", "10.1.2.3")
    with pytest.raises(ValueError, match="non-public"):
        safe_fetch.get("https://example.com/")


def test_an_ipv4_mapped_internal_address_is_refused(monkeypatch):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo",
                        lambda host, port, type=0: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", port, 0, 0))])
    with pytest.raises(ValueError):
        safe_fetch.get("http://sneaky.example/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_only_http_is_fetched(url):
    with pytest.raises(ValueError, match="http"):
        safe_fetch.get(url)


def test_the_connection_goes_to_the_checked_address(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    dialed = []

    class Sock:
        def sendall(self, data):
            dialed.append(data)

        def makefile(self, mode, *a, **k):
            import io
            return io.BytesIO(b"HTTP/1.1 302 Found\r\nLocation: http://10.0.0.1/\r\nContent-Length: 2\r\n\r\nno")

        def close(self):
            pass

    monkeypatch.setattr(safe_fetch.socket, "create_connection", lambda addr, timeout=None: dialed.append(addr) or Sock())
    out = safe_fetch.get("http://example.com/a?b=1")
    assert dialed[0] == ("93.184.216.34", 80)
    assert b"GET /a?b=1 HTTP/1.1" in dialed[1] and b"Host: example.com" in dialed[1]
    assert out.status_code == 302 and out.text == "no"       # the redirect is reported, not followed
