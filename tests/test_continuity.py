"""Offline pytest suite for the Sydney-native conversation-continuity wiring.

Exercises the REAL HTTP handler code path (`_plan_chat_turn`,
`ConversationSessionStore`, `_run_plain_turn`, `_stream_plain_turn`,
`_render_continuation_delta`, `_conversation_fingerprint`) end-to-end over a
real local HTTP server, with `run_chat_turn` monkeypatched to a fake,
network-free stand-in -- so this validates the wiring WITHOUT spending any of
Sydney's own live quota (unlike `scripts/probe_conversation_reuse.py`, which
needs the real backend).
"""

import pytest

import m365_openai_proxy as proxy


@pytest.fixture
def recorder(monkeypatch):
    """Monkeypatch `run_chat_turn` to a network-free echo stub that records
    every call as `(text, conversation_id)` and can be told to fail for a
    given conversation_id. Returns a small handle exposing `.calls` (list) and
    `.fail_ids` (set)."""

    class Recorder:
        def __init__(self):
            self.calls = []
            self.fail_ids = set()

    rec = Recorder()

    def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
        rec.calls.append((text, conversation_id))
        if conversation_id in rec.fail_ids:
            raise proxy.ProtocolError(
                "simulated Chathub failure for this conversation_id"
            )
        yield f"echo:{text}"

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return rec


def test_new_conversation_sends_full_text(recorder, fake_token_cache, run_server, post):
    port = run_server(fake_token_cache, proxy.ConversationSessionStore())
    status, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    assert status == 200
    assert len(recorder.calls) == 1
    # full (unmodified) text is sent, not a delta, and a conversation_id is minted
    assert recorder.calls[0][0] == "hello there"
    assert recorder.calls[0][1]
    assert body["choices"][0]["message"]["content"] == "echo:hello there"


def test_continuation_reuses_conversation_id(
    recorder, fake_token_cache, run_server, post
):
    sessions = proxy.ConversationSessionStore()
    port = run_server(fake_token_cache, sessions)

    _, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    conversation_id_1 = recorder.calls[0][1]
    reply1 = body["choices"][0]["message"]["content"]

    # Client appends the assistant reply + a new user message, exactly like a
    # real OpenAI-style client -- should be recognized as a native continuation.
    recorder.calls.clear()
    messages_2 = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": reply1},
        {"role": "user", "content": "and now the second thing"},
    ]
    _, body = post(port, {"model": "m365-copilot", "messages": messages_2})
    assert len(recorder.calls) == 1
    # ONLY the newest message is sent (not the whole transcript), same conv id
    assert recorder.calls[0][0] == "and now the second thing"
    assert recorder.calls[0][1] == conversation_id_1
    reply2 = body["choices"][0]["message"]["content"]

    # Turn 3 continues turn 2 the same way -- proves it isn't a one-shot case.
    recorder.calls.clear()
    messages_3 = messages_2 + [
        {"role": "assistant", "content": reply2},
        {"role": "user", "content": "a third thing"},
    ]
    post(port, {"model": "m365-copilot", "messages": messages_3})
    assert recorder.calls[0][1] == conversation_id_1
    assert recorder.calls[0][0] == "a third thing"


def test_branch_falls_back_to_new_conversation(
    recorder, fake_token_cache, run_server, post
):
    sessions = proxy.ConversationSessionStore()
    port = run_server(fake_token_cache, sessions)

    _, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    conversation_id_1 = recorder.calls[0][1]
    reply1 = body["choices"][0]["message"]["content"]

    # Turn 2 extends turn 1 -- this CONSUMES turn 1's (single-use) continuation
    # entry, so the branch below can no longer match it.
    post(
        port,
        {
            "model": "m365-copilot",
            "messages": [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": reply1},
                {"role": "user", "content": "and now the second thing"},
            ],
        },
    )

    # Same prefix as turn 2, but a DIFFERENT final message than the one that
    # extended the store -- turn 1's continuation point is already consumed, so
    # this must NOT be treated as a continuation; it falls back to full
    # context-stuffing under a brand-new conversation_id.
    recorder.calls.clear()
    messages_branch = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": reply1},
        {"role": "user", "content": "a DIFFERENT second thing entirely"},
    ]
    post(port, {"model": "m365-copilot", "messages": messages_branch})
    assert recorder.calls[0][1] != conversation_id_1
    assert "hello there" in recorder.calls[0][0]
    assert "DIFFERENT" in recorder.calls[0][0]


def test_tools_present_never_continuation(recorder, fake_token_cache, run_server, post):
    sessions = proxy.ConversationSessionStore()
    port = run_server(fake_token_cache, sessions)

    _, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    conversation_id_1 = recorder.calls[0][1]
    reply1 = body["choices"][0]["message"]["content"]

    # tools present: must never take the continuation path even though the
    # history exactly extends turn 1.
    recorder.calls.clear()
    post(
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
    assert recorder.calls[0][1] != conversation_id_1
    assert "hello there" in recorder.calls[0][0]


def test_failure_recovery_retries_as_fresh(
    recorder, fake_token_cache, run_server, post
):
    sessions = proxy.ConversationSessionStore()
    port = run_server(fake_token_cache, sessions)

    # A continuation turn whose Chathub call fails is retried once as a
    # brand-new, fully context-stuffed conversation.
    _, body = post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "recovery seed"}],
        },
    )
    recovery_reply = body["choices"][0]["message"]["content"]
    recovery_conversation_id = recorder.calls[0][1]

    recorder.calls.clear()
    recorder.fail_ids.add(recovery_conversation_id)
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
    assert status == 200  # HTTP 200 despite the underlying failure
    assert len(recorder.calls) == 2  # failed continuation + retry
    assert recorder.calls[0][1] == recovery_conversation_id
    assert recorder.calls[1][1] != recovery_conversation_id  # retry used a fresh id
    assert "recovery seed" in recorder.calls[1][0]  # retry sent the full transcript


def test_continuity_disabled_never_reuses(recorder, fake_token_cache, run_server, post):
    # conversation_sessions=None (== --disable-conversation-continuity) must
    # make every request take the fresh/full-context-stuffing path.
    port = run_server(fake_token_cache, None)

    post(
        port,
        {
            "model": "m365-copilot",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    first_id = recorder.calls[0][1]

    recorder.calls.clear()
    post(
        port,
        {
            "model": "m365-copilot",
            "messages": [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": "echo:hello there"},
                {"role": "user", "content": "second"},
            ],
        },
    )
    assert recorder.calls[0][1] != first_id
    assert "hello there" in recorder.calls[0][0]


def test_conversation_session_store_unit():
    store = proxy.ConversationSessionStore()
    fp_a = proxy._conversation_fingerprint([{"role": "user", "content": "x"}])
    fp_b = proxy._conversation_fingerprint([{"role": "user", "content": "y"}])
    assert fp_a != fp_b
    assert fp_a == proxy._conversation_fingerprint([{"role": "user", "content": "x"}])

    store.remember(fp_a, "conv-a", "oid-1")
    assert store.lookup(fp_a, "oid-1") is not None
    assert store.lookup(fp_a, "oid-2") is None  # different oid misses

    store.forget(fp_a)
    assert store.lookup(fp_a, "oid-1") is None
    assert store.forget(None) is None  # harmless no-op
