"""Offline pytest suite for logging Sydney's per-turn `throttling` quota block.

Sydney sends a `throttling: {maxNumUserMessagesInConversation,
numUserMessagesInConversation, ...}` object as a SIBLING of `messages[]` inside
the same `target:"update"` frames that carry the answer (frame 5 in
REVERSE_ENGINEERING.md's frame-by-frame table). This proxy received it and
threw it away for its whole history, which left one specific question
unanswerable from a log alone: when a run ends in Sydney's silent empty-reply
throttle, was the account near a quota ceiling at the time or not?

`stream_chat_reply` now logs it. These tests pin the two things that make that
log trustworthy -- that every key present is surfaced (including ones this
proxy has never seen, since discovering those is the point) and that a repeated
block logs once, not once per frame. No network: a fake WebSocket replays
canned frames.
"""

import json
import logging

import pytest

import m365_openai_proxy as proxy


class FakeWS:
    """Replays canned SignalR frames, one `recv_text()` per frame (same shape
    as `tests/test_progress_messages.py`'s -- frames are joined with the real
    record separator so `SignalRBuffer` is exercised for real)."""

    def __init__(self, frames):
        self._pending = [json.dumps(f) + proxy.SIGNALR_RS for f in frames]

    def recv_text(self):
        if not self._pending:
            return None
        return self._pending.pop(0)


def _update(*messages, **extra):
    arg = {"messages": list(messages)}
    arg.update(extra)
    return {"type": 1, "target": "update", "arguments": [arg]}


def _answer(text, message_id="answer-1"):
    return {
        "text": text,
        "author": "bot",
        "responseIdentifier": "Default",
        "messageId": message_id,
        "contentOrigin": "DeepLeo",
    }


COMPLETION = {"type": 3, "invocationId": "0"}

#: The block's REAL shape, captured live from a Chathub turn on 2026-08-02
#: via `scripts/dump_frames.py` (values as received; see
#: REVERSE_ENGINEERING.md's "Sydney's `throttling` block, measured"). Note the
#: third counter -- it is not in any earlier writeup, and it is the concrete
#: reason `_throttling_summary` renders every key rather than a known list.
QUOTA = {
    "maxNumUserMessagesInConversation": 600,
    "numUserMessagesInConversation": 1,
    "numLongDocSummaryUserMessagesInConversation": 0,
}


def _collect(frames):
    return "".join(proxy.stream_chat_reply(FakeWS(frames), timeout_s=5))


