"""Offline pytest coverage for the RFC 6455 handshake.

Regression coverage for a real-world failure. The `Sec-WebSocket-Accept`
computation used to be inlined in `WebSocketClient.__init__`, reachable only
by opening a real TLS connection -- so no test, on any interpreter, ever ran
it. That let a Python-3.9-only spelling of the call (`hashlib.sha1(...,
usedforsecurity=False)`) ship and break *every chat turn* on Python 3.7/3.8
with `TypeError: openssl_sha1() takes no keyword arguments`, while
byte-compilation, `--help`, and the entire offline suite stayed green.

The computation now lives in `websocket_accept_value()`, and the handshake
itself is driven here against an in-memory fake socket -- no TLS, no threads,
no network -- so both the happy path and the rejection path are exercised on
every interpreter CI runs pytest on. The same accept vector is additionally
checked by `selfcheck.py`, which is what covers Python 3.7 (where current
pytest cannot be installed at all).
"""

import pytest

import m365_openai_proxy as proxy

# RFC 6455 section 1.3's worked example.
RFC6455_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC6455_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


class _FakeSocket:
    """Answers a WebSocket upgrade request entirely in memory."""

    def __init__(self, accept=None, status="HTTP/1.1 101 Switching Protocols"):
        self._accept = accept
        self._status = status
        self._sent = b""
        self._pending = b""
        self.closed = False

    def sendall(self, data):
        self._sent += data
        if self._pending or b"\r\n\r\n" not in self._sent:
            return
        key = ""
        for line in self._sent.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = (
            self._accept
            if self._accept is not None
            else proxy.websocket_accept_value(key)
        )
        self._pending = (
            self._status + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + accept + "\r\n\r\n"
        ).encode()

    def recv(self, n):
        chunk, self._pending = self._pending[:n], self._pending[n:]
        return chunk

    def settimeout(self, _):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_transport(monkeypatch):
    """Make WebSocketClient connect to an in-memory socket instead of the net."""
    created = []

    def _install(sock):
        created.append(sock)

        class _Ctx:
            def wrap_socket(self, _raw, server_hostname=None):
                return sock

        monkeypatch.setattr(proxy.socket, "create_connection", lambda *a, **k: sock)
        monkeypatch.setattr(proxy.ssl, "create_default_context", _Ctx)
        return sock

    return _install


def test_accept_value_matches_rfc6455_vector():
    assert proxy.websocket_accept_value(RFC6455_KEY) == RFC6455_ACCEPT


def test_accept_value_takes_no_version_specific_kwargs():
    """Guards the actual 3.7/3.8 breakage: the call must stay portable.

    `hashlib.sha1(..., usedforsecurity=False)` raises TypeError on Python
    < 3.9, so a plain call that returns the right answer is the invariant.
    """
    assert proxy.websocket_accept_value("") == proxy.websocket_accept_value("")
    assert len(proxy.websocket_accept_value(RFC6455_KEY)) == 28  # base64 of 20 bytes


def test_handshake_accepts_a_correct_accept_header(fake_transport):
    fake_transport(_FakeSocket())
    ws = proxy.WebSocketClient("wss://example.invalid/chathub")
    assert ws.sock is not None


def test_handshake_rejects_a_tampered_accept_header(fake_transport):
    fake_transport(_FakeSocket(accept="AAAAAAAAAAAAAAAAAAAAAAAAAAA="))
    with pytest.raises(proxy.WSError, match="Sec-WebSocket-Accept did not match"):
        proxy.WebSocketClient("wss://example.invalid/chathub")


def test_handshake_rejects_a_non_101_status(fake_transport):
    fake_transport(_FakeSocket(status="HTTP/1.1 403 Forbidden"))
    with pytest.raises(proxy.WSError, match="handshake failed: HTTP 403"):
        proxy.WebSocketClient("wss://example.invalid/chathub")


def test_only_wss_is_accepted():
    with pytest.raises(ValueError, match="only wss"):
        proxy.WebSocketClient("ws://example.invalid/chathub")
