"""Offline pytest suite for Sydney's non-text content and capability surface.

Covers three things found by probing Sydney's own capabilities live on
2026-08-02 (`scripts/probe_sydney_capabilities.py`; see
REVERSE_ENGINEERING.md's "Sydney's own capability surface, probed"):

1. **The image-only turn, which used to be reported as throttling.** Sydney's
   image generation really works -- it invokes its own `image_gen` tool and
   produces a file -- but streams NO answer text, so the turn came back empty
   and `_looks_like_throttled_empty_reply` reported HTTP 429 "Microsoft is
   temporarily throttling this account ... wait a bit and try again". Nothing
   was throttled and waiting never helps; the request fails identically
   forever. It must now be HTTP 502 `unsupported_upstream_content` -- while a
   genuinely empty reply must STILL be 429, since that one really is the
   throttle signature.

2. **`throttling.metering`**, Sydney's own list of 15 metered capabilities,
   which rides on the type-2 StreamItem frame and so was missed entirely by
   the type-1-only parsing shipped in v0.12.0.

3. **`invocation`**, direct evidence that Sydney emits real, native,
   OpenAI-shaped `tool_calls` for its own built-in tools.

Fixtures are verbatim live captures, trimmed only where noted. No network.
"""

import json
import logging

import pytest

import m365_openai_proxy as proxy


class FakeWS:
    """Replays canned SignalR frames, one `recv_text()` per frame (same shape
    as `tests/test_progress_messages.py`'s)."""

    def __init__(self, frames):
        self._pending = [json.dumps(f) + proxy.SIGNALR_RS for f in frames]

    def recv_text(self):
        if not self._pending:
            return None
        return self._pending.pop(0)


def _update(*messages):
    return {"type": 1, "target": "update", "arguments": [{"messages": list(messages)}]}


def _answer(text, message_id="answer-1"):
    return {
        "text": text,
        "author": "bot",
        "responseIdentifier": "Default",
        "messageId": message_id,
        "contentOrigin": "DeepLeo",
    }


COMPLETION = {"type": 3, "invocationId": "0"}

#: Verbatim from the live capture, with the (very long) opaque `pollUrl` and
#: `ImageReferenceUrls` values shortened -- nothing reads them.
IMAGE_PROGRESS = {
    "text": "Loading image",
    "author": "bot",
    "messageType": "Progress",
    "messageId": "1daba4a6-47e1-4ebf-89ef-9d4e384f4c52",
    "contentType": "GraphicArt",
    "contentOrigin": "ImageGeneration",
    "isPersisted": True,
    "contentGenerationProgressList": [
        {
            "contentType": "image",
            "size": "Xlimage",
            "orientation": "Landscape",
            "pollUrl": "eyJQb2xsSWQiOiJmYjk2ZDJhOC0x",
            "fileToken": "18f10018-4a37-4e42-9c60-13e20d800d6b",
            "ImageReferenceUrls": ["https://designerapp.officeapps.live.com/x"],
            "status": 2,
        }
    ],
    # Doubly JSON-encoded, exactly as received.
    "invocation": json.dumps(
        [
            json.dumps(
                {
                    "function": {
                        "arguments": '{"orientation":"landscape"}',
                        "name": "image_gen",
                    },
                    "id": "call_zvq9VI82lh3kvZdNlbDl5c",
                    "type": "function",
                }
            )
        ]
    ),
}

#: Verbatim live `throttling` from a type-2 StreamItem, all 15 capabilities.
STREAM_ITEM_THROTTLING = {
    "maxNumUserMessagesInConversation": 600,
    "numUserMessagesInConversation": 1,
    "numLongDocSummaryUserMessagesInConversation": 0,
    "metering": {
        "TenantDataAccess": {"remainingAllowance": 0},
        "ImageAnalysis": {"remainingAllowance": 0},
        "LLMOnly": {"remainingAllowance": 100},
        "VisualCreator": {"remainingAllowance": 0},
        "PersonalDataAccess": {"remainingAllowance": 0},
        "GraphicArt": {"remainingAllowance": 0},
        "CodeInterpreter": {"remainingAllowance": 0},
        "FileReference": {"remainingAllowance": 3},
        "DeepResearch": {"remainingAllowance": 0},
        "CopilotTuning": {"remainingAllowance": 0},
        "DeepWork": {"remainingAllowance": 0},
        "WXPAgentMode": {"remainingAllowance": 7},
        "NotebookCowork": {"remainingAllowance": 0},
        "CostQuota": {"remainingAllowance": 0},
        "ImageGeneration": {"remainingAllowance": 100},
    },
}


