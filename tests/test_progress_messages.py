"""Offline pytest suite for Sydney's transient status/progress messages.

Sydney interleaves its own UI chrome -- progress placeholders ("Gathering
details...", "Working on it...", "Coding and executing"), follow-up
suggestions, citation-list end markers -- into the very same
`target:"update"` frames that carry the answer, distinguished ONLY by
carrying a `messageType` where the real answer entry carries none. Those must
never reach the caller: before this was filtered, every plain (non-tool-call)
reply came back with a status string glued to the front of its content
(observed live: `"Making it happen\u2026pong"`, and a first streamed chunk of
`"Working on it\u2026"`).

Frames here are verbatim-shaped copies of a live `scripts/dump_frames.py`
capture, trimmed to the fields `stream_chat_reply` actually reads. No network:
a fake WebSocket replays canned frames.
"""

import json

import pytest

import m365_openai_proxy as proxy


class FakeWS:
    """Replays canned SignalR frames, one `recv_text()` per frame.

    Frames are handed over already joined with the record separator the real
    hub uses, so `SignalRBuffer` is exercised for real. Once exhausted,
    `recv_text()` returns None (what the real client returns on a closed
    socket) -- which `stream_chat_reply` treats as a truncated reply, so every
    test below ends its frame list with a completion (type 3) frame unless it
    is specifically testing another terminal path.
    """

    def __init__(self, frames):
        self._pending = [json.dumps(f) + proxy.SIGNALR_RS for f in frames]

    def recv_text(self):
        if not self._pending:
            return None
        return self._pending.pop(0)


def _update(*messages, **extra):
    """A type-1 `target:"update"` frame carrying `messages[]`."""
    arg = {"messages": list(messages)}
    arg.update(extra)
    return {"type": 1, "target": "update", "arguments": [arg]}


def _answer(text, message_id="answer-1"):
    """An answer entry: note NO `messageType` key at all -- that absence is
    the only thing distinguishing it from the chrome below."""
    return {
        "text": text,
        "author": "bot",
        "responseIdentifier": "Default",
        "messageId": message_id,
        "contentOrigin": "DeepLeo",
    }


def _progress(text="Gathering details\u2026", message_id="progress-1"):
    return {
        "text": text,
        "author": "bot",
        "contentType": "EarlyProgress",
        "isPersisted": False,
        "messageId": message_id,
        "messageType": "Progress",
    }


COMPLETION = {"type": 3, "invocationId": "0"}


def _collect(frames):
    return "".join(proxy.stream_chat_reply(FakeWS(frames), timeout_s=5))


def test_progress_placeholder_is_not_part_of_the_reply():
    """The exact live shape: a `Progress` placeholder arrives BEFORE the first
    answer token, so a naive implementation yields it as the reply's opening
    text."""
    reply = _collect(
        [
            _update(_progress()),
            _update(_answer("hello")),
            _update(_answer("hello world")),
            COMPLETION,
        ]
    )
    assert reply == "hello world"


def test_progress_placeholder_interleaved_mid_reply_is_dropped():
    """Chrome arriving BETWEEN two answer snapshots must neither appear in the
    reply nor corrupt the snapshot diff that follows it."""
    reply = _collect(
        [
            _update(_answer("hel")),
            _update(_progress("Working on it\u2026")),
            _update(_answer("hello")),
            _update(_progress("Making it happen\u2026", message_id="progress-2")),
            _update(_answer("hello world")),
            COMPLETION,
        ]
    )
    assert reply == "hello world"


@pytest.mark.parametrize(
    "message_type",
    [
        "Progress",
        "InternalLoaderMessage",
        "InternalSearchQuery",
        "ReferencesListComplete",
        "Suggestion",
        "RenderCardRequest",
    ],
)
def test_every_chrome_message_type_is_dropped(message_type):
    chrome = dict(_progress(), messageType=message_type)
    reply = _collect([_update(chrome), _update(_answer("pong")), COMPLETION])
    assert reply == "pong"


def test_snapshots_are_diffed_per_message_id():
    """Two answer-bearing messages in one turn must each be diffed against
    THEIR OWN previous snapshot. With one shared "last text", the second
    snapshot of `answer-1` no longer extends the last text seen (which by then
    came from the other message), and the whole answer gets re-yielded --
    duplicating it in the reply."""
    disengaged = {
        "text": "[refusal]",
        "author": "bot",
        "messageId": "disengaged-1",
        "messageType": "Disengaged",
    }
    reply = _collect(
        [
            _update(_answer("hello")),
            _update(disengaged),
            _update(_answer("hello world")),
            COMPLETION,
        ]
    )
    assert reply == "hello[refusal] world"
    assert reply.count("hello") == 1


