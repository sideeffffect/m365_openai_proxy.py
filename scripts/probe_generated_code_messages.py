#!/usr/bin/env python3
"""Ad-hoc diagnostic: for a real code-mode tool-calling turn, dump every bot
`messages[]` entry as (messageType, messageId, text) so we can see WHICH entry
the `invoke_capability(...)` call actually lands in -- the plain answer entry
(no messageType) or a `messageType:"GeneratedCode"` entry.

This decides whether `stream_chat_reply`'s chrome filter may treat
`GeneratedCode` as non-answer text. NOT part of the shipped proxy.

Usage: python3 scripts/probe_generated_code_messages.py <credentials-prefix>
"""

import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import m365_openai_proxy as proxy

logging.basicConfig(level=logging.WARNING)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

MESSAGES = [{"role": "user", "content": "What is the weather in Prague?"}]


def main():
    prefix = sys.argv[1]
    store = proxy.CredentialStore(prefix)
    token_cache = proxy.TokenCache(store)
    auth = token_cache.get()

    prompt = proxy._render_conversation_prompt(MESSAGES, TOOLS, None, mode="code")
    print(f"--- prompt ({len(prompt)} chars) rendered in code mode ---\n")

    ws, session_id = proxy.open_chathub(auth)
    try:
        proxy.send_chat_message(ws, session_id, prompt, tool_mode="code")
        buf = proxy.SignalRBuffer()
        deadline = time.time() + 90
        while time.time() < deadline:
            raw = ws.recv_text()
            if raw is None:
                print("connection closed")
                break
            if not raw:
                continue
            done = False
            for frame in buf.feed(raw):
                if frame.get("type") == 1 and frame.get("target") == "update":
                    for arg in frame.get("arguments") or []:
                        for msg in arg.get("messages") or []:
                            if msg.get("author") != "bot":
                                continue
                            print(
                                json.dumps(
                                    {
                                        "messageType": msg.get("messageType"),
                                        "messageId": str(msg.get("messageId"))[:8],
                                        "contentType": msg.get("contentType"),
                                        "text": msg.get("text"),
                                    },
                                    indent=2,
                                    ensure_ascii=False,
                                )
                            )
                            print("-" * 70)
                elif frame.get("type") in (3, 7):
                    done = True
            if done:
                break
    finally:
        ws.close()


if __name__ == "__main__":
    main()
