#!/usr/bin/env python3
"""LIVE probe: can Sydney's Local MCP surface a client tool to the model?

Reverse-engineering (see REVERSE_ENGINEERING.md's "Local MCP tool-calling
bridge" section) found, by reading the officeweb client's own bundled JS, a
real mechanism by which Sydney invokes Model Context Protocol tools *through
the client*, over the same Chathub SignalR connection this proxy already
speaks:

  - the client advertises which local MCP servers it has by sending a
    fire-and-forget SignalR `send` invocation carrying a `LocalMcpDiscovery`
    annotation (`{type:"LocalMcpDiscovery", serverIds:[...],
    disableDescriptorCache:...}`) -- the browser's warmupLocalMCPPlugins();
  - Sydney then (server -> client) invokes `mcp_discover` / `mcp_describe`
    to enumerate + learn the tools, and `invoke_local_plugin` to actually
    call one, each as a SignalR Invocation the client answers via the
    "client results" feature (a type:3 Completion keyed by invocationId).

Earlier probe runs already CONFIRMED on the wire, for this tenant:
  * Local MCP is reachable: our warmup advertisement made Sydney send
    `mcp_describe` back with our server_ids.
  * The MCP invocation DOES carry an `invocationId` (client-results is real).
  * The advertisement shape works.
What did NOT work: the described tool never surfaced to the model (it was
absent from the model's own enumeration of its capabilities, and the model
refused, claiming no such tool). This run exhausts the remaining cheap
hypotheses for WHY the described tool isn't made available in the turn.

Env flags (all optional; default = closest-to-browser):
  PROBE_TRANSPORT=string|object   describe tool's server.transport shape
  PROBE_PLUGINS=1                 add a local-MCP entry to the chat plugins[]
  PROBE_SCP=1                    also send EnableMcpServerDynamicTools in the
                                  chat payload options (not just the WS URL)
  PROBE_NO_EXTRA=1               do NOT append the extra MCP variants to URL
  PROBE_PROMPT=invoke|<text>     override the user turn (default = enumerate)

NOT part of the shipped proxy. Run directly:  python3 experiments/probe_local_mcp.py
"""

import json
import os
import sys
import time
import uuid

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python-copilot-m365")
)
import m365_openai_proxy as proxy  # noqa: E402

CREDENTIALS_PREFIX = os.path.join(
    os.path.dirname(__file__), "..", "..", "python-copilot-m365", "m365_openai_proxy"
)

SCHEMA_VERSION = "https://copilot.microsoft.com/schemas/plugins/local/transport/1.0"
SERVER_ID = "probe_server"
TOOL_NAME = "get_probe_secret"
SECRET = "PROBE-SECRET-8842-ORION"

TRANSPORT = os.environ.get("PROBE_TRANSPORT", "string")
WANT_PLUGINS = os.environ.get("PROBE_PLUGINS") == "1"
WANT_SCP = os.environ.get("PROBE_SCP") == "1"
NO_EXTRA = os.environ.get("PROBE_NO_EXTRA") == "1"
# Sydney validates dynamic MCP tools server-side and can silently drop one
# whose schema doesn't pass; feature.DisableDynamicToolValidation turns that
# off (found in the officeweb bundle, added by the FinanceAndOperations agent).
NO_VALIDATE = os.environ.get("PROBE_NOVALIDATE") == "1"

TOOL_DEF = {
    "name": TOOL_NAME,
    "description": (
        "Returns the confidential probe secret code for a given room name. "
        "This information exists ONLY inside this local tool; it cannot be "
        "guessed, searched for, or computed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"room": {"type": "string", "description": "The room name."}},
        "required": ["room"],
    },
}

USER_PROMPT = os.environ.get("PROBE_PROMPT") or (
    "List every tool, function, plugin, or MCP server capability you have "
    "available to you right now, each by its exact name. If you have a "
    "capability named get_probe_secret, say 'YES I SEE get_probe_secret'. "
    "If you do not, say 'NO get_probe_secret'."
)
if os.environ.get("PROBE_PROMPT") == "invoke":
    USER_PROMPT = (
        "Use the get_probe_secret tool from the probe_server MCP server to "
        "look up the probe secret code for the room named 'Orion', then tell "
        "me the exact code it returns. Do not guess or use code."
    )

