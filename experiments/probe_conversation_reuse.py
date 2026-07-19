#!/usr/bin/env python3
"""
probe_conversation_reuse.py -- live experiment, NOT part of the shipped proxy.

Answers the one genuinely-unconfirmed question that used to block a
Sydney-native conversation mode (see REVERSE_ENGINEERING.md's "Sydney-native
conversation continuity" section): if we open a Chathub WebSocket, send
turn 1, close it, then open a BRAND NEW WebSocket reusing the SAME
`ConversationId` (new `session_id`, new `X-SessionId`, new
`clientrequestid`, exactly like a second, independent
`/v1/chat/completions` HTTP call would) and send a turn 2 that only makes
sense if the server remembers turn 1 -- does Sydney answer using its own
server-side memory, or does it behave like a fresh conversation?

Hand-rolls the `chat` invocation payload (rather than calling
`send_chat_message()`) only so `isStartOfSession` can be varied per turn --
the shipped proxy always sends `isStartOfSession: false` unconditionally
(matching every real capture in REVERSE_ENGINEERING.md), so this probe sets
it to `true` for a conversation's genuine first turn to match what a real
client does, and `false` for continuations, to keep the experiment as
close to realistic client behavior as possible.

Run directly against the sibling checkout's already-configured credential
files (read-only except for the refresh_token rotation the proxy always does
as part of normal operation) -- this deliberately does NOT copy the
credential files, to avoid two independent copies of the same refresh token
racing each other:

    python3 experiments/probe_conversation_reuse.py

Prints each turn's reply text (synthetic test content only, nothing
sensitive) and a final verdict line.

RESULT (live-tested 2026-07-19, see REVERSE_ENGINEERING.md's "Sydney-native
conversation continuity" section for the full transcript): Sydney DOES honor
a reused `ConversationId` across independent WebSocket connections as real
server-side conversation memory -- the control turn (fresh
`ConversationId`) correctly showed no knowledge of the secret word.
"""

import json
import logging
import os
import sys
import urllib.parse
import uuid

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "python-copilot-m365"),
)

import m365_openai_proxy as proxy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CREDENTIALS_PREFIX = os.path.join(
    os.path.dirname(__file__), "..", "..", "python-copilot-m365", "m365_openai_proxy"
)

SECRET_WORD = "zylophant-47"


def one_turn(auth, conversation_id, text, is_start_of_session):
    """Opens a fresh WebSocket (fresh session_id) but pins ConversationId,
    exactly like run_chat_turn() but with an explicit, reusable
    conversation_id and explicit control over isStartOfSession (see module
    docstring for why the latter isn't exposed by send_chat_message)."""
    session_id = str(uuid.uuid4())
    session_id_nodash = session_id.replace("-", "")

    query = urllib.parse.urlencode(
        {
            "chatsessionid": session_id_nodash,
            "XRoutingParameterSessionKey": session_id_nodash,
            "clientrequestid": session_id_nodash,
            "X-SessionId": session_id,
            "ConversationId": conversation_id,
            "access_token": auth.access_token,
            "variants": proxy.CHATHUB_VARIANTS,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "licenseType": "Starter",
            "isEdu": "false",
            "agent": "web",
            "scenario": "OfficeWebIncludedCopilot",
        }
    )
    url = (
        f"wss://{proxy.CHATHUB_HOST}/m365Copilot/Chathub/{auth.oid}@{auth.tid}?{query}"
    )
    ws = proxy.WebSocketClient(
        url, extra_headers={"Origin": "https://m365.cloud.microsoft"}
    )
    ws.send_text(json.dumps({"protocol": "json", "version": 1}) + proxy.SIGNALR_RS)
    ack = ws.recv_text()
    if ack is None:
        raise proxy.ProtocolError("handshake failed")

    try:
        trace_id = str(uuid.uuid4())
        payload = {
            "type": 4,
            "target": "chat",
            "invocationId": "0",
            "arguments": [
                {
                    "source": "officeweb",
                    "clientCorrelationId": trace_id,
                    "sessionId": session_id,
                    "optionsSets": proxy.OPTIONS_SETS,
                    "streamingMode": "ConciseWithPadding",
                    "options": {},
                    "extraExtensionParameters": {},
                    "allowedMessageTypes": proxy.ALLOWED_MESSAGE_TYPES,
                    "sliceIds": [],
                    "threadLevelGptId": {},
                    "traceId": trace_id,
                    "isStartOfSession": is_start_of_session,
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
                        "entityAnnotationTypes": [
                            "People",
                            "File",
                            "Event",
                            "Email",
                            "TeamsMessage",
                        ],
                        "requestId": trace_id,
                        "locationInfo": {"timeZoneOffset": 0, "timeZone": "UTC"},
                        "locale": "en-US",
                        "messageType": "Chat",
                        "experienceType": "Default",
                        "adaptiveCards": [],
                        "clientPreferences": {},
                        "connectedFederatedConnections": ["dummyId"],
                    },
                    "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                    "isSbsSupported": True,
                    "tone": "Magic",
                    "renderReferencesBehindEOS": True,
                    "disconnectBehavior": "continue",
                }
            ],
        }
        ws.send_text(json.dumps(payload) + proxy.SIGNALR_RS)
        reply = "".join(proxy.stream_chat_reply(ws))
        return reply, session_id
    finally:
        ws.close()


