#!/usr/bin/env python3
"""Ad-hoc diagnostic: dumps every raw SignalR frame for one Chathub turn, to
debug why a reply came back empty. NOT part of the shipped proxy."""

import json
import logging
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python-copilot-m365")
)
import m365_openai_proxy as proxy  # noqa: E402

logging.basicConfig(level=logging.WARNING)

CREDENTIALS_PREFIX = os.path.join(
    os.path.dirname(__file__), "..", "..", "python-copilot-m365", "m365_openai_proxy"
)


def main():
    store = proxy.CredentialStore(CREDENTIALS_PREFIX)
    token_cache = proxy.TokenCache(store)
    auth = token_cache.get()
    ws, session_id = proxy.open_chathub(auth)
    try:
        proxy.send_chat_message(ws, session_id, "Reply with exactly: hello world")
        buf = proxy.SignalRBuffer()
        import time

        deadline = time.time() + 30
        while time.time() < deadline:
            raw = ws.recv_text()
            if raw is None:
                print("connection closed")
                break
            if not raw:
                continue
            for frame in buf.feed(raw):
                print(json.dumps(frame)[:2000])
                if frame.get("type") in (3, 7):
                    return
    finally:
        ws.close()


if __name__ == "__main__":
    main()
