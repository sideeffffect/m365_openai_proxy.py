"""Offline pytest suite for CredentialStore's refresh_token vs.
encrypted_refresh_token precedence.

Regression coverage for a real-world failure: the proxy always overwrites
`refresh_token.conf` after every successful exchange (see `rotate()`), so
once the encrypted-cache credential path has ever been used, a
`refresh_token.conf` sticks around from then on. If that rotated refresh
token later expires or is superseded (e.g. Entra's AADSTS700084 for
SPA-issued tokens, capped at a fixed 24h lifetime) and the operator
recaptures a fresh `encrypted_refresh_token`/`cache_encryption_key` pair,
the stale `refresh_token.conf` must NOT keep winning just because it still
exists -- whichever credential was written to more recently should be
used. `_decrypt_msal_cache_entry` is monkeypatched out (it needs a real
MSAL cache entry) so these tests exercise pure file-precedence logic, no
crypto.
"""

import os
import time

import pytest

import m365_openai_proxy as proxy


def _write(path, value):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# header\n{value}\n")


def _touch(path, when):
    os.utime(path, (when, when))


@pytest.fixture
def prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return str(tmp_path / "m365_openai_proxy")


@pytest.fixture
def stub_decrypt(monkeypatch):
    calls = []

    def fake_decrypt(encrypted, key, local_storage_key):
        calls.append((encrypted, key))
        return "decrypted-refresh-token"

    monkeypatch.setattr(proxy, "_decrypt_msal_cache_entry", fake_decrypt)
    return calls


def _write_encrypted_trio(prefix, when):
    _write(f"{prefix}.encrypted_refresh_token.conf", '{"id": "x", "data": "y"}')
    _write(f"{prefix}.cache_encryption_key.conf", '{"id": "x", "key": "z"}')
    _touch(f"{prefix}.encrypted_refresh_token.conf", when)
    _touch(f"{prefix}.cache_encryption_key.conf", when)


def test_stale_plaintext_refresh_token_loses_to_fresher_encrypted(prefix, stub_decrypt):
    """A refresh_token.conf written BEFORE a freshly recaptured encrypted
    trio must be ignored in favor of the newer encrypted credential."""
    now = time.time()
    _write(f"{prefix}.refresh_token.conf", "stale-plaintext-rt")
    _touch(f"{prefix}.refresh_token.conf", now - 3600)  # an hour older

    _write_encrypted_trio(prefix, now)  # just recaptured

    store = proxy.CredentialStore(prefix)

    assert store.current() == "decrypted-refresh-token"
    assert len(stub_decrypt) == 1


def test_fresher_plaintext_refresh_token_still_wins(prefix, stub_decrypt):
    """The normal/common case: refresh_token.conf was rotated more recently
    than the (older, unchanged) encrypted trio -- plaintext still wins."""
    now = time.time()
    _write_encrypted_trio(prefix, now - 3600)  # old, untouched since capture

    _write(f"{prefix}.refresh_token.conf", "fresh-plaintext-rt")
    _touch(f"{prefix}.refresh_token.conf", now)  # just rotated

    store = proxy.CredentialStore(prefix)

    assert store.current() == "fresh-plaintext-rt"
    assert len(stub_decrypt) == 0


def test_plaintext_only_still_used_with_no_encrypted_files(prefix, stub_decrypt):
    _write(f"{prefix}.refresh_token.conf", "only-plaintext-rt")

    store = proxy.CredentialStore(prefix)

    assert store.current() == "only-plaintext-rt"
    assert len(stub_decrypt) == 0


def test_encrypted_only_still_used_with_no_plaintext_file(prefix, stub_decrypt):
    _write_encrypted_trio(prefix, time.time())

    store = proxy.CredentialStore(prefix)

    assert store.current() == "decrypted-refresh-token"
    assert len(stub_decrypt) == 1
