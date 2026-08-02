"""Offline pytest suite for the MCP (Model Context Protocol) endpoint at /mcp.

Drives the proxy's REAL HTTP handler over the Streamable HTTP transport with
`run_chat_turn` stubbed, so nothing here needs live credentials or spends any
Sydney quota. The end-to-end behavior these assert was verified live on
2026-08-02 (initialize / tools/list / tools/call for both tools, plus the
error paths). See REVERSE_ENGINEERING.md / README for the surface.
"""

import base64
import json
import struct
import threading
import urllib.error
import urllib.request
import zlib

import pytest

import m365_openai_proxy as proxy


def _make_png(rgb=(30, 60, 200), w=16, h=16):
    """A minimal valid solid-color PNG, pure stdlib."""
    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(typ, data):
        c = typ + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _data_uri(png_bytes):
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


def _mcp(port, body, raw=False):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            if raw:
                return resp.status, data
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        err = e.read()
        return e.code, (json.loads(err) if err else None)


@pytest.fixture
def mcp_port(run_server, fake_token_cache):
    return run_server(fake_token_cache, None)


@pytest.fixture
def stub_chat(monkeypatch):
    """Replaces run_chat_turn with a capturing stub that yields a fixed reply.
    Returns the `calls` list so a test can assert what was passed."""
    calls = []

    def fake_run_chat_turn(
        token_cache, prompt, conversation_id=None, images=None, **kw
    ):
        calls.append({"prompt": prompt, "images": images})
        yield "STUB REPLY"

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return calls


# ---------------------------------------------------------------------------
# handshake / discovery
# ---------------------------------------------------------------------------


def test_initialize_echoes_protocol_version(mcp_port):
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert status == 200
    res = r["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"]["tools"] == {"listChanged": False}
    assert res["serverInfo"] == {
        "name": "m365-copilot-proxy",
        "version": proxy.PROXY_VERSION,
    }
    assert "instructions" in res


def test_initialize_defaults_protocol_version_when_absent(mcp_port):
    _status, r = _mcp(
        mcp_port, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert r["result"]["protocolVersion"] == proxy.MCP_PROTOCOL_VERSION


def test_notification_gets_202_and_no_body(mcp_port):
    status, body = _mcp(
        mcp_port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, raw=True
    )
    assert status == 202
    assert body == b""


def test_ping(mcp_port):
    status, r = _mcp(mcp_port, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert status == 200
    assert r["result"] == {}


def test_tools_list_shape(mcp_port):
    status, r = _mcp(mcp_port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"ask_copilot", "describe_image", "generate_image"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert "description" in t
    assert tools["ask_copilot"]["inputSchema"]["required"] == ["prompt"]
    assert tools["describe_image"]["inputSchema"]["required"] == ["image"]


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


def test_call_ask_copilot(mcp_port, stub_chat):
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "ask_copilot", "arguments": {"prompt": "hi there"}},
        },
    )
    assert status == 200
    res = r["result"]
    assert res["isError"] is False
    assert res["content"] == [{"type": "text", "text": "STUB REPLY"}]
    assert stub_chat[-1]["prompt"] == "hi there"
    assert stub_chat[-1]["images"] is None


def test_call_describe_image_passes_image(mcp_port, stub_chat):
    uri = _data_uri(_make_png())
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "describe_image",
                "arguments": {"image": uri, "prompt": "what color?"},
            },
        },
    )
    assert status == 200
    assert r["result"]["isError"] is False
    call = stub_chat[-1]
    assert call["prompt"] == "what color?"
    assert len(call["images"]) == 1
    assert call["images"][0]["data_uri"] == uri


def test_call_describe_image_defaults_prompt(mcp_port, stub_chat):
    _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "describe_image",
                "arguments": {"image": _data_uri(_make_png())},
            },
        },
    )
    assert stub_chat[-1]["prompt"] == "What is in this image?"


def test_call_unknown_tool_is_jsonrpc_error(mcp_port, stub_chat):
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        },
    )
    assert status == 200
    assert r["error"]["code"] == -32602
    assert "nope" in r["error"]["message"]


def test_call_missing_prompt_is_iserror_result(mcp_port, stub_chat):
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "ask_copilot", "arguments": {}},
        },
    )
    assert status == 200
    assert r["result"]["isError"] is True
    assert "prompt" in r["result"]["content"][0]["text"]


def test_call_bad_image_is_iserror_result(mcp_port, stub_chat):
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "describe_image",
                "arguments": {"image": "https://example.com/x.png"},
            },
        },
    )
    assert status == 200
    assert r["result"]["isError"] is True
    assert "data:" in r["result"]["content"][0]["text"]


def test_upstream_throttle_is_iserror_result(mcp_port, monkeypatch):
    def boom(*a, **k):
        raise proxy.ThrottledError("slow down")
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(proxy, "run_chat_turn", boom)
    status, r = _mcp(
        mcp_port,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "ask_copilot", "arguments": {"prompt": "x"}},
        },
    )
    assert status == 200
    assert r["result"]["isError"] is True


def test_unknown_method_is_jsonrpc_error(mcp_port):
    status, r = _mcp(mcp_port, {"jsonrpc": "2.0", "id": 7, "method": "bogus/method"})
    assert status == 200
    assert r["error"]["code"] == -32601


def test_non_object_body_is_rejected(mcp_port):
    status, r = _mcp(mcp_port, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert status == 400
    assert r["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# routing: GET 405, and --disable-mcp -> 404
# ---------------------------------------------------------------------------


def test_get_mcp_returns_405(mcp_port):
    req = urllib.request.Request(f"http://127.0.0.1:{mcp_port}/mcp", method="GET")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 405")
    except urllib.error.HTTPError as e:
        assert e.code == 405
        assert e.headers.get("Allow") == "POST"


def test_disable_mcp_routes_to_404(fake_token_cache):
    handler_cls = proxy.make_handler(fake_token_cache, None, mcp_enabled=False)
    server = proxy._LoggingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        status, _r = _mcp(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
