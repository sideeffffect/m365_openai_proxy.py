#!/usr/bin/env python3
"""Offline test for PR #7's token-refresh race fix: with a cold cache and
N concurrent callers, TokenCache.get() must redeem the single-use refresh
token EXACTLY ONCE (dedicated _refresh_lock + double-checked caching),
never N times. No network: exchange_refresh_token + CredentialStore are
stubbed. Also asserts the fast path takes no refresh lock once warm.
"""

import base64
import importlib.util
import json
import threading
import time

spec = importlib.util.spec_from_file_location("proxy", "m365_openai_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

FAILURES = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' -- ' + extra) if extra else ''}")
    if not cond:
        FAILURES.append(label)


# ---- fake single-use refresh-token store + exchange ----
exchange_calls = []
rotate_calls = []
_exchange_lock = threading.Lock()


# a minimal unsigned JWT-ish access token with oid/tid/exp claims that
# proxy.jwt_claims() can decode (it base64-decodes the middle segment).
def _fake_access_token(exp):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    header = seg({"alg": "none"})
    payload = seg({"oid": "oid-1", "tid": "tid-1", "exp": exp})
    return f"{header}.{payload}.sig"


CURRENT_RT = {"v": "refresh-token-0"}


class FakeStore:
    oid_hint = "oid-1"
    tid_hint = "tid-1"

    def current(self):
        return CURRENT_RT["v"]

    def rotate(self, new_rt):
        rotate_calls.append(new_rt)
        CURRENT_RT["v"] = new_rt


def fake_exchange(refresh_token, oid=None, tid=None):
    # Simulate Entra: single-use. Record the redemption; if the same token
    # is redeemed twice, that's the bug we're guarding against.
    with _exchange_lock:
        exchange_calls.append(refresh_token)
        time.sleep(0.05)  # widen the race window
        return {
            "access_token": _fake_access_token(int(time.time()) + 3600),
            "refresh_token": f"refresh-token-{len(exchange_calls)}",
            "expires_in": 3600,
        }


def main():
    proxy.exchange_refresh_token = fake_exchange
    cache = proxy.TokenCache(FakeStore())

    N = 20
    results = []
    barrier = threading.Barrier(N)

    def worker():
        barrier.wait()  # maximize simultaneity on a cold cache
        auth = cache.get()
        results.append(auth.access_token)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(
        "cold-start: exactly ONE refresh-token redemption across 20 threads",
        len(exchange_calls) == 1,
        f"got {len(exchange_calls)}",
    )
    check(
        "cold-start: exactly ONE rotate() write",
        len(rotate_calls) == 1,
        f"got {len(rotate_calls)}",
    )
    check("all 20 callers got a token", len(results) == N and all(results))
    check("all 20 callers got the SAME token", len(set(results)) == 1)

    # warm path: another get() must NOT trigger a new exchange
    before = len(exchange_calls)
    cache.get()
    check(
        "warm cache: no additional redemption",
        len(exchange_calls) == before,
        f"before={before} after={len(exchange_calls)}",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        raise SystemExit(1)
    print("\nAll token-refresh race checks passed.")


if __name__ == "__main__":
    main()
