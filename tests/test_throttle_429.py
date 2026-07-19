"""Offline pytest suite for throttle harmonization.

Both Sydney throttle shapes -- (a) explicit `ThrottledError` refusal and
(b) silent empty reply -- must surface uniformly as HTTP 429 with err_type
"upstream_throttled", on BOTH the non-streaming and streaming paths. No
network: `run_chat_turn` is stubbed.
"""

import json

import pytest

import m365_openai_proxy as proxy


@pytest.fixture
def mode(monkeypatch):
    """Monkeypatch `run_chat_turn` to a stub whose behavior is selected by the
    returned dict's ``["kind"]``: "ok" | "throttled" | "empty" | "empty_none"."""
    state = {"kind": "ok"}

    def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
        kind = state["kind"]
        if kind == "throttled":
            raise proxy.ThrottledError(
                "Sydney refused the turn: \"We're temporarily unable to respond to "
                'this volume of requests. Please try again later."'
            )
        if kind == "empty":
            yield ""  # whitespace-only -> _looks_like_throttled_empty_reply
            return
        if kind == "empty_none":
            return  # generator yields nothing at all (streaming empty)
        yield f"echo:{text}"

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return state


@pytest.fixture
def port(fake_token_cache, run_server):
    return run_server(fake_token_cache, proxy.ConversationSessionStore())


BASE_MSGS = [{"role": "user", "content": "hello"}]


def _sse_error_types(raw_body):
    return [
        json.loads(ln[6:]).get("error", {}).get("type")
        for ln in raw_body.splitlines()
        if ln.startswith("data: ") and '"error"' in ln
    ]


def test_ok_non_streaming_is_200(mode, port, raw_post):
    mode["kind"] = "ok"
    status, _ = raw_post(port, {"messages": BASE_MSGS})
    assert status == 200


def test_throttled_error_non_streaming(mode, port, raw_post):
    mode["kind"] = "throttled"
    status, body = raw_post(port, {"messages": BASE_MSGS})
    obj = json.loads(body)
    assert status == 429
    assert obj.get("error", {}).get("type") == "upstream_throttled"
    assert "volume of requests" in obj.get("error", {}).get("message", "")


def test_empty_reply_non_streaming(mode, port, raw_post):
    mode["kind"] = "empty"
    status, body = raw_post(port, {"messages": BASE_MSGS})
    obj = json.loads(body)
    assert status == 429
    assert obj.get("error", {}).get("type") == "upstream_throttled"


def test_throttled_error_streaming(mode, port, raw_post):
    mode["kind"] = "throttled"
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    assert "upstream_throttled" in _sse_error_types(body)


def test_empty_reply_streaming(mode, port, raw_post):
    mode["kind"] = "empty_none"
    _, body = raw_post(port, {"messages": BASE_MSGS, "stream": True})
    assert "upstream_throttled" in _sse_error_types(body)


def test_throttled_continuation_not_silently_retried(mode, port, raw_post):
    # A continuation that throttles should forget the session and propagate a
    # 429, not silently retry as a fresh conversation.
    mode["kind"] = "ok"
    m1 = [{"role": "user", "content": "turn one"}]
    _, body = raw_post(port, {"messages": m1})
    reply = json.loads(body)["choices"][0]["message"]["content"]
    m2 = m1 + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "turn two"},
    ]
    mode["kind"] = "throttled"
    status, _ = raw_post(port, {"messages": m2})
    assert status == 429