def _stream_item(throttling=None, messages=None):
    item = {"messages": messages or [], "turnState": "Success"}
    if throttling is not None:
        item["throttling"] = throttling
    return {"type": 2, "invocationId": "0", "item": item}


def _collect(frames):
    return "".join(proxy.stream_chat_reply(FakeWS(frames), timeout_s=5))


def _quota_lines(caplog):
    return [
        r.getMessage() for r in caplog.records if "throttling/quota" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# The image-only turn: an explicit error, never a throttle
# ---------------------------------------------------------------------------


def test_image_only_turn_raises_unsupported_content():
    with pytest.raises(proxy.UnsupportedContentError) as excinfo:
        _collect([_update(IMAGE_PROGRESS), _stream_item(), COMPLETION])
    assert "image" in str(excinfo.value)


def test_the_error_says_retrying_will_not_help():
    """The whole point of this error: the message it replaces told callers to
    wait and retry, which can never succeed."""
    with pytest.raises(proxy.UnsupportedContentError) as excinfo:
        _collect([_update(IMAGE_PROGRESS), COMPLETION])
    message = str(excinfo.value)
    assert "NOT a rate limit" in message
    assert "retrying will not help" in message


def test_unsupported_content_is_not_caught_as_a_throttle():
    """`UnsupportedContentError` and `ThrottledError` are both
    `ProtocolError`s, and every dispatch site that distinguishes them must
    check the more specific one first."""
    assert issubclass(proxy.UnsupportedContentError, proxy.ProtocolError)
    assert not issubclass(proxy.UnsupportedContentError, proxy.ThrottledError)


def test_text_alongside_generated_content_is_a_normal_success():
    """A reply that has BOTH an image and prose is not an error -- there is
    something to return, so return it."""
    reply = _collect(
        [_update(IMAGE_PROGRESS), _update(_answer("here you go")), COMPLETION]
    )
    assert reply == "here you go"


def test_a_genuinely_empty_reply_still_raises_nothing_here():
    """No generated content means this is the real throttle signature, which
    is detected downstream by `_looks_like_throttled_empty_reply` -- this
    layer must stay out of its way."""
    assert _collect([_update(), COMPLETION]) == ""


def test_image_only_turn_raises_on_hub_close_too():
    """Sydney can end a turn with a type-7 Close instead of a completion."""
    with pytest.raises(proxy.UnsupportedContentError):
        _collect([_update(IMAGE_PROGRESS), {"type": 7}])


# ---------------------------------------------------------------------------
# Native tool-call invocations
# ---------------------------------------------------------------------------


def test_native_invocation_name_is_parsed_from_the_live_shape():
    assert proxy._native_invocation_names(IMAGE_PROGRESS["invocation"]) == ["image_gen"]


def test_native_invocation_is_logged(caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(proxy.UnsupportedContentError):
        _collect([_update(IMAGE_PROGRESS), COMPLETION])
    assert any(
        "OWN built-in tools" in r.getMessage() and "image_gen" in r.getMessage()
        for r in caplog.records
    )


def test_repeated_invocation_logs_once_per_turn(caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(proxy.UnsupportedContentError):
        _collect([_update(IMAGE_PROGRESS), _update(IMAGE_PROGRESS), COMPLETION])
    assert sum("OWN built-in tools" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    "value",
    [None, "", "not json", "[]", '["not json"]', '[{"no":"function"}]', 42],
)
def test_malformed_invocation_never_raises(value):
    """A logging aid must not be able to break a chat turn."""
    assert proxy._native_invocation_names(value) == []


# ---------------------------------------------------------------------------
# throttling.metering -- the capability list
# ---------------------------------------------------------------------------


def test_metering_renders_every_capability():
    summary = proxy._metering_summary(STREAM_ITEM_THROTTLING["metering"])
    assert summary.startswith("CodeInterpreter=0 CopilotTuning=0 CostQuota=0 ")
    assert "LLMOnly=100" in summary
    assert "FileReference=3" in summary
    assert "TenantDataAccess=0" in summary
    assert len(summary.split()) == 15


def test_metering_is_nested_inside_the_throttling_summary():
    summary = proxy._throttling_summary(STREAM_ITEM_THROTTLING)
    assert summary.startswith("used=1 max=600 headroom=599 ")
    # Sorted key order puts `metering` between the two `num...` counters, so
    # this is a nested group in the middle of the line, not a suffix.
    assert " metering[CodeInterpreter=0 " in summary
    assert "WXPAgentMode=7] numLongDocSummaryUserMessagesInConversation=0" in summary


def test_metering_surfaces_an_unknown_sub_key():
    """Same reason the outer summary renders every key: this list is
    server-controlled, and a new field is exactly what we want to notice."""
    summary = proxy._metering_summary(
        {"Something": {"remainingAllowance": 5, "resetsAt": "soon"}}
    )
    assert "Something=5" in summary
    assert "Something.resetsAt='soon'" in summary


def test_metering_handles_a_capability_with_no_allowance():
    assert proxy._metering_summary({"Odd": {}}) == "Odd=(empty)"
    assert proxy._metering_summary({"Odd": 7}) == "Odd=7"


def test_stream_item_throttling_is_logged(caplog):
    """v0.12.0 read the type-1 `update` frames only, so the metering block --
    which rides on the type-2 StreamItem -- never reached the log at all."""
    caplog.set_level(logging.INFO)
    _collect(
        [
            _update(_answer("hi")),
            _stream_item(throttling=STREAM_ITEM_THROTTLING),
            COMPLETION,
        ]
    )
    lines = _quota_lines(caplog)
    assert len(lines) == 1
    assert "metering[" in lines[0]
    assert "CodeInterpreter=0" in lines[0]


def test_stream_item_throttling_is_logged_even_on_a_successful_turn(caplog):
    """The failure check on this frame is guarded on "no text yielded"; the
    quota logging deliberately is not."""
    caplog.set_level(logging.INFO)
    reply = _collect(
        [
            _update(_answer("plenty of text")),
            _stream_item(throttling=STREAM_ITEM_THROTTLING),
            COMPLETION,
        ]
    )
    assert reply == "plenty of text"
    assert len(_quota_lines(caplog)) == 1


def test_stream_item_failure_detection_still_works(caplog):
    """Restructuring the type-2 branch to log quota must not disturb the
    turnState:"Failed" throttle detection that shares it."""
    failed = {"author": "bot", "turnState": "Failed", "text": "too much volume"}
    with pytest.raises(proxy.ThrottledError):
        _collect([_stream_item(messages=[failed]), COMPLETION])


# ---------------------------------------------------------------------------
# End-to-end through the real HTTP handler
# ---------------------------------------------------------------------------


@pytest.fixture
def kind(monkeypatch):
    """Stubs `run_chat_turn` to raise/yield per the returned dict's ["kind"]."""
    state = {"kind": "image"}

    def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
        if state["kind"] == "image":
            raise proxy.UnsupportedContentError(
                proxy._UNSUPPORTED_CONTENT_MSG % "image x1"
            )
        if state["kind"] == "empty":
            yield ""
            return
        yield "fine"

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    return state


@pytest.fixture
def port(fake_token_cache, run_server):
    return run_server(fake_token_cache, proxy.ConversationSessionStore())


MSGS = [{"role": "user", "content": "Generate an image of a red bicycle."}]


def test_image_request_is_502_not_429(kind, port, raw_post):
    """The regression this release exists for."""
    kind["kind"] = "image"
    status, body = raw_post(port, {"messages": MSGS})
    assert status == 502
    error = json.loads(body)["error"]
    assert error["type"] == "unsupported_upstream_content"
    assert "NOT a rate limit" in error["message"]


def test_a_genuinely_empty_reply_is_still_429(kind, port, raw_post):
    """The throttle path must be untouched -- this is the case where 'wait and
    retry' really is the right advice."""
    kind["kind"] = "empty"
    status, body = raw_post(port, {"messages": MSGS})
    assert status == 429
    assert json.loads(body)["error"]["type"] == "upstream_throttled"


def test_image_request_is_502_on_the_streaming_path_too(kind, port, raw_post):
    kind["kind"] = "image"
    status, body = raw_post(port, {"messages": MSGS, "stream": True})
    types = [
        json.loads(ln[6:]).get("error", {}).get("type")
        for ln in body.splitlines()
        if ln.startswith("data: ") and '"error"' in ln
    ]
    assert status == 200  # SSE headers are already sent; the error is in-band
    assert types == ["unsupported_upstream_content"]
