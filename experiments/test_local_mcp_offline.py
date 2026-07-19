#!/usr/bin/env python3
"""Offline (no-network) test for the Local MCP tool-calling bridge.

Drives the bridge end-to-end WITHOUT Sydney: it spawns the real
mock_mcp_server.py subprocess (a genuine stdio MCP server), builds a
LocalMCPHost over it, and feeds the host the exact server->client SignalR
invocation frames Sydney sends (mcp_discover / mcp_describe /
invoke_local_plugin), asserting that:

  * StdioMCPClient really talks JSON-RPC to the subprocess (initialize /
    tools/list / tools/call round-trip, secret value comes back from the
    child process);
  * the host's client-result envelopes match the shapes extracted from the
    officeweb client's own source (schema_version, correlation_id,
    response.status/message, and the JSON-encoded `payload`);
  * handle_frame() answers on the ws with a SignalR type:3 Completion keyed by
    the invocation's invocationId, carrying that envelope as `result`;
  * send_warmup() emits the LocalMcpDiscovery advertisement frame;
  * a bad server/tool/endpoint yields a status:"Fail" envelope, not a crash.

Run from the repo root:  python3 experiments/test_local_mcp_offline.py
"""

import importlib.util
import json
import os

spec = importlib.util.spec_from_file_location("proxy", "m365_openai_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

MOCK = os.path.join("experiments", "mock_mcp_server.py")
SERVER_ID = "mock"

FAILURES = []


def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


class FakeWS:
    """Captures whatever the bridge sends, splitting on the SignalR record
    separator so each sent frame is one decoded dict."""

    def __init__(self):
        self.frames = []

    def send_text(self, text):
        for part in text.split(proxy.SIGNALR_RS):
            if part:
                self.frames.append(json.loads(part))


def discover_frame(cid, inv_id="i1"):
    return {
        "type": 1,
        "target": "mcp_discover",
        "invocationId": inv_id,
        "arguments": [
            {"correlation_id": cid, "invocation": {"payload": json.dumps({})}}
        ],
    }


def describe_frame(cid, inv_id="i2"):
    return {
        "type": 1,
        "target": "mcp_describe",
        "invocationId": inv_id,
        "arguments": [
            {
                "correlation_id": cid,
                "invocation": {"payload": json.dumps({"server_ids": [SERVER_ID]})},
            }
        ],
    }


def invoke_frame(cid, method, endpoint, params=None, inv_id="i3"):
    return {
        "type": 1,
        "target": "invoke_local_plugin",
        "invocationId": inv_id,
        "arguments": [
            {
                "correlation_id": cid,
                "invocation": {
                    "payload": json.dumps({"method": method, "params": params or {}}),
                    "local_endpoint": endpoint,
                },
            }
        ],
    }


def envelope_of(ws_frame):
    """The type:3 Completion's `result` IS the client-result envelope."""
    return ws_frame["result"]


def main():
    client = proxy.StdioMCPClient(command="python3", args=[MOCK])
    server_host = proxy.LocalMCPHost({SERVER_ID: client})
    server_host.start()
    try:
        # --- StdioMCPClient direct round-trip ---
        tools = client.list_tools()
        check(
            "tools/list returns echo_secret",
            any(t.get("name") == "echo_secret" for t in tools),
        )
        result = client.call_tool("echo_secret", {"room": "Orion"})
        text = (result.get("content") or [{}])[0].get("text")
        check(
            "tools/call round-trips the secret from the subprocess",
            text == "SECRET-Orion",
        )

        # --- mcp_discover ---
        ws = FakeWS()
        handled = server_host.handle_frame(ws, discover_frame("c-disc"))
        check("discover: handle_frame returns True", handled is True)
        check(
            "discover: one type:3 completion sent",
            len(ws.frames) == 1 and ws.frames[0].get("type") == 3,
        )
        check(
            "discover: completion keyed by invocationId",
            ws.frames[0].get("invocationId") == "i1",
        )
        env = envelope_of(ws.frames[0])
        check(
            "discover: schema_version present",
            env.get("schema_version") == proxy.LOCAL_MCP_SCHEMA_VERSION,
        )
        check("discover: correlation_id echoed", env.get("correlation_id") == "c-disc")
        check(
            "discover: status Success",
            env.get("response", {}).get("status") == "Success",
        )
        disc_payload = json.loads(env["response"]["payload"])
        check(
            "discover: payload lists our server_id",
            disc_payload.get("server_ids") == [SERVER_ID],
        )

        # --- mcp_describe ---
        ws = FakeWS()
        server_host.handle_frame(ws, describe_frame("c-desc"))
        env = envelope_of(ws.frames[0])
        check(
            "describe: status Success",
            env.get("response", {}).get("status") == "Success",
        )
        desc_payload = json.loads(env["response"]["payload"])
        servers = desc_payload.get("servers") or []
        check("describe: one server described", len(servers) == 1)
        srv = servers[0] if servers else {}
        check("describe: server_id matches", srv.get("server_id") == SERVER_ID)
        check(
            "describe: tool schema forwarded (name+inputSchema)",
            any(
                t.get("name") == "echo_secret" and "inputSchema" in t
                for t in srv.get("tools", [])
            ),
        )

        # --- invoke_local_plugin (the real tool call) ---
        ws = FakeWS()
        server_host.handle_frame(
            ws,
            invoke_frame(
                "c-inv",
                method="mcp_echo_secret",  # "mcp" prefix + tool name (with underscore)
                endpoint=f"mcp://{SERVER_ID}",
                params={"room": "Vega"},
            ),
        )
        env = envelope_of(ws.frames[0])
        check(
            "invoke: status Success", env.get("response", {}).get("status") == "Success"
        )
        inv_payload = json.loads(env["response"]["payload"])
        entries = inv_payload.get("result") or []
        check("invoke: result carries one entry", len(entries) == 1)
        entry = entries[0] if entries else {}
        check("invoke: entry id == correlation_id", entry.get("id") == "c-inv")
        # `data` is the JSON-encoded MCP tool result -> decode and check secret
        data = json.loads(entry.get("data", "{}"))
        got = (data.get("content") or [{}])[0].get("text")
        check("invoke: tool's real return value round-tripped", got == "SECRET-Vega")

        # --- failure shapes: never crash, always a Fail envelope ---
        ws = FakeWS()
        server_host.handle_frame(
            ws,
            invoke_frame("c-bad-srv", method="mcp_echo_secret", endpoint="mcp://nope"),
        )
        env = envelope_of(ws.frames[0])
        check(
            "invoke unknown server -> Fail",
            env.get("response", {}).get("status") == "Fail",
        )

        ws = FakeWS()
        server_host.handle_frame(
            ws,
            invoke_frame(
                "c-bad-tool", method="mcp_no_such_tool", endpoint=f"mcp://{SERVER_ID}"
            ),
        )
        env = envelope_of(ws.frames[0])
        check(
            "invoke unknown tool -> Fail",
            env.get("response", {}).get("status") == "Fail",
        )

        # --- warmup advertisement ---
        ws = FakeWS()
        server_host.send_warmup(ws)
        check("warmup: one frame sent", len(ws.frames) == 1)
        wf = ws.frames[0] if ws.frames else {}
        arg = (wf.get("arguments") or [{}])[0]
        check("warmup: target 'send'", wf.get("target") == "send")
        check(
            "warmup: LocalMcpDiscovery annotation with our server_id",
            arg.get("type") == proxy.LOCAL_MCP_DISCOVERY_ANNOTATION
            and arg.get("serverIds") == [SERVER_ID],
        )

        # --- non-MCP frame is ignored by the bridge ---
        ws = FakeWS()
        handled = server_host.handle_frame(
            ws, {"type": 1, "target": "update", "arguments": []}
        )
        check(
            "non-MCP frame: handle_frame returns False (not handled)",
            handled is False and not ws.frames,
        )

    finally:
        server_host.stop()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        raise SystemExit(1)
    print("\nAll Local MCP bridge offline checks passed.")


if __name__ == "__main__":
    main()