def test_generated_code_entry_is_kept_as_content():
    """`GeneratedCode` is NOT chrome, however much it looks like it.

    Verified live with `scripts/probe_generated_code_messages.py`: in code
    tool-calling mode the fenced block holding the `invoke_capability(...)`
    call arrives as a `messageType:"GeneratedCode"` entry, while the prose
    answer entry of that same turn holds only prose. Filtering it out silently
    breaks every code-mode tool call -- there is then nothing left for
    `_extract_code_mode_calls` to parse.
    """
    code_block = (
        "```python\nresult = invoke_capability('get_weather', "
        "{'city': 'Prague'})\nprint(result)\n```"
    )
    generated_code = {
        "text": code_block,
        "author": "bot",
        "messageId": "code-1",
        "messageType": "GeneratedCode",
    }
    reply = _collect(
        [
            # The live ordering: an "Coding and executing" Progress entry sits
            # immediately before the code -- the leak this fix removes -- and
            # the code itself must survive it.
            _update(_progress("Coding and executing", message_id="progress-2")),
            _update(generated_code),
            COMPLETION,
        ]
    )
    assert reply == code_block
    assert "Coding and executing" not in reply
    # The parser the tool-call path depends on must still find the call.
    assert proxy._extract_code_mode_calls(reply)[0]


def test_disengaged_refusal_text_still_reaches_the_caller():
    """`Disengaged` carries Sydney's own canned "I'd rather not continue this
    conversation" text, which is meant to be shown -- so filtering chrome must
    not silently swallow it into an unexplained empty reply."""
    disengaged = {
        "text": "I'm sorry, I'd rather not continue this conversation.",
        "author": "bot",
        "messageId": "disengaged-1",
        "messageType": "Disengaged",
    }
    assert _collect([_update(disengaged), COMPLETION]) == disengaged["text"]


def test_failed_turn_is_reported_even_after_a_progress_placeholder():
    """A turn that emits only a progress placeholder and then fails must still
    surface as a throttle. While placeholders counted as answer text, the
    type-2 `turnState:"Failed"` check was guarded off by the placeholder and
    the turn "succeeded" with the placeholder as its entire content."""
    stream_item = {
        "type": 2,
        "invocationId": "0",
        "item": {
            "messages": [
                {"text": "the prompt", "author": "user"},
                _progress(),
                {
                    "text": (
                        "We're temporarily unable to respond to this volume of "
                        "requests. Please try again later."
                    ),
                    "author": "bot",
                    "turnState": "Failed",
                },
            ]
        },
    }
    with pytest.raises(proxy.ThrottledError, match="temporarily unable"):
        _collect([_update(_progress()), stream_item, COMPLETION])


def test_auth_error_still_raises():
    """The pre-existing AuthError path is checked before the chrome filter, so
    it must survive it (its entry carries `messageType:"AuthError"`, which is
    not an answer type)."""
    auth_error = {
        "text": "token expired",
        "author": "bot",
        "messageType": "AuthError",
    }
    with pytest.raises(proxy.AuthError, match="token expired"):
        _collect([_update(auth_error), COMPLETION])


def test_unrecognized_message_type_is_skipped_but_warned_about(caplog):
    """Unknown types are skipped (fail closed -- chrome is far likelier than a
    new answer shape), but WARNed so that a reply that comes back empty for
    this reason is diagnosable from the log file alone."""
    unknown = {
        "text": "who knows",
        "author": "bot",
        "messageId": "mystery-1",
        "messageType": "SomeBrandNewType",
    }
    with caplog.at_level("WARNING"):
        reply = _collect([_update(unknown), _update(_answer("pong")), COMPLETION])
    assert reply == "pong"
    assert "SomeBrandNewType" in caplog.text


def test_known_chrome_does_not_warn(caplog):
    """...while the chrome we already know about stays out of the log at
    WARNING, so normal turns don't spam it."""
    with caplog.at_level("WARNING"):
        _collect([_update(_progress()), _update(_answer("pong")), COMPLETION])
    assert caplog.text == ""


def test_reply_text_is_never_logged(caplog):
    """Message payloads are logged as lengths only (the file's logging
    convention), so the skipped chrome's text must not appear in the log."""
    with caplog.at_level("DEBUG"):
        _collect(
            [
                _update(_progress("Gathering secrets\u2026")),
                _update(_answer("pong")),
                COMPLETION,
            ]
        )
    assert "Gathering secrets" not in caplog.text
    assert "text_length=18" in caplog.text
