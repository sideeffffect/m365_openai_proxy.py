#!/usr/bin/env python3
"""LIVE probe: which capabilities does Sydney actually have/advertise?

NOT part of the shipped proxy. Answers "what tools does Sydney offer?"
empirically rather than by asking it (models confabulate their own tool
lists), by driving one turn per suspected capability and recording what
actually fires ON THE WIRE:

  - every SignalR server->client `target` seen (a target other than
    `update`/`Metrics` would be Sydney trying to call something ON the client
    -- e.g. the `mcp_discover`/`mcp_describe`/`invoke_local_plugin` Local MCP
    bridge documented in REVERSE_ENGINEERING.md);
  - every bot `messageType` seen (`InternalSearchQuery` = it really ran a
    search, `GeneratedCode` = it really ran code, etc.);
  - the actual internal search queries it issued, which is direct evidence of
    a retrieval tool being invoked rather than the answer being generated;
  - whether the reply carried `sourceAttributions` (citations), and of what
    kind -- the discriminator between "answered from the model" and
    "answered from a grounded lookup";
  - any frame key anywhere that looks capability-related.

Each prompt runs in its OWN fresh ConversationId so capabilities can't leak
between probes.

Usage: python3 scripts/probe_sydney_capabilities.py [credentials-prefix]
Writes full raw frames to /tmp/sydney_caps_frames.jsonl for later analysis;
prints only a compact per-probe summary.
"""

import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import m365_openai_proxy as proxy

logging.basicConfig(level=logging.WARNING)

FRAME_DUMP = "/tmp/sydney_caps_frames.jsonl"

#: One probe per suspected capability. `key` labels it; `prompt` is worded to
#: force the capability rather than invite a chatty answer about it.
PROBES = [
    ("web_search", "What is the top headline on the BBC News website right now?"),
    (
        "code_interpreter",
        "Run Python code to compute the sum of all primes below 1000, and show me the code you ran.",
    ),
    (
        "mail",
        "Summarize the subject lines of my three most recent emails in my mailbox.",
    ),
    (
        "files",
        "List three files I have recently worked on in my OneDrive or SharePoint.",
    ),
    ("calendar", "What meetings are on my Outlook calendar this week?"),
    ("people", "Who is my manager according to my organization's directory?"),
    ("image_gen", "Generate an image of a red bicycle."),
    (
        "self_report",
        "List the names of every tool, plugin or capability you can invoke. Names only, as a list.",
    ),
]

CAPABILITY_KEY_HINTS = (
    "plugin",
    "tool",
    "capabilit",
    "mcp",
    "action",
    "invoke",
    "skill",
    "agent",
    "connector",
)


def walk_keys(obj, out, prefix=""):
    """Collects every key path in a nested structure, so a capability-ish key
    this project has never seen still shows up."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(prefix + k)
            walk_keys(v, out, prefix + k + ".")
    elif isinstance(obj, list):
        for v in obj[:20]:
            walk_keys(v, out, prefix)


def run_probe(token_cache, label, prompt, dump):
    auth = token_cache.get()
    ws, session_id = proxy.open_chathub(auth)
    targets = {}
    msg_types = {}
    search_queries = []
    attributions = []
    answer = []
    all_keys = set()
    generated_code = []
    try:
        proxy.send_chat_message(ws, session_id, prompt)
        buf = proxy.SignalRBuffer()
        deadline = time.time() + 90
        while time.time() < deadline:
            raw = ws.recv_text()
            if raw is None:
                break
            if not raw:
                continue
            for frame in buf.feed(raw):
                dump.write(json.dumps({"probe": label, "frame": frame}) + "\n")
                walk_keys(frame, all_keys)
                ftype = frame.get("type")
                if ftype == 1:
                    tgt = frame.get("target")
                    targets[tgt] = targets.get(tgt, 0) + 1
                    if tgt != "update":
                        continue
                    for arg in frame.get("arguments") or []:
                        for msg in arg.get("messages") or []:
                            if msg.get("author") != "bot":
                                continue
                            mt = msg.get("messageType")
                            msg_types[str(mt)] = msg_types.get(str(mt), 0) + 1
                            text = msg.get("text") or ""
                            if mt == "InternalSearchQuery":
                                search_queries.append(text[:160])
                            elif mt == "GeneratedCode":
                                generated_code.append(text[:200])
                            elif mt in proxy.ANSWER_MESSAGE_TYPES:
                                answer.append(text)
                            for sa in msg.get("sourceAttributions") or []:
                                attributions.append(
                                    {
                                        "providerDisplayName": sa.get(
                                            "providerDisplayName"
                                        ),
                                        "seeMoreUrl": (sa.get("seeMoreUrl") or "")[
                                            :120
                                        ],
                                        "searchQuery": sa.get("searchQuery"),
                                    }
                                )
                elif ftype in (3, 7):
                    raise StopIteration
    except StopIteration:
        pass
    finally:
        ws.close()

    final = max(answer, key=len) if answer else ""
    return {
        "probe": label,
        "targets": targets,
        "messageTypes": msg_types,
        "searchQueries": search_queries[:6],
        "generatedCode": generated_code[:2],
        "attributionCount": len(attributions),
        "attributions": attributions[:5],
        "capabilityKeys": sorted(
            k for k in all_keys if any(h in k.lower() for h in CAPABILITY_KEY_HINTS)
        ),
        "answerLen": len(final),
        "answer": final[:700],
    }


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else "m365_openai_proxy"
    store = proxy.CredentialStore(prefix)
    token_cache = proxy.TokenCache(store)
    token_cache.get()
    only = os.environ.get("PROBES")
    probes = [p for p in PROBES if not only or p[0] in only.split(",")]
    with open(FRAME_DUMP, "w") as dump:
        for label, prompt in probes:
            try:
                result = run_probe(token_cache, label, prompt, dump)
            except Exception as e:  # noqa: BLE001 - a probe failing must not stop the rest
                result = {"probe": label, "error": f"{type(e).__name__}: {e}"}
            print(json.dumps(result))
            sys.stdout.flush()
            time.sleep(4)  # be gentle: account-level throttle after bursts


if __name__ == "__main__":
    main()
