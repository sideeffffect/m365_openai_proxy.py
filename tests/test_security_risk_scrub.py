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

# ...or even the </function> itself, with security_risk dead last.
FINISH_CALL_RISK_LAST_UNCLOSED = (
    "<function=finish>\n"
    "<parameter=message>Hi!</parameter>\n"
    "<parameter=summary>Greet</parameter>\n"
    "<parameter=security_risk>LOW"
)

# security_risk is a real, schema-allowed parameter on every non-read-only
# tool (it carries the client's LLM security analyzer's risk prediction) --
# only `finish` rejects it, so only finish blocks are scrubbed.
TERMINAL_CALL = (
    "<function=terminal>\n"
    "<parameter=command>echo hi</parameter>\n"
    "<parameter=security_risk>MEDIUM</parameter>\n"
    "<parameter=summary>Say hi</parameter>\n"
    "</function>"
)

# Sydney often prefixes its code-interpreter boilerplate to the block.
PROSE_THEN_FINISH = (
    "Coding and executing<function=finish>\n"
    "<parameter=message>Done.</parameter>\n"
    "<parameter=security_risk>LOW</parameter>\n"
    "<parameter=summary>Wrap up</parameter>\n"
    "</function>"
)


@pytest.fixture
def mode(monkeypatch):
    """Monkeypatch `run_chat_turn` to yield a fixed reply, selected by the
    returned dict's ["reply"] -- a single string (yielded as one delta) or a
    list of strings (yielded as separate deltas, to exercise streaming)."""
    state = {"reply": FINISH_CALL}

    def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
        reply = state["reply"]
        yield from [reply] if isinstance(reply, str) else reply

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return state


@pytest.fixture
def port(fake_token_cache, run_server):
    return run_server(fake_token_cache, proxy.ConversationSessionStore())


BASE_MSGS = [{"role": "user", "content": "hello"}]


def _streamed_text(raw_body):
    """Concatenates the content of every SSE delta chunk in `raw_body`."""
    chunks = [
        json.loads(ln[6:])
        for ln in raw_body.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


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


def test_scrub_removes_trailing_unclosed_security_risk_param():
    scrubbed = proxy._scrub_security_risk_param(FINISH_CALL_RISK_LAST_UNCLOSED)
    assert "security_risk" not in scrubbed
    assert scrubbed.endswith("<parameter=summary>Greet</parameter>")


def test_scrub_leaves_non_finish_calls_untouched():
    assert proxy._scrub_security_risk_param(TERMINAL_CALL) == TERMINAL_CALL


def test_scrub_mixed_blocks_only_touches_finish():
    text = TERMINAL_CALL + "\n\n" + FINISH_CALL
    scrubbed = proxy._scrub_security_risk_param(text)
    # terminal's risk prediction (used by the client's security analyzer)
    # survives; only finish's rejected copy is removed.
    assert "<parameter=security_risk>MEDIUM</parameter>" in scrubbed
    assert scrubbed.count("security_risk") == 1


def test_streaming_scrubs_finish_after_leading_prose(mode, port, raw_post):
    mode["reply"] = PROSE_THEN_FINISH
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    full_text = _streamed_text(body)
    assert "security_risk" not in full_text
    assert full_text.startswith("Coding and executing")
    assert "<function=finish>" in full_text


def test_streaming_scrubs_when_tag_is_split_across_deltas(mode, port, raw_post):
    mode["reply"] = [
        "Coding and exec",
        "uting<func",
        "tion=finish>\n<parameter=message>Done.</parameter>\n"
        "<parameter=security_risk>LOW</parameter>\n</function>",
    ]
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    full_text = _streamed_text(body)
    assert "security_risk" not in full_text
    assert full_text.startswith("Coding and executing")
    assert "<function=finish>" in full_text