def main():
    store = proxy.CredentialStore(CREDENTIALS_PREFIX)
    token_cache = proxy.TokenCache(store)
    auth = token_cache.get()

    conversation_id = str(uuid.uuid4())
    print(f"=== fixed ConversationId for this probe: {conversation_id} ===")

    print("\n--- turn 1 (new WS #1, isStartOfSession=True): teach the secret word ---")
    text1 = (
        f"For this conversation only, the secret code word is '{SECRET_WORD}'. "
        "Please just confirm you've noted it in one short sentence, don't ask "
        "anything else."
    )
    reply1, sid1 = one_turn(auth, conversation_id, text1, is_start_of_session=True)
    print(f"[session_id={sid1}] bot reply:\n{reply1}\n")

    print(
        "--- turn 2 (BRAND NEW WS #2, same ConversationId, isStartOfSession=False) ---"
    )
    text2 = (
        "Without me repeating it: what was the secret code word I told you "
        "a moment ago in this same conversation?"
    )
    reply2, sid2 = one_turn(auth, conversation_id, text2, is_start_of_session=False)
    print(f"[session_id={sid2}] bot reply:\n{reply2}\n")

    print("--- control turn (BRAND NEW WS #3, BRAND NEW ConversationId) ---")
    control_conversation_id = str(uuid.uuid4())
    reply3, sid3 = one_turn(
        auth, control_conversation_id, text2, is_start_of_session=True
    )
    print(f"[session_id={sid3}] bot reply:\n{reply3}\n")

    print("=== verdict ===")
    remembered = SECRET_WORD.lower() in reply2.lower()
    control_knew = SECRET_WORD.lower() in reply3.lower()
    print(f"turn 2 (same ConversationId) mentions the secret word: {remembered}")
    print(
        f"control turn (fresh ConversationId) mentions the secret word: {control_knew}"
    )
    if remembered and not control_knew:
        print(
            "=> Sydney DOES honor a reused ConversationId across independent "
            "WebSocket connections as server-side conversation memory."
        )
    elif not remembered:
        print(
            "=> Sydney does NOT appear to recall turn 1 when ConversationId is "
            "reused across a new WebSocket connection (native memory keyed on "
            "something else, e.g. the live socket, or not honored at all)."
        )
    else:
        print("=> inconclusive -- both turns show knowledge, check logs manually.")


if __name__ == "__main__":
    main()
