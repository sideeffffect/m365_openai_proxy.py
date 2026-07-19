"""Offline pytest suite for the token-refresh race fix.

With a cold cache and N concurrent callers, `TokenCache.get()` must redeem the
single-use refresh token EXACTLY ONCE (dedicated ``_refresh_lock`` +
double-checked caching), never N times. No network: `exchange_refresh_token`
and the credential store are stubbed. Also asserts the warm path takes no
refresh lock / no extra redemption.
"""

import base64
import json
import threading
import time

import pytest

import m365_openai_proxy as proxy


def _fake_access_token(exp):
    """A minimal unsigned JWT-ish token with oid/tid/exp claims that
    `proxy.jwt_claims()` can decode (it base64-decodes the middle segment)."""

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    header = seg({"alg": "none"})
    payload = seg({"oid": "oid-1", "tid": "tid-1", "exp": exp})
    return f"{header}.{payload}.sig"


@pytest.fixture
def harness(monkeypatch):
    """Wire up a `TokenCache` over a fake single-use refresh-token store and a
    fake Entra exchange that records every redemption. Returns a handle with
    `.cache`, `.exchange_calls`, `.rotate_calls`."""
    exchange_calls = []
    rotate_calls = []
    exchange_lock = threading.Lock()
    current_rt = {"v": "refresh-token-0"}

    class FakeStore:
        oid_hint = "oid-1"
        tid_hint = "tid-1"

        def current(self):
            return current_rt["v"]

        def rotate(self, new_rt):
            rotate_calls.append(new_rt)
            current_rt["v"] = new_rt

    def fake_exchange(refresh_token, oid=None, tid=None):
        # Simulate Entra: single-use. Record the redemption; redeeming the same
        # token twice is exactly the bug we're guarding against.
        with exchange_lock:
            exchange_calls.append(refresh_token)
            time.sleep(0.05)  # widen the race window
            return {
                "access_token": _fake_access_token(int(time.time()) + 3600),
                "refresh_token": f"refresh-token-{len(exchange_calls)}",
                "expires_in": 3600,
            }

    monkeypatch.setattr(proxy, "exchange_refresh_token", fake_exchange)

    class Harness:
        cache = proxy.TokenCache(FakeStore())

    h = Harness()
    h.exchange_calls = exchange_calls
    h.rotate_calls = rotate_calls
    return h


def test_cold_start_redeems_exactly_once(harness):
    n = 20
    results = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()  # maximize simultaneity on a cold cache
        results.append(harness.cache.get().access_token)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(harness.exchange_calls) == 1, harness.exchange_calls
    assert len(harness.rotate_calls) == 1, harness.rotate_calls
    assert len(results) == n and all(results)
    assert len(set(results)) == 1  # all callers got the SAME token


def test_warm_cache_does_not_redeem_again(harness):
    harness.cache.get()  # warm it
    before = len(harness.exchange_calls)
    harness.cache.get()
    assert len(harness.exchange_calls) == before
