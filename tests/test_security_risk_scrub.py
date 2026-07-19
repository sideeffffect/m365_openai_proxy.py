"""Offline pytest suite for the `security_risk` parameter scrub.

Some clients (OpenHands with `native_tool_calling: false`, at least) render
their own tool-calling convention as plain text inside the system prompt and
parse the model's reply for `<function=...><parameter=...>` blocks. Sydney
reliably includes a `security_risk` parameter on every such call, which such
a client's parser can reject for read-only tools -- see
`_scrub_security_risk_param`'s docstring. This proxy has no visibility into
that client-side parser, but it can and does strip the parameter from
Sydney's raw reply text before it reaches the client, in both the
non-streaming and streaming plain-chat (no `tools`) paths. No network:
`run_chat_turn` is stubbed.
"""

import json

import pytest

import m365_openai_proxy as proxy


FINISH_CALL = (
    "<function=finish>\n"
    "<parameter=message>Hello! How can I help you today?</parameter>\n"
    "<parameter=security_risk>LOW</parameter>\n"
    "<parameter=summary>Greet user and offer assistance</parameter>\n"
    "</function>"
)

# Some replies leave the last parameter's closing tag off entirely.
FINISH_CALL_UNCLOSED_TAIL = (
    "<function=finish>\n"
    "<parameter=message>Hi! How can I help you today?</parameter>\n"
    "<parameter=summary>Greet user and offer assistance</parameter>\n"
    "<parameter=security_risk>LOW\n"
    "</function>"
)


@pytest.fixture
def mode(monkeypatch):
    """Monkeypatch `run_chat_turn` to yield a fixed reply, selected by the
    returned dict's ["reply"]."""
    state = {"reply": FINISH_CALL}

    def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
        yield state["reply"]

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return state


@pytest.fixture
def port(fake_token_cache, run_server):
    return run_server(fake_token_cache, proxy.ConversationSessionStore())


BASE_MSGS = [{"role": "user", "content": "hello"}]


def test_scrub_removes_closed_security_risk_param():
    scrubbed = proxy._scrub_security_risk_param(FINISH_CALL)
    assert "security_risk" not in scrubbed
    assert "<parameter=message>Hello! How can I help you today?" in scrubbed
    assert "<parameter=summary>Greet user and offer assistance" in scrubbed


def test_scrub_removes_unclosed_trailing_security_risk_param():
    scrubbed = proxy._scrub_security_risk_param(FINISH_CALL_UNCLOSED_TAIL)
    assert "security_risk" not in scrubbed
    assert scrubbed.endswith("</function>")
    assert "<parameter=summary>Greet user and offer assistance" in scrubbed


def test_scrub_is_a_no_op_without_the_tag():
    text = "Just an ordinary reply, nothing tool-shaped here."
    assert proxy._scrub_security_risk_param(text) == text


def test_non_streaming_plain_reply_is_scrubbed(mode, port, post):
    status, obj = post(port, {"messages": BASE_MSGS})
    assert status == 200
    content = obj["choices"][0]["message"]["content"]
    assert "security_risk" not in content
    assert "<function=finish>" in content


def test_streaming_plain_reply_is_scrubbed(mode, port, raw_post):
    mode["reply"] = FINISH_CALL
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    chunks = [
        json.loads(ln[6:])
        for ln in body.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]
    full_text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "security_risk" not in full_text
    assert "<function=finish>" in full_text


def test_streaming_ordinary_prose_is_untouched(mode, port, raw_post):
    mode["reply"] = "Just an ordinary reply, nothing tool-shaped here."
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    chunks = [
        json.loads(ln[6:])
        for ln in body.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]
    full_text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert full_text == mode["reply"]