def _quota_lines(caplog):
    return [
        r.getMessage() for r in caplog.records if "throttling/quota" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# _throttling_summary
# ---------------------------------------------------------------------------


#: What the live block above renders to. Pinned exactly: this is the string a
#: human greps for in a log, so its shape is part of the feature.
QUOTA_SUMMARY = (
    "used=1 max=600 headroom=599 numLongDocSummaryUserMessagesInConversation=0"
)


def test_summary_reports_used_max_and_derived_headroom():
    assert proxy._throttling_summary(QUOTA) == QUOTA_SUMMARY


def test_summary_surfaces_keys_this_proxy_has_never_seen():
    """The whole reason the summary isn't a fixed key list: a field Sydney
    starts sending must show up the first time it arrives, not be dropped."""
    summary = proxy._throttling_summary(
        dict(QUOTA, someNewFlag=True, dailyBudgetRemaining=17)
    )
    assert summary.startswith("used=1 max=600 headroom=599 ")
    assert "someNewFlag=True" in summary
    assert "dailyBudgetRemaining=17" in summary
    # The counter that was itself undocumented until it was measured -- the
    # case this whole design decision exists for.
    assert "numLongDocSummaryUserMessagesInConversation=0" in summary


def test_summary_handles_a_partially_populated_block():
    """Historically this block has been observed populated only partially, so
    a missing counter must not suppress the one that IS there. With only one
    of the pair present there is no headroom to derive."""
    summary = proxy._throttling_summary({"maxNumUserMessagesInConversation": 30})
    assert summary == "max=30"
    assert "headroom" not in summary


def test_summary_of_an_empty_block_is_explicit():
    """An empty block is itself a finding (it means Sydney sent the key with
    nothing in it), so it must log as something rather than a blank line."""
    assert proxy._throttling_summary({}) == "(empty)"


def test_summary_does_not_mistake_booleans_for_counters():
    """`isinstance(True, int)` is True in Python, so a bool in either counter
    slot would otherwise render as `used=1` and produce a nonsense headroom."""
    summary = proxy._throttling_summary(
        {"numUserMessagesInConversation": True, "maxNumUserMessagesInConversation": 30}
    )
    assert "used=" not in summary
    assert "headroom" not in summary
    assert "max=30" in summary
    assert "numUserMessagesInConversation=True" in summary


@pytest.mark.parametrize(
    "value,expected",
    [
        ("short", "'short'"),
        ("x" * 200, "<str len=200>"),
        ([1, 2, 3], "<list len=3>"),
        ({"a": 1}, "<dict len=1>"),
        (None, "None"),
    ],
)
def test_summary_reduces_oversized_and_structured_values(value, expected):
    """Metadata-only logging convention: this block is server-controlled, so
    scalars log verbatim but anything long or nested logs as type + size."""
    assert proxy._throttling_summary({"k": value}) == f"k={expected}"


# ---------------------------------------------------------------------------
# stream_chat_reply wiring
# ---------------------------------------------------------------------------


def test_quota_block_is_logged_from_a_live_shaped_frame(caplog):
    caplog.set_level(logging.INFO)
    reply = _collect([_update(throttling=QUOTA), _update(_answer("hi")), COMPLETION])
    assert reply == "hi"
    assert _quota_lines(caplog) == [f"Sydney throttling/quota state: {QUOTA_SUMMARY}"]


def test_repeated_identical_block_logs_once_per_turn(caplog):
    """Sydney repeats the block across several frames in one turn; logging per
    frame would bury the log in duplicates on every long reply."""
    caplog.set_level(logging.INFO)
    _collect(
        [
            _update(_answer("h"), throttling=QUOTA),
            _update(_answer("hi"), throttling=QUOTA),
            _update(_answer("hi!"), throttling=QUOTA),
            COMPLETION,
        ]
    )
    assert len(_quota_lines(caplog)) == 1


def test_a_changed_block_logs_again(caplog):
    """Deduplication is on the value, not a once-per-turn latch: if the
    counters actually move mid-turn, that is exactly what we want to see."""
    caplog.set_level(logging.INFO)
    _collect(
        [
            _update(_answer("h"), throttling=QUOTA),
            _update(
                _answer("hi"), throttling=dict(QUOTA, numUserMessagesInConversation=4)
            ),
            COMPLETION,
        ]
    )
    lines = _quota_lines(caplog)
    assert len(lines) == 2
    assert "used=1 max=600 headroom=599" in lines[0]
    assert "used=4 max=600 headroom=596" in lines[1]


def test_absent_or_malformed_throttling_block_is_ignored(caplog):
    """Frames without the key (most of them) and a non-dict value must both be
    no-ops -- never a crash, never a log line."""
    caplog.set_level(logging.INFO)
    reply = _collect(
        [_update(_answer("hi")), _update(_answer("hi!"), throttling="nope"), COMPLETION]
    )
    assert reply == "hi!"
    assert _quota_lines(caplog) == []


def test_quota_block_never_leaks_into_the_reply_text():
    """It is a sibling of `messages[]`, so it must not be mistaken for content
    the way progress placeholders once were."""
    reply = _collect(
        [
            _update(_answer("pong"), throttling=QUOTA, nonce="n", requestId="r"),
            COMPLETION,
        ]
    )
    assert reply == "pong"