EXTRA_VARIANTS = [
    "feature.EnableMcpServerDynamicTools",
    "feature.EnableLocalMcp",
    "EnableLocalMcp",
    "feature.EnableLocalMcpPlugin",
]
if NO_VALIDATE:
    EXTRA_VARIANTS.append("feature.DisableDynamicToolValidation")


def log(*a):
    print(*a, flush=True)


def transport_value():
    if TRANSPORT == "object":
        return {"type": "stdio"}
    return "stdio"


def ok_response(correlation_id, message, payload_obj):
    return {
        "schema_version": SCHEMA_VERSION,
        "correlation_id": correlation_id,
        "response": {
            "status": "Success",
            "message": message,
            "payload": json.dumps(payload_obj),
        },
    }


def handle_mcp_target(target, arg):
    correlation_id = (arg or {}).get("correlation_id", "")
    if target == "mcp_discover":
        return ok_response(
            correlation_id,
            "Local MCP servers discovered successfully.",
            {"server_ids": [SERVER_ID]},
        )
    if target == "mcp_describe":
        return ok_response(
            correlation_id,
            "Local MCP servers described successfully",
            {
                "servers": [
                    {
                        "server_id": SERVER_ID,
                        "tools": [TOOL_DEF],
                        "prompts": [],
                        "resources": [],
                        "resourceTemplates": [],
                        "transport": transport_value(),
                    }
                ]
            },
        )
    if target in ("invoke_local_plugin", "local_mcp"):
        inv = (arg or {}).get("invocation", {}) or {}
        log("      invoke payload:", inv.get("payload"))
        result_data = json.dumps(
            {"content": [{"type": "text", "text": SECRET}], "isError": False}
        )
        return ok_response(
            correlation_id,
            f"Method {TOOL_NAME} invoked successfully.",
            {
                "result": [
                    {
                        "id": correlation_id,
                        "data": result_data,
                        "type": "text/plain",
                        "description_for_model": (
                            f"Tool invocation result for method {TOOL_NAME} "
                            f"on server {SERVER_ID}"
                        ),
                    }
                ],
                "jsonrpc": "2.0",
                "id": correlation_id,
            },
        )
    return None


def send_warmup(ws):
    frame = {
        "type": 1,
        "target": "send",
        "arguments": [
            {
                "type": "LocalMcpDiscovery",
                "serverIds": [SERVER_ID],
                "disableDescriptorCache": True,
            }
        ],
    }
    ws.send_text(json.dumps(frame) + proxy.SIGNALR_RS)
    log(">> warmup LocalMcpDiscovery:", json.dumps(frame["arguments"][0]))


def send_chat(ws, session_id, text):
    """Mirror proxy.send_chat_message but allow injecting a local-MCP plugins[]
    entry and sydneyConfigurationParameters variants, to test whether either
    is what makes the described tool available in the turn."""
    trace_id = str(uuid.uuid4())
    plugins = []
    if WANT_PLUGINS:
        plugins = [{"Id": SERVER_ID, "Source": "LocalMcp"}]
    options = {}
    arg = {
        "source": "officeweb",
        "clientCorrelationId": trace_id,
        "sessionId": session_id,
        "optionsSets": proxy.OPTIONS_SETS,
        "streamingMode": "ConciseWithPadding",
        "options": options,
        "extraExtensionParameters": {},
        "allowedMessageTypes": proxy.ALLOWED_MESSAGE_TYPES,
        "sliceIds": [],
        "threadLevelGptId": {},
        "traceId": trace_id,
        "isStartOfSession": False,
        "clientInfo": {
            "clientPlatform": "mcmcopilot-web",
            "clientAppName": "Office",
            "clientEntrypoint": "mcmcopilot-officeweb",
            "clientSessionId": session_id,
            "ProductCategory": "Chat",
            "clientAppType": "Web",
            "productEntryPoint": "ChatPanel",
            "deviceOS": "Linux",
            "deviceType": "Desktop",
            "clientPlatformVersion": "Unknown",
        },
        "message": {
            "author": "user",
            "inputMethod": "Keyboard",
            "text": text,
            "requestId": trace_id,
            "locale": "en-US",
            "messageType": "Chat",
            "experienceType": "Default",
            "adaptiveCards": [],
            "clientPreferences": {},
        },
        "plugins": plugins,
        "isSbsSupported": True,
        "tone": "Magic",
        "renderReferencesBehindEOS": True,
        "disconnectBehavior": "continue",
    }
    if WANT_SCP:
        scp_variants = ["feature.EnableMcpServerDynamicTools"]
        if NO_VALIDATE:
            scp_variants.append("feature.DisableDynamicToolValidation")
        arg["sydneyConfigurationParameters"] = {"variants": scp_variants}
    payload = {"type": 4, "target": "chat", "invocationId": "0", "arguments": [arg]}
    ws.send_text(json.dumps(payload) + proxy.SIGNALR_RS)
    log(f">> sent chat turn (plugins={plugins}, scp={WANT_SCP})")


