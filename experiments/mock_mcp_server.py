#!/usr/bin/env python3
"""A tiny, pure-stdlib Model Context Protocol server over stdio, for offline
testing of this proxy's Local MCP bridge (see test_local_mcp_offline.py). It
speaks just enough MCP -- newline-delimited JSON-RPC 2.0 -- to be driven by
StdioMCPClient: `initialize`, `notifications/initialized`, `tools/list`,
`tools/call`.

Exposes one tool, `echo_secret(room)`, returning "SECRET-<room>" so a test can
prove the value round-tripped through a real subprocess rather than being
fabricated. NOT part of the shipped proxy.
"""

import json
import sys

TOOLS = [
    {
        "name": "echo_secret",
        "description": "Return the secret code for a given room name.",
        "inputSchema": {
            "type": "object",
            "properties": {"room": {"type": "string"}},
            "required": ["room"],
        },
    }
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-mcp-server", "version": "0.1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            pass  # a notification: no response
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo_secret":
                room = args.get("room", "")
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"SECRET-{room}"}],
                            "isError": False,
                        },
                    }
                )
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"no such tool: {name}"},
                    }
                )
        elif req_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
            )


if __name__ == "__main__":
    main()
