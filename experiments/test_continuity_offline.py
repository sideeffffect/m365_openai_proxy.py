#!/usr/bin/env python3
"""
test_continuity_offline.py -- exercises the REAL HTTP handler code path
(`_plan_chat_turn`, `ConversationSessionStore`, `_run_plain_turn`,
`_stream_plain_turn`, `_render_continuation_delta`, `_conversation_fingerprint`)
end-to-end over a real local HTTP server, with `run_chat_turn` monkeypatched
to a fake, network-free stand-in -- so this validates the wiring added by
the Sydney-native conversation continuity feature WITHOUT spending any of
Sydney's own live quota (unlike experiments/probe_conversation_reuse.py,
which needs the real backend).

NOT part of the shipped proxy; not a pytest suite (this project is
stdlib-only end to end, including its own testing, on principle) -- just a
script that asserts and prints PASS/FAIL. Exits non-zero on any failure.

    python3 experiments/test_continuity_offline.py
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import m365_openai_proxy as proxy  # noqa: E402

FAILURES = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


class FakeAuth:
    oid = "fake-oid-1234"
    tid = "fake-tid-5678"
    access_token = "fake-access-token"
    expires_at = time.time() + 3600


class FakeTokenCache:
    """Stands in for the real TokenCache -- returns a fixed FakeAuth with no
    network call at all (not even to Entra ID)."""

    def get(self):
        return FakeAuth()


# calls made through the (monkeypatched) run_chat_turn: [(text, conversation_id), ...]
CALLS = []
# conversation_ids that should simulate a failed Chathub turn
FAIL_CONVERSATION_IDS = set()


def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
    CALLS.append((text, conversation_id))
    if conversation_id in FAIL_CONVERSATION_IDS:
        raise proxy.ProtocolError("simulated Chathub failure for this conversation_id")
    yield f"echo:{text}"


def post(port, body):
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


def main():
    proxy.run_chat_turn = fake_run_chat_turn  # module-level monkeypatch, no network

    conversation_sessions = proxy.ConversationSessionStore()
    handler_cls = proxy.make_handler(FakeTokenCache(), conversation_sessions)
    server = proxy._LoggingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _run_tests(port, conversation_sessions)
    finally:
        server.shutdown()
        server.server_close()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")


def _run_tests(port, conversation_sessions):
    # --- Turn 1: brand-new conversation, single user message ---
    CALLS.clear()
    status, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    check("turn 1: HTTP 200", status == 200)
    check("turn 1: exactly one Chathub call made", len(CALLS) == 1)
    check(
        "turn 1: full (unmodified) text sent, not a delta", CALLS[0][0] == "hello there"
    )
    reply1 = body["choices"][0]["message"]["content"]
    check("turn 1: reply content is the echoed text", reply1 == "echo:hello there")
    conversation_id_1 = CALLS[0][1]
    check("turn 1: a conversation_id was minted", bool(conversation_id_1))

    # --- Turn 2: client appends the assistant reply + a new user message,
    # exactly like a real OpenAI-style client would -- should be recognized
    # as a Sydney-native continuation of turn 1's conversation. ---
    CALLS.clear()
    messages_2 = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": reply1},
        {"role": "user", "content": "and now the second thing"},
    ]
    status, body = post(port, {"model": "m365-copilot", "messages": messages_2})
    check("turn 2: HTTP 200", status == 200)
    check("turn 2: exactly one Chathub call made", len(CALLS) == 1)
    check(
        "turn 2: ONLY the newest message was sent (not the whole transcript)",
        CALLS[0][0] == "and now the second thing",
    )
    check(
        "turn 2: reused turn 1's conversation_id (Sydney-native continuation)",
        CALLS[0][1] == conversation_id_1,
    )
    reply2 = body["choices"][0]["message"]["content"]

    # --- Turn 3: continues turn 2 the same way -- should keep reusing the
    # same conversation_id, proving this isn't a one-shot special case. ---
    CALLS.clear()
    messages_3 = messages_2 + [
        {"role": "assistant", "content": reply2},
        {"role": "user", "content": "a third thing"},
    ]
    status, body = post(port, {"model": "m365-copilot", "messages": messages_3})
    check(
        "turn 3: still reusing the same conversation_id",
        CALLS[0][1] == conversation_id_1,
    )
    check("turn 3: only the newest message sent", CALLS[0][0] == "a third thing")

    # --- Branched history: same prefix as turn 2, but a DIFFERENT final
    # message than what turn 2 actually used to extend the store -- must
    # NOT be treated as a continuation of turn 3's session (which was keyed
    # off turn 2's reply, not turn 1's), so falls back to full context
    # stuffing under a brand-new conversation_id. ---
    CALLS.clear()
    messages_branch = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": reply1},
        {"role": "user", "content": "a DIFFERENT second thing entirely"},
    ]
    status, body = post(port, {"model": "m365-copilot", "messages": messages_branch})
    check(
        "branch: falls back to a brand-new conversation_id",
        CALLS[0][1] != conversation_id_1,
    )
    check(
        "branch: full context-stuffed prompt sent (not just the delta)",
        "hello there" in CALLS[0][0] and "DIFFERENT" in CALLS[0][0],
    )

    # --- tools present: must never take the continuation path even though
    # the message history exactly extends turn 1. ---
    CALLS.clear()
    status, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": reply1},
                {"role": "user", "content": "and now the second thing"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "gets the weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
    )
    check(
        "tools present: never a continuation (fresh conversation_id, full prompt)",
        CALLS[0][1] != conversation_id_1 and "hello there" in CALLS[0][0],
    )

    # --- Failure recovery: a continuation turn whose Chathub call fails is
    # retried once as a brand-new, fully context-stuffed conversation. Needs
    # its OWN fresh conversation (not one of the earlier ones above) since
    # ConversationSessionStore entries are single-use -- messages_3's
    # continuation point was already consumed by turn 3 itself. ---
    CALLS.clear()
    status, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "recovery seed"}],
        },
    )
    recovery_reply = body["choices"][0]["message"]["content"]
    recovery_conversation_id = CALLS[0][1]

    CALLS.clear()
    FAIL_CONVERSATION_IDS.add(recovery_conversation_id)
    status, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [
                {"role": "user", "content": "recovery seed"},
                {"role": "assistant", "content": recovery_reply},
                {"role": "user", "content": "recovery follow-up"},
            ],
        },
    )
    check("failure recovery: HTTP 200 despite the underlying failure", status == 200)
    check(
        "failure recovery: exactly two Chathub calls (failed + retry)", len(CALLS) == 2
    )
    check(
        "failure recovery: first attempt was the (failing) continuation",
        CALLS[0][1] == recovery_conversation_id,
    )
    check(
        "failure recovery: retry used a brand-new conversation_id",
        len(CALLS) == 2 and CALLS[1][1] != recovery_conversation_id,
    )
    check(
        "failure recovery: retry sent the full context-stuffed transcript",
        len(CALLS) == 2 and "recovery seed" in CALLS[1][0],
    )
    FAIL_CONVERSATION_IDS.discard(recovery_conversation_id)

    # --- --disable-conversation-continuity equivalent: conversation_sessions=None
    # must make every request take the fresh/full-context-stuffing path. ---
    CALLS.clear()
    handler_cls_disabled = proxy.make_handler(FakeTokenCache(), None)
    server2 = proxy._LoggingHTTPServer(("127.0.0.1", 0), handler_cls_disabled)
    port2 = server2.server_address[1]
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t2.start()
    try:
        status, body = post(
            port2,
            {
                "model": "m365-copilot",
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )
        first_id = CALLS[0][1]
        CALLS.clear()
        status, body = post(
            port2,
            {
                "model": "m365-copilot",
                "messages": [
                    {"role": "user", "content": "hello there"},
                    {"role": "assistant", "content": "echo:hello there"},
                    {"role": "user", "content": "second"},
                ],
            },
        )
        check(
            "--disable-conversation-continuity: never reuses a conversation_id",
            CALLS[0][1] != first_id,
        )
        check(
            "--disable-conversation-continuity: always full context-stuffing",
            "hello there" in CALLS[0][0],
        )
    finally:
        server2.shutdown()
        server2.server_close()

    # --- ConversationSessionStore unit checks (no HTTP involved) ---
    store = proxy.ConversationSessionStore()
    fp_a = proxy._conversation_fingerprint([{"role": "user", "content": "x"}])
    fp_b = proxy._conversation_fingerprint([{"role": "user", "content": "y"}])
    check("fingerprint: different content -> different fingerprint", fp_a != fp_b)
    check(
        "fingerprint: identical content -> identical fingerprint",
        fp_a == proxy._conversation_fingerprint([{"role": "user", "content": "x"}]),
    )
    store.remember(fp_a, "conv-a", "oid-1")
    check(
        "store: lookup right after remember succeeds",
        store.lookup(fp_a, "oid-1") is not None,
    )
    check(
        "store: lookup with a different oid misses", store.lookup(fp_a, "oid-2") is None
    )
    store.forget(fp_a)
    check("store: lookup after forget misses", store.lookup(fp_a, "oid-1") is None)
    check("store: forget(None) is a harmless no-op", store.forget(None) is None)


if __name__ == "__main__":
    main()