def main():
    if not NO_EXTRA:
        base = proxy.CHATHUB_VARIANTS
        extra = ",".join(v for v in EXTRA_VARIANTS if v not in base)
        proxy.CHATHUB_VARIANTS = base + "," + extra if extra else base
    log(
        f"== config: transport={TRANSPORT} plugins={WANT_PLUGINS} scp={WANT_SCP} "
        f"no_extra={NO_EXTRA} =="
    )

    store = proxy.CredentialStore(CREDENTIALS_PREFIX)
    token_cache = proxy.TokenCache(store)
    auth = token_cache.get()
    ws, session_id = proxy.open_chathub(auth)

    saw = {
        "mcp_discover": None,
        "mcp_describe": None,
        "invoke_local_plugin": None,
        "local_mcp": None,
    }
    last_bot_text = ""
    chat_sent = False
    saw_invoke = False
    try:
        send_warmup(ws)
        buf = proxy.SignalRBuffer()
        deadline = time.time() + 120
        described_at = None
        while time.time() < deadline:
            if described_at and not chat_sent and time.time() - described_at > 1.2:
                send_chat(ws, session_id, USER_PROMPT)
                chat_sent = True
            ws.sock.settimeout(2.0)
            try:
                raw = ws.recv_text()
            except (TimeoutError, OSError):
                continue
            if raw is None:
                log("!! peer closed")
                break
            if not raw:
                continue
            for frame in buf.feed(raw):
                ftype = frame.get("type")
                target = frame.get("target")
                inv_id = frame.get("invocationId")
                if ftype == 1 and target in saw:
                    saw[target] = inv_id
                    args = frame.get("arguments") or [None]
                    log(f"\n*** {target!r} invocationId={inv_id!r}")
                    log("    arg:", json.dumps(args[0])[:900])
                    result = handle_mcp_target(target, args[0])
                    if inv_id is not None:
                        ws.send_text(
                            json.dumps(
                                {"type": 3, "invocationId": inv_id, "result": result}
                            )
                            + proxy.SIGNALR_RS
                        )
                        log(f"    <- type:3 completion for {inv_id!r}")
                    if target == "mcp_describe":
                        described_at = time.time()
                    if target in ("invoke_local_plugin", "local_mcp"):
                        saw_invoke = True
                elif ftype == 1 and target == "update":
                    for a in frame.get("arguments") or []:
                        for msg in a.get("messages") or []:
                            if msg.get("author") == "bot" and msg.get("text"):
                                last_bot_text = msg["text"]
                elif ftype == 1 and target not in ("update", "Metrics"):
                    log(
                        f"   (target={target!r} inv={inv_id!r} "
                        f"{json.dumps(frame.get('arguments'))[:300]})"
                    )
                elif ftype == 3 and chat_sent:
                    raise SystemExit(0)
                elif ftype == 7:
                    raise SystemExit(0)
    except SystemExit:
        pass
    finally:
        ws.close()

    surfaced = (
        saw_invoke
        or SECRET in last_bot_text
        or "YES I SEE get_probe_secret" in last_bot_text
    )
    log("\n================ RESULT ================")
    log(
        "describe seen:",
        saw["mcp_describe"] is not None,
        "| discover seen:",
        saw["mcp_discover"] is not None,
    )
    log("invoke_local_plugin seen:", saw_invoke)
    log("TOOL SURFACED TO MODEL:", surfaced)
    log("final bot text:", repr(last_bot_text[:600]))


if __name__ == "__main__":
    main()
