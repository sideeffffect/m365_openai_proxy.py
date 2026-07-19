"""Shared pytest fixtures for the offline (network-free) proxy tests.

These import the shipped proxy as a module (`import m365_openai_proxy as proxy`)
and drive its REAL HTTP handler / TokenCache code paths with every network call
stubbed out, so nothing here needs live Microsoft credentials or spends any of
Sydney's live quota. The live, credential-requiring probes live under
`scripts/` and are intentionally NOT collected as tests.

`m365_openai_proxy` is importable because `[tool.pytest.ini_options]` in
`pyproject.toml` puts the repo root on `sys.path` (`pythonpath = ["."]`).
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import m365_openai_proxy as proxy


class FakeAuth:
    """A fixed, network-free stand-in for a redeemed access token."""

    oid = "fake-oid-1234"
    tid = "fake-tid-5678"
    access_token = "fake-access-token"
    expires_at = time.time() + 3600


class FakeTokenCache:
    """Stands in for the real TokenCache -- returns a fixed FakeAuth with no
    network call at all (not even to Entra ID)."""

    def get(self):
        return FakeAuth()


@pytest.fixture
def fake_token_cache():
    return FakeTokenCache()


@pytest.fixture
def run_server():
    """Factory fixture: `run_server(token_cache, sessions) -> port` starts the
    proxy's real HTTP server on an ephemeral port and tears every started
    server down at the end of the test."""
    servers = []

    def _start(token_cache, sessions):
        handler_cls = proxy.make_handler(token_cache, sessions)
        server = proxy._LoggingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server.server_address[1]

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def post():
    """Return a `post(port, body) -> (status, parsed_json)` helper for the
    non-streaming chat-completions endpoint."""

    def _post(port, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    return _post


@pytest.fixture
def raw_post():
    """Return a `raw_post(port, body) -> (status, raw_text)` helper -- keeps the
    body as raw text so streaming (SSE) responses can be parsed line by line."""

    def _raw_post(port, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    return _raw_post
