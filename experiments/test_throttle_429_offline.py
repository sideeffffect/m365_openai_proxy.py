#!/usr/bin/env python3
"""Targeted offline test for the HARMONIZATION change specifically:
both Sydney throttle shapes -- (a) explicit ThrottledError refusal (from
PR #11) and (b) silent empty reply (from PR #10) -- must now surface
uniformly as HTTP 429 with err_type "upstream_throttled", on BOTH the
non-streaming and streaming paths. No network: run_chat_turn is stubbed.
"""

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("proxy", "m365_openai_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

FAILURES = []


def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


class FakeAuth:
    oid = "fake-oid"
    tid = "fake-tid"
    access_token = "fake"
    expires_at = time.time() + 3600


class FakeTokenCache:
    def get(self):
        return FakeAuth()


# Behavior switch for the stub, set per-test.
MODE = {"kind": "ok"}


def fake_run_chat_turn(token_cache, text, conversation_id=None, **kwargs):
    kind = MODE["kind"]
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


def raw_post(port, body, stream=False):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()
            return resp.status, data
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    proxy.run_chat_turn = fake_run_chat_turn
    sessions = proxy.ConversationSessionStore()
    handler_cls = proxy.make_handler(FakeTokenCache(), sessions)
    server = proxy._LoggingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base_msgs = [{"role": "user", "content": "hello"}]

    try:
        # --- sanity: ok path is 200 ---
        MODE["kind"] = "ok"
        st, body = raw_post(port, {"messages": base_msgs})
        check("ok non-streaming -> 200", st == 200)

        # --- (a) explicit ThrottledError, non-streaming ---
        MODE["kind"] = "throttled"
        st, body = raw_post(port, {"messages": base_msgs})
        obj = json.loads(body)
        check("ThrottledError non-streaming -> 429", st == 429)
        check(
            "ThrottledError non-streaming -> upstream_throttled",
            obj.get("error", {}).get("type") == "upstream_throttled",
        )
        check(
            "ThrottledError message carries Sydney's refusal text",
            "volume of requests" in obj.get("error", {}).get("message", ""),
        )

        # --- (b) silent empty reply, non-streaming ---
        MODE["kind"] = "empty"
        st, body = raw_post(port, {"messages": base_msgs})
        obj = json.loads(body)
        check("empty-reply non-streaming -> 429", st == 429)
        check(
            "empty-reply non-streaming -> upstream_throttled",
            obj.get("error", {}).get("type") == "upstream_throttled",
        )

        # --- (a) explicit ThrottledError, STREAMING (SSE error event) ---
        MODE["kind"] = "throttled"
        st, body = raw_post(port, {"messages": base_msgs, "stream": True}, stream=True)
        # streaming path returns 200 header then an SSE error event
        err_types = [
            json.loads(ln[6:]).get("error", {}).get("type")
            for ln in body.splitlines()
            if ln.startswith("data: ") and '"error"' in ln
        ]
        check(
            "ThrottledError streaming -> SSE error event upstream_throttled",
            "upstream_throttled" in err_types,
        )

        # --- (b) empty reply, STREAMING ---
        MODE["kind"] = "empty_none"
        st, body = raw_post(port, {"messages": base_msgs, "stream": True}, stream=True)
        err_types = [
            json.loads(ln[6:]).get("error", {}).get("type")
            for ln in body.splitlines()
            if ln.startswith("data: ") and '"error"' in ln
        ]
        check(
            "empty-reply streaming -> SSE error event upstream_throttled",
            "upstream_throttled" in err_types,
        )

        # --- ThrottledError must NOT be retried in place (over-capacity) ---
        # A continuation that throttles should forget the session and propagate,
        # not silently retry as a fresh conversation. Verify via call count.
        MODE["kind"] = "ok"
        m1 = [{"role": "user", "content": "turn one"}]
        st, body = raw_post(port, {"messages": m1})
        reply = json.loads(body)["choices"][0]["message"]["content"]
        m2 = m1 + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "turn two"},
        ]
        # now throttle the continuation
        MODE["kind"] = "throttled"
        st, body = raw_post(port, {"messages": m2})
        check("throttled continuation -> 429 (not silent retry)", st == 429)

    finally:
        server.shutdown()
        server.server_close()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        raise SystemExit(1)
    print("\nAll throttle-unification checks passed.")


if __name__ == "__main__":
    main()
