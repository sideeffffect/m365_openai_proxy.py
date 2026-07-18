#!/usr/bin/env python3
"""
m365_openai_proxy.py -- a minimal OpenAI-compatible HTTP API backed by
https://m365.cloud.microsoft's Copilot chat backend ("Sydney" / Chathub).

SELF-CONTAINED: this single file uses ONLY the Python 3 standard library --
no `pip install` of anything, ever, for any feature (including the MSAL
encrypted-cache decrypt path, which needs AES-256-GCM; this file implements
that itself in pure Python -- see the "Pure-Python AES-256-GCM" section
below). Drop this one file onto any machine with a Python 3 interpreter and
run it; nothing else needs to be installed.

------------------------------------------------------------------------------
AUTHENTICATION MODEL -- READ THIS FIRST
------------------------------------------------------------------------------
This proxy's OpenAI-style HTTP API (`/v1/chat/completions`, `/v1/models`)
takes NO Authorization header and NO API key from its callers, by design.
All of Microsoft's authentication is handled internally by this program.

Instead, YOU (the operator) configure the proxy once, at startup, with raw
material you copy out of your own already-authenticated browser session --
plain-text "credential files" (see below), holding either a plaintext
refresh token, or an encrypted MSAL cache entry plus its decryption key.
From that point on, this program:
  - silently redeems that credential for a short-lived Sydney/Chathub access
    token whenever it needs one (via Microsoft's FOCI mechanism -- no client
    secret required, since the client IDs involved are Microsoft's own
    first-party apps);
  - PERSISTS each newly-rotated refresh token back to its credential file,
    since Entra ID invalidates the previous refresh token on every redemption
    (this is not optional bookkeeping -- without persisting the rotation,
    this proxy would work for exactly one internal exchange and then break,
    which is exactly what happened during manual testing before this design
    existed);
  - opens the Chathub WebSocket and speaks its wire protocol on your behalf.

Because there is no per-request auth, bind this to 127.0.0.1 (the default)
unless you have your own reason to expose it, and treat the credential
files as at least as sensitive as a password.

------------------------------------------------------------------------------
LOGGING
------------------------------------------------------------------------------
Every run writes a detailed log to `m365_openai_proxy.log` next to wherever
you ran it from (override with --log-file). This is FILE-ONLY for almost
everything the proxy does -- nothing about normal operation is printed to
the console. The log file is meant to be self-sufficient for
troubleshooting: if something goes wrong, the operator (who may not be the
person who wrote or configured this proxy) can send just this one file to
whoever supports them -- a developer, or an AI given the file to read --
without needing to reproduce the problem live or paste terminal
scrollback. --log-level controls verbosity (default INFO; use DEBUG for
maximum detail e.g. redacted Chathub URLs and cache-hit decisions).

The ONE exception to "file-only": when the proxy cannot start at all, or
crashes to a complete stop while running, a short, plain-language message
IS printed to the console (see `_print_fatal_console_message`). It
deliberately contains no technical detail whatsoever -- no error codes, no
tracebacks -- just a sentence saying the program couldn't continue, and an
instruction to send `m365_openai_proxy.log` to whoever supports this
program, because that file has everything needed to diagnose it. This
covers things like: the credential files are missing/malformed, the
configured credential was rejected by Entra ID at startup, or an
unexpected bug crashed the process. A single failed chat request, or a
temporary hiccup that leaves the server itself still running and able to
accept the next request, is NOT one of these cases -- those stay
file-only, since the proxy hasn't actually stopped functioning.

Every run's log opens with a startup banner (Python version, platform,
process id, bind host/port, credentials/log file paths) so a reader has
basic environment context without asking follow-up questions. Beyond that,
logged: which credential-file field was used and why, every refresh-token
rotation (only its length, never its value), every Sydney/Chathub token
exchange (oid/tid/expiry, never the token itself), every Chathub WebSocket
open/close (session/conversation ids), every incoming HTTP request
(method/path/lengths/model), every chat turn's outcome (reply length,
never its content), and -- importantly -- a full traceback for ANY
unhandled exception, whether it happens during startup (main thread) or
while servicing a request (worker thread): both a global excepthook and a
per-request catch-all ensure a bug never just silently drops a request or
crashes invisibly, it always leaves a diagnosable entry in the log.

Secrets/tokens/passwords (refresh tokens, access tokens, decrypted cache
secrets, this program's own outgoing Authorization-equivalent credentials)
are NEVER written to any log line -- only their lengths, or safe
identifiers like oid/tid/session ids, are logged where useful for
debugging. This holds even for the catch-all exception logging above:
Python's traceback formatting only prints source lines and local exception
context, not arbitrary local variables, so a secret held in a local
variable at the point of a crash is not printed as a side effect of this.
If you ever spot a token/secret value appearing in `m365_openai_proxy.log`,
that's a bug in this file, not intended behavior.

------------------------------------------------------------------------------
CREDENTIAL FILE FORMATS
------------------------------------------------------------------------------
Credentials are spread across FOUR plain-text files rather than one JSON
file -- deliberately, so that pasting a raw value (which for two of these
fields is itself a JSON snippet copied out of DevTools) never requires the
operator to escape or re-encode anything. Each file is named
`<prefix>.<field>.conf`, where `<prefix>` is set with --credentials-prefix
(default: `m365_openai_proxy`, i.e. right next to this script):

    m365_openai_proxy.refresh_token.conf
    m365_openai_proxy.encrypted_refresh_token.conf
    m365_openai_proxy.cache_encryption_key.conf
    m365_openai_proxy.local_storage_key.conf

Run with --init-credentials to write all four as starter templates (only
if none of them already exist) rather than typing them out by hand. You
don't actually have to remember to do this yourself, though: if you just
run the proxy normally and none of the four files exist yet at the
configured prefix, it writes these same starter templates for you
automatically, then stops and tells you (both on the console and in the
log) to go fill them in and restart -- exactly as if you had passed
--init-credentials first. Each template is a block of comment lines
starting with "#" explaining what the field is and, most importantly,
exactly where in the browser to get it from -- self-documenting even
without this docstring open:

    # m365_openai_proxy -- refresh_token
    #
    # <wrapped explanation of what this is and where to get it>
    #
    # Paste the value below this line (everything from the next
    # non-comment line to the end of the file is used verbatim), then save.

To fill one in: open the file, and on a new line below the comment block,
paste the raw value exactly as copied from the browser -- no quoting, no
escaping, whitespace around it is trimmed automatically. Everything from
the first non-comment, non-blank line to the end of the file is taken
as-is as that field's value (so a multi-line/pretty-printed paste works
too, not just a single line).

Only ONE of two value combinations needs to actually be filled in:

  1. `refresh_token` file = a plaintext refresh token string (RECOMMENDED
     -- this is the form this project has actually verified works
     end-to-end). Leave this file's value empty (just the comment block)
     if you're instead using option 2 below.

     How to get one: open https://m365.cloud.microsoft in a browser you're
     signed into, open DevTools -> Network tab, filter by "token", then
     reload the page or send a chat message so MSAL performs a silent token
     renewal. Find the resulting POST to
     login.microsoftonline.com/.../oauth2/v2.0/token, open its Request/
     Payload view, and copy the `refresh_token` form field's value verbatim
     -- it is sent in cleartext over HTTPS at that point, unlike the copy
     MSAL keeps at rest. This must be done freshly each time you fill in
     the file: MSAL silently rotates this token in the background for as
     long as that browser tab stays open, which invalidates any earlier
     copy you took (observed directly during development -- a token
     harvested this way and left unused for ~40 minutes while the source
     browser tab stayed open was already rejected by Entra as superseded).

  2. `encrypted_refresh_token` + `cache_encryption_key` + `local_storage_key`
     all filled in (VERIFIED WORKING -- algorithm confirmed against MSAL
     Browser's own source, see below):
       - `encrypted_refresh_token` = the localStorage entry's VALUE:
         DevTools -> Application/Storage -> Local Storage ->
         m365.cloud.microsoft -> find the KEY NAME containing
         "refreshtoken" -- its value is the whole
         `{"id":...,"nonce":...,"data":...,"lastUpdatedAt":...}` object,
         paste it here verbatim.
       - `local_storage_key` = that same entry's KEY NAME itself (not its
         value), e.g.
         `msal.3|<homeAccountId>|login.windows.net|refreshtoken|<clientId
         or familyId>|||` -- needed to derive the correct decryption
         context (MSAL binds the derived key to the owning clientId when
         one is present in this key name).
       - `cache_encryption_key` = DevTools -> Application/Storage ->
         Cookies -> m365.cloud.microsoft -> the `msal.cache.encryption`
         cookie's value (URL-decode it first if it starts with "%7B") --
         `{"id":...,"key":...}`. Its `id` must match the
         `encrypted_refresh_token` file's `id` or decryption will fail (it
         means the two weren't captured at the same moment).

     ALGORITHM (reverse-engineered from MSAL Browser's actual source --
     lib/msal-browser/src/crypto/BrowserCrypto.ts and
     lib/msal-browser/src/cache/LocalStorage.ts on the AzureAD/
     microsoft-authentication-library-for-js repo -- NOT the raw AES-GCM one
     might first assume, which is why an earlier attempt at this brute-
     forcing standard parameters failed):
       1. `rawKey` = base64url-decode the `cache_encryption_key` file's `key`.
       2. `context` = the owning app's clientId, taken from the
          `local_storage_key` file's 5th `|`-separated segment IF that
          segment is GUID-shaped (a real client-specific token); otherwise
          `""` (a FOCI family-shared token, whose key instead carries a
          short marker like "1" there).
       3. Derive a per-entry AES-256 key via HKDF-SHA256(ikm=rawKey,
          salt=base64url-decode(the `encrypted_refresh_token` file's
          `nonce`), info=utf8(context)) -- note "nonce" is the HKDF *salt*,
          not the AES-GCM IV.
       4. AES-GCM-decrypt the `encrypted_refresh_token` file's `data` using
          that derived key with a FIXED all-zero 12-byte IV (safe only
          because step 3 derives a brand new key per operation -- straight
          from a comment in MSAL's own source to that effect).
       5. The resulting plaintext is JSON;
          `{"credentialType":"RefreshToken","secret":"<the refresh
          token>",...}`.
     `_hkdf_sha256()` and the AES-256-GCM decrypt itself are both implemented
     in this file with the standard library only (`hmac`/`hashlib` for HKDF;
     see the "Pure-Python AES-256-GCM" section further down for the AES/GCM
     step -- no third-party crypto package needed for any of this). Confirmed
     working end-to-end during development: decrypted a real captured cache
     entry into well-formed
     `{"credentialType":"RefreshToken","clientId":...,"secret":...}` JSON,
     and that same decrypted secret was then successfully redeemed against
     Entra ID and used to serve real chat completions (see
     REVERSE_ENGINEERING.md's writeup on the `exchange_refresh_token()`
     request-shape fix for why earlier attempts at this appeared to fail).
     `local_storage_key`/`encrypted_refresh_token`/`cache_encryption_key`
     should still be captured close together in time as good practice (a
     `local_storage_key`/`cache_encryption_key` pair with mismatched `id`s
     will fail to decrypt outright, which is easy to tell apart from Entra
     rejecting a since-superseded token), but this is no longer the
     dominant failure mode it once appeared to be.

Whichever option you fill in, the proxy overwrites the `refresh_token`
file after every token exchange with Entra's newly-rotated refresh token
(see AUTHENTICATION MODEL above for why) -- the other three files are left
untouched so you can see what was originally supplied.

------------------------------------------------------------------------------
KNOWN LIMITATIONS
------------------------------------------------------------------------------
- **Multi-turn memory is "context-stuffing", not native Sydney state.**
  `_render_conversation_prompt()` renders the *entire* incoming `messages`
  array (system/developer instructions + every prior user/assistant turn)
  into one plain-text blob, and that whole blob becomes the single Chathub
  turn's `message.text` -- `run_chat_turn()` still mints a brand-new random
  Chathub `ConversationId` on every single API call, same as always. In
  other words: this proxy does NOT use Sydney's own server-side memory (it
  is unconfirmed whether reusing a stable `ConversationId` across calls
  would even work -- see REVERSE_ENGINEERING.md); instead it relies on the
  *client* resending its full growing conversation every request, which is
  exactly what stateless per-request clients like Aider already do. Two
  practical consequences: (1) the effective prompt sent to Sydney grows with
  the conversation, so very long-running sessions may eventually hit
  whatever context/size limit Sydney enforces (unconfirmed, not yet
  surfaced by this proxy); (2) a client that expects Sydney's *own*
  memory/threading semantics (e.g. server-side conversation continuation
  independent of what the client resends) will not get that -- it gets
  exactly the transcript it sent, replayed back through one fresh Sydney
  turn, every time.
- `usage` (prompt/completion/total tokens) in every response is hardcoded
  to zero -- this proxy does no token counting at all.
- One Chathub WebSocket is opened, used for exactly one turn, and closed
  per HTTP request -- no connection pooling or reuse across requests.
- Sydney's own per-conversation throttling info (seen in the Chathub
  protocol's `throttling: {maxNumUserMessagesInConversation, ...}` field --
  see REVERSE_ENGINEERING.md) is not surfaced or specially handled; if you
  hit a quota, whatever Sydney returns is passed through as-is.
- **Tool/function calling is EMULATED and probabilistic, not a guarantee.**
  Sydney has no native OpenAI-style `tools`/`tool_calls` mechanism this proxy
  can use (its real one, Local MCP, is a separate, unimplemented, much
  harder project -- see below). What this proxy does instead: when a
  request includes `tools`, it injects a plain-text instructions block
  (`_render_tools_block()`) teaching the model a fixed textual convention
  (`<action_request>{"name": ..., "arguments": {...}}</action_request>`)
  and parses that back out of the reply (`_extract_tool_calls()`) into a
  real OpenAI `tool_calls` response. This is entirely prompt-level steering
  with NO formal contract -- live testing during development found:
    - Sydney's own built-in capabilities (its code interpreter, mainly) very
      often preempt this convention entirely -- it just tries to satisfy the
      request itself first, even for things it structurally cannot do (e.g.
      reading a file from the *user's* machine, which its sandboxed
      interpreter has no access to).
    - The literal word "tool"/"tools" ANYWHERE in the rendered prompt --
      including in the CLIENT's own system prompt, which this proxy cannot
      control -- measurably makes this worse. This proxy actively launders
      that word out of the entire prompt when `tools` is present
      (`_neutralize_tool_word()`), which helps but does not fix it outright.
    - Even under the best (simplest, single-capability, word-laundered)
      conditions measured, the model followed the convention roughly HALF
      the time on a single attempt. This proxy retries automatically up to
      3 times per turn (`_run_tool_call_turn()`) when the first attempt
      doesn't produce a call, which raises the effective success rate
      substantially -- but retrying costs latency (each attempt is a full
      Chathub round trip) and still isn't 100%.
    - Continuing a conversation *after* a tool result required its own fix:
      naively rendering "the assistant already called X" as a fabricated
      prior turn reliably triggered a flat refusal on the next reply (a
      classic prompt-injection SHAPE, even with entirely mundane content).
      Folding the tool result into the next turn as ordinary user-supplied
      context instead of a fabricated assistant/tool history entry fixed
      this specific failure mode.
  See REVERSE_ENGINEERING.md's "Tool-calling emulation" section for the
  full trial-by-trial account. Net assessment: usable for occasional/low-
  stakes tool use, but NOT reliable enough to assume a long agentic loop
  (the kind OpenCode or OpenHands actually run -- many tool calls in a row)
  will complete without derailing into a plain-text non-call at some point.
  Sydney's own REAL tool-invocation mechanism (Local MCP, over the same
  Chathub connection -- `mcp_discover`/`mcp_describe`/`invoke_local_plugin`
  SignalR targets, reverse-engineered from the officeweb client's own
  source) is documented in REVERSE_ENGINEERING.md's "Local MCP tool-calling
  bridge" section but remains unimplemented -- bridging it properly runs
  into a genuine architecture mismatch (Sydney's invocation is synchronous,
  mid-turn, and presumably timeout-bound) that is a substantial separate
  project, not a quick patch, and would sidestep everything above.

------------------------------------------------------------------------------
REVERSE-ENGINEERING PROVENANCE / CONFIDENCE
------------------------------------------------------------------------------
Every constant and wire-format detail below (the FOCI client id, the Sydney
scope, the Chathub URL shape, the SignalR JSON framing, the `chat`
invocation payload, the streaming reply shape) was reverse-engineered from
browser HAR/WebSocket captures of the real m365.cloud.microsoft web app --
see REVERSE_ENGINEERING.md in this repository for the full analysis. The
refresh-token-exchange call and the Chathub WebSocket send/stream path (the
functions in this file) have both been LIVE-TESTED successfully against the
real service during development -- see that document's final sections. The
encrypted-cache decrypt path's algorithm was reverse-engineered directly from
MSAL Browser's own published source and confirmed to produce well-formed,
correctly-shaped plaintext against a real captured cache entry (and its
pure-Python AES-256-GCM implementation cross-validated against the FIPS-197
AES-256 test vector and against the third-party `cryptography` package
across many random inputs during development, then removed as a dependency
once validated).

An earlier revision of this docstring warned that the dominant remaining
risk was "background token rotation racing the manual copy-paste
workflow" -- repeated `AADSTS70000`/`invalid_grant` rejections during live
testing looked exactly like that. They weren't: the actual cause was
`exchange_refresh_token()` sending a request that, while RFC 6749-valid,
didn't match any of the ~16 real MSAL refresh_token-grant requests
captured across every HAR in this project (wrong token endpoint, missing
`X-AnchorMailbox`/telemetry fields -- see that function's own docstring
for the full comparison and REVERSE_ENGINEERING.md for the writeup). Fixed
and confirmed live: the exact same "stale-looking" credential that had
failed three times in a row with the old request shape was accepted
immediately once the request was corrected, and a full chat completion
(both plain and streaming) was exchanged successfully end-to-end. Ordinary
MSAL background rotation may still be a real, secondary consideration if
the source browser tab is left open for a long time between capturing a
value and using it, but it is no longer the proven explanation for
exchange failures that it once appeared to be.

------------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------------
    # one-time: write the four starter credential files (see formats
    # above), then fill in one of the two options they describe:
    python3 m365_openai_proxy.py --init-credentials

    python3 m365_openai_proxy.py --port 8000

    curl http://127.0.0.1:8000/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -d '{"model": "m365-copilot", "messages": [{"role": "user", "content": "hello"}]}'

    # note: no Authorization header -- none is expected or checked.
"""

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import logging
import os
import platform
import re
import socket
import ssl
import struct
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Single source of truth for the version string reported in the startup
# banner (see _log_startup_banner) and in the HTTP Server header.
PROXY_VERSION = "0.5"

# ==============================================================================
# Pure-Python AES-256-GCM (decrypt only) -- stdlib only, no third-party deps.
# ==============================================================================
# Implements just enough of AES (256-bit key, single-block encrypt) and
# Galois/Counter Mode to decrypt the one artifact this proxy ever needs to
# decrypt: an MSAL Browser "encrypted cache" localStorage entry (see
# _decrypt_msal_cache_entry). An earlier revision soft-depended on the
# third-party `cryptography` package for exactly this step; this ~150-line
# implementation replaces that so the whole program is a single
# dependency-free file. The S-box and Rcon tables are computed
# programmatically (not hand-transcribed) to eliminate transcription risk,
# and the whole thing was validated during development against
# `cryptography`'s AESGCM across a range of input lengths/AAD, against the
# FIPS-197 Appendix C.3 AES-256 test vector, and against a real captured
# MSAL cache entry (reproducing the exact previously-confirmed plaintext) --
# see REVERSE_ENGINEERING.md.


def _gf8_mul(a, b):
    """Multiply two bytes in GF(2^8) with the AES reducing polynomial
    x^8+x^4+x^3+x+1 (0x11B)."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes_build_sbox():
    """Computes the standard AES S-box: multiplicative inverse in GF(2^8)
    (brute-forced -- the field has only 256 elements, so this is instant)
    followed by the AES affine transformation. Computed rather than
    hand-transcribed to eliminate the risk of a silent copy-paste error in
    a 256-byte constant table."""
    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _gf8_mul(a, b) == 1:
                inv[a] = b
                break

    def rotl8(x, n):
        return ((x << n) | (x >> (8 - n))) & 0xFF

    def affine(b):
        return b ^ rotl8(b, 1) ^ rotl8(b, 2) ^ rotl8(b, 3) ^ rotl8(b, 4) ^ 0x63

    return bytes(affine(inv[i]) for i in range(256))


_AES_SBOX = _aes_build_sbox()
assert (
    _AES_SBOX[0x00] == 0x63 and _AES_SBOX[0x53] == 0xED and _AES_SBOX[0xFF] == 0x16
), "computed AES S-box failed a sanity check against known values"


def _aes_xtime(a):
    hi = a & 0x80
    a = (a << 1) & 0xFF
    return a ^ 0x1B if hi else a


def _aes_build_rcon():
    rcon = [0]  # index 0 unused; Rcon[i] = x^(i-1) in GF(2^8)
    v = 1
    for _ in range(14):
        rcon.append(v)
        v = _aes_xtime(v)
    return rcon


_AES_RCON = _aes_build_rcon()


def _aes256_key_expansion(key):
    """AES-256 key schedule (Nk=8, Nr=14): returns 15 round keys, 16 bytes each."""
    Nk, Nr, Nb = 8, 14, 4
    w = [list(key[4 * i : 4 * i + 4]) for i in range(Nk)]
    for i in range(Nk, Nb * (Nr + 1)):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_AES_SBOX[b] for b in temp]  # SubWord
            temp[0] ^= _AES_RCON[i // Nk]
        elif i % Nk == 4:
            temp = [_AES_SBOX[b] for b in temp]  # SubWord (256-bit specific)
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    round_keys = []
    for r in range(Nr + 1):
        rk = bytearray()
        for c in range(4):
            rk += bytes(w[r * 4 + c])
        round_keys.append(bytes(rk))
    return round_keys


def _aes_sub_bytes(state):
    return bytes(_AES_SBOX[b] for b in state)


def _aes_shift_rows(state):
    new = bytearray(16)
    for c in range(4):
        for r in range(4):
            new[r + 4 * c] = state[r + 4 * ((c + r) % 4)]
    return bytes(new)


def _aes_mix_columns(state):
    new = bytearray(16)
    for c in range(4):
        s0, s1, s2, s3 = (
            state[4 * c],
            state[4 * c + 1],
            state[4 * c + 2],
            state[4 * c + 3],
        )
        new[4 * c + 0] = _gf8_mul(s0, 2) ^ _gf8_mul(s1, 3) ^ s2 ^ s3
        new[4 * c + 1] = s0 ^ _gf8_mul(s1, 2) ^ _gf8_mul(s2, 3) ^ s3
        new[4 * c + 2] = s0 ^ s1 ^ _gf8_mul(s2, 2) ^ _gf8_mul(s3, 3)
        new[4 * c + 3] = _gf8_mul(s0, 3) ^ s1 ^ s2 ^ _gf8_mul(s3, 2)
    return bytes(new)


def _aes_add_round_key(state, round_key):
    return bytes(a ^ b for a, b in zip(state, round_key))


def _aes256_encrypt_block(round_keys, block):
    """Encrypts a single 16-byte block. Only building block GCM needs --
    this program never encrypts user data, only decrypts one cache entry."""
    Nr = len(round_keys) - 1
    state = _aes_add_round_key(block, round_keys[0])
    for rnd in range(1, Nr):
        state = _aes_sub_bytes(state)
        state = _aes_shift_rows(state)
        state = _aes_mix_columns(state)
        state = _aes_add_round_key(state, round_keys[rnd])
    state = _aes_sub_bytes(state)
    state = _aes_shift_rows(state)
    state = _aes_add_round_key(state, round_keys[Nr])
    return state


_GCM_R = 0xE1 << 120  # GF(2^128) reduction constant, NIST SP 800-38D


def _gf128_mul(x, y):
    """Multiply two 128-bit big-endian integers in GF(2^128) per the GCM
    spec (NIST SP 800-38D section 6.3)."""
    z = 0
    v = x
    for i in range(128):
        if (y >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ _GCM_R
        else:
            v >>= 1
    return z


def _ghash(h_int, data):
    """`data` must already be zero-padded to a multiple of 16 bytes."""
    y = 0
    for i in range(0, len(data), 16):
        y = _gf128_mul(y ^ int.from_bytes(data[i : i + 16], "big"), h_int)
    return y


def _gcm_inc32(block):
    """Increment the rightmost 32 bits of a 16-byte block, mod 2**32."""
    prefix, counter = block[:12], int.from_bytes(block[12:], "big")
    return prefix + ((counter + 1) & 0xFFFFFFFF).to_bytes(4, "big")


def _gcm_pad16(data):
    if len(data) % 16:
        data = data + b"\x00" * (16 - len(data) % 16)
    return data


def aes256_gcm_decrypt(key, iv, ciphertext_and_tag, aad=b"", tag_length=16):
    """Decrypt+authenticate AES-256-GCM (96-bit/12-byte IV only -- all this
    proxy ever needs). Raises ValueError on any authentication failure
    (wrong key, tampered ciphertext, wrong tag, etc)."""
    if len(key) != 32:
        raise ValueError("aes256_gcm_decrypt requires a 32-byte key")
    if len(iv) != 12:
        raise ValueError("aes256_gcm_decrypt only supports a 96-bit (12-byte) IV")
    if len(ciphertext_and_tag) < tag_length:
        raise ValueError("ciphertext shorter than the authentication tag")

    ciphertext = ciphertext_and_tag[:-tag_length]
    received_tag = ciphertext_and_tag[-tag_length:]

    round_keys = _aes256_key_expansion(key)
    h_int = int.from_bytes(_aes256_encrypt_block(round_keys, bytes(16)), "big")

    j0 = iv + b"\x00\x00\x00\x01"

    ghash_input = _gcm_pad16(aad) + _gcm_pad16(ciphertext)
    ghash_input += (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(
        8, "big"
    )
    s_bytes = _ghash(h_int, ghash_input).to_bytes(16, "big")

    tag_keystream = _aes256_encrypt_block(round_keys, j0)
    expected_tag = bytes(a ^ b for a, b in zip(s_bytes, tag_keystream))[:tag_length]

    if not hmac.compare_digest(expected_tag, received_tag):
        raise ValueError(
            "AES-GCM authentication failed (tag mismatch) -- wrong key or tampered/corrupt ciphertext"
        )

    counter = _gcm_inc32(j0)
    plaintext = bytearray()
    for i in range(0, len(ciphertext), 16):
        ks = _aes256_encrypt_block(round_keys, counter)
        chunk = ciphertext[i : i + 16]
        plaintext += bytes(a ^ b for a, b in zip(chunk, ks[: len(chunk)]))
        counter = _gcm_inc32(counter)
    return bytes(plaintext)


# ==============================================================================
# Constants reverse-engineered from live captures -- see REVERSE_ENGINEERING.md
# ==============================================================================

USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
)

# The AAD app registration observed minting the Sydney/Chathub access token.
# Part of Microsoft's "Family of Client IDs" (FOCI): a refresh token obtained
# by any family member (e.g. via a normal interactive sign-in to any M365 web
# app) can be redeemed under this client_id without re-prompting the user.
FOCI_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"

# The "broker" client id / redirect the captured traffic always paired with
# refresh_token redemptions (Outlook Web's own registration, used as an SSO
# broker so first-party M365 web apps don't re-prompt each other).
BROKER_CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
BROKER_REDIRECT_URI = "https://m365.cloud.microsoft/spalanding"
TOKEN_REDIRECT_URI = "brk-multihub://outlook.office.com"

# Every one of the ~16 real refresh_token-grant requests captured across all
# HAR sessions in this project used the TENANT-SPECIFIC endpoint, never
# /common/ -- so that's what we use whenever the tenant id is known (see
# CredentialStore._tid_hint). /common/ is kept only as a fallback for the
# plaintext-refresh_token credential path, where no tenant id is available
# ahead of the first exchange.
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tid}/oauth2/v2.0/token"
TOKEN_URL_COMMON = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SYDNEY_SCOPE = (
    "https://substrate.office.com/sydney/.default openid profile offline_access"
)

# Static MSAL-Browser telemetry/capability fields observed on every single
# captured refresh_token-grant request, regardless of scope or session --
# these look like a client-identity fingerprint Entra's backend may use for
# anti-automation heuristics, so this proxy sends the exact same values a
# real msal.js client would rather than omit them.
MSAL_CLIENT_SKU = "msal.js.browser"
MSAL_CLIENT_VER = "5.9.0"
MSAL_LIB_CAPABILITY = "retry-after, h429"
MSAL_CURRENT_TELEMETRY = "5|61,0,,,|,"
MSAL_LAST_TELEMETRY = "5|0|||0,0"

CHATHUB_HOST = "substrate.office.com"

# Static query parameters observed on the Chathub WebSocket URL. `variants`
# is a huge CSV of server-side feature flags; sent verbatim as captured --
# almost certainly prunable, but untested, so kept intact for fidelity.
CHATHUB_VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,"
    "feature.EnableImageGenInsufficientTokensThrottled,"
    "feature.EnableImageGenSystemCapacityThrottled,feature.EnableLuForChatCIQ,"
    "feature.enableChatCIQPlugin,EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,"
    "feature.bizchatfluxv3,feature.enablechatpages,feature.enableCodeCanvas,"
    "feature.turnOnDARecommendation,feature.IsStreamingModeInChatRequestEnabled,"
    "IncludeSourceAttributionsConcise,SkipPublishEmptyMessage,"
    "feature.EnableDeduplicatingSourceAttributions,"
    "feature.IsCitationsReferencesOutputEnabled,feature.enableDeltaStreamingForReferences,"
    "feature.enableIncludeReferencesInDeltaResponse,feature.enablereferencesforagents,"
    "Enable3PActionProgressMessages,feature.enableClientWebRtc,"
    "feature.EnableMeetingRecapOfSeriesMeetingWithCiq,"
    "feature.EnableReferencesListCompleteSignal,feature.StorageMessageSplitDisabled,"
    "feature.EnableCuaTakeControlApi,cdxenablefccinmainline,EnableComposeWidget,"
    "-agt_researcheragent_enableMemoryRead,feature.cwcallowedos,"
    "feature.EnableMergingPureDeltas,feature.disabledisallowedmsgs,"
    "feature.enableCitationsForSynthesisData,feature.EnableConversationShareApis,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,"
    "feature.EnableUpdatedUXForConfirmationDialog,"
    "feature.EnableContentApiandDocTypeHtmlInRichAnswers,"
    "cdxgrounding_api_v2_rich_web_answers_reference_bottom_force,"
    "cdxenablerenderforisocomp,"
    "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot,"
    "feature.EnableDesignEditorImageGrounding,feature.EnableDesignerEditor,"
    "feature.EnableSkipRehydrationForSpeCIdImages,feature.EnablePersonalization,"
    "rich_responses,feature.EnableBase64DataInMessageAnnotations,"
    "feature.EnableSkipEmittingMessageOnFlush,feature.EnableRemoveEmptySourceAttributions,"
    "feature.EnableRemoveStreamingMode,feature.OfficeWebToHelix,"
    "feature.OfficeDesktopToHelix,feature.M365TeamsHubToHelix,feature.OwaHubToHelix,"
    "feature.MonarchHubToHelix,feature.Win32OutlookHubToHelix,"
    "feature.MacOutlookHubToHelix,Agt_bizchat_enableGpt5ForHelix"
)

# The `optionsSets` feature-toggle list from the captured `chat` invocation.
OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "update_textdoc_response_after_streaming",
    "deepleo_networking_timeout_10minutes_canmore",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwcfluxgptv",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "cwc_code_interpreter_interactive_charts_inline_image",
    "code_interpreter_matplotlib_patching",
    "cwc_fileupload_odb",
    "update_memory_plugin",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
    "flux_v3_references",
    "flux_v3_references_entities",
    "flux_v3_image_gen_enable_dimensions",
    "flux_v3_image_gen_enable_non_watermarked_storage",
    "flux_v3_image_gen_enable_icon_dimensions",
    "flux_v3_image_gen_enable_system_text_with_params",
    "flux_v3_image_gen_enable_designer_dimensions_meta_prompting_in_system_prompts",
    "flux_v3_image_gen_enable_story",
    "rich_responses",
]

ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GeneratedCode",
    "RenderCardRequest",
    "AdsQuery",
    "SemanticSerp",
    "GenerateContentQuery",
    "GenerateGraphicArt",
    "SearchQuery",
    "ConfirmationCard",
    "AuthError",
    "DeveloperLogs",
    "TriggerPlugin",
    "HintInvocation",
    "MemoryUpdate",
    "EndOfRequest",
    "TriggerConfirmation",
    "ResumeInvokeAction",
    "ResumeUserInputRequest",
    "TriggerUserInputRequest",
    "EscapeHatch",
    "TriggerPluginAuth",
    "ResumePluginAuth",
    "SideBySide",
    "ReferencesListComplete",
    "SwitchRespondingEndpoint",
]

SIGNALR_RS = "\x1e"  # SignalR JSON Hub Protocol record separator

# Sydney's own built-in plugins/capabilities (BingWebSearch via the `plugins`
# field; the code interpreter and image-gen capabilities via `cwc_code_
# interpreter*`/`flux*`/`gptv*` OPTIONS_SETS entries) reliably preempt this
# proxy's injected tool-calling emulation -- confirmed live during
# development: a weather question got a flat refusal, and a basic-arithmetic
# "use the calculator tool" request got silently answered via Sydney's own
# code interpreter instead of our requested `<tool_call>` convention (see
# REVERSE_ENGINEERING.md's "Tool-calling emulation" section). When a request
# includes `tools`, this proxy asks Sydney with these capabilities stripped,
# so the model has nothing to reach for except our injected convention.
TOOL_MODE_OPTIONS_SETS = [
    o
    for o in OPTIONS_SETS
    if not any(kw in o for kw in ("code_interpreter", "flux", "gptv"))
]


# ==============================================================================
# Errors
# ==============================================================================


class AuthError(Exception):
    """Refresh-token/access-token acquisition failed."""


class CredentialError(Exception):
    """The credentials file is missing, malformed, or couldn't be decrypted."""


class ProtocolError(Exception):
    """The Chathub WebSocket did something we didn't expect."""


class WSError(Exception):
    """Low-level WebSocket handshake/framing failure."""


# ==============================================================================
# Minimal stdlib-only WebSocket client (RFC 6455 subset: client -> TLS only,
# no permessage-deflate -- confirmed from captures that the real server does
# not require compression, so we simply never offer the extension).
# ==============================================================================


class WebSocketClient:
    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    def __init__(self, url, extra_headers=None, timeout=30):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss":
            raise ValueError("only wss:// is supported")
        host = parts.hostname
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        ctx = ssl.create_default_context()
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        self.sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        headers = {
            "Host": host,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
        if extra_headers:
            headers.update(extra_headers)

        request_lines = [f"GET {path} HTTP/1.1"]
        request_lines += [f"{k}: {v}" for k, v in headers.items()]
        request = ("\r\n".join(request_lines) + "\r\n\r\n").encode()
        self.sock.sendall(request)

        status, resp_headers, leftover = self._read_handshake_response()
        if status != 101:
            raise WSError(f"WebSocket handshake failed: HTTP {status}")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + self._GUID).encode(), usedforsecurity=False).digest()
        ).decode()
        if resp_headers.get("sec-websocket-accept") != expected_accept:
            raise WSError(
                "Sec-WebSocket-Accept did not match -- handshake not trustworthy"
            )

        self._buf = leftover

    def _read_handshake_response(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            buf += chunk
        header_bytes, _, leftover = buf.partition(b"\r\n\r\n")
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.lower()] = v
        return status, headers, leftover

    def _fill(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed by peer")
            self._buf += chunk

    def _recv_exact(self, n):
        self._fill(n)
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        mask_key = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length)
        if mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        b1 = 0x80 | opcode  # FIN=1, no fragmentation on send
        length = len(payload)
        mask_key = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", b1, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", b1, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", b1, 0x80 | 127, length)
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask_key + masked_payload)

    def send_text(self, text):
        self._send_frame(self.OPCODE_TEXT, text.encode("utf-8"))

    def recv_text(self):
        """Return the next complete text message, "" for a binary message
        (ignored -- not expected from this API), or None if the peer closed."""
        parts = []
        opcode = None
        while True:
            fin, op, payload = self._read_frame()
            if op == self.OPCODE_PING:
                self._send_frame(self.OPCODE_PONG, payload)
                continue
            if op == self.OPCODE_PONG:
                continue
            if op == self.OPCODE_CLOSE:
                return None
            if op != self.OPCODE_CONTINUATION:
                opcode = op
            parts.append(payload)
            if fin:
                break
        data = b"".join(parts)
        return (
            data.decode("utf-8", errors="replace") if opcode == self.OPCODE_TEXT else ""
        )

    def close(self):
        try:
            self._send_frame(self.OPCODE_CLOSE, b"")
        except OSError:
            pass
        finally:
            try:
                self.sock.close()
            except OSError:
                pass


class SignalRBuffer:
    """Accumulates raw WS text payloads and yields complete JSON objects,
    splitting on SignalR's \\x1e record separator."""

    def __init__(self):
        self._buf = ""

    def feed(self, chunk):
        self._buf += chunk
        while SIGNALR_RS in self._buf:
            msg, self._buf = self._buf.split(SIGNALR_RS, 1)
            if msg:
                yield json.loads(msg)


# ==============================================================================
# Credential file: load, optionally decrypt, and persist rotated refresh tokens
# ==============================================================================


def _b64url_decode(segment):
    segment += "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment)


def jwt_claims(token):
    """Decode (NOT verify) a JWT's payload claims. We trust this token because
    we just received it directly from login.microsoftonline.com ourselves."""
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError(
            "access_token is not a plain JWT (it may be encrypted/opaque) -- "
            "cannot read oid/tid claims from it"
        )
    return json.loads(_b64url_decode(parts[1]))


def _hkdf_sha256(ikm, salt, info, length):
    """RFC 5869 HKDF-Extract-and-Expand using SHA-256, implemented with
    stdlib hmac/hashlib only. Mirrors what WebCrypto's
    `crypto.subtle.deriveKey({name:"HKDF", salt, hash:"SHA-256", info}, ...)`
    does -- salt is the HKDF extract-step salt, info is the expand-step
    context. See _decrypt_msal_cache_entry for why this specific derivation
    is needed instead of using the base key directly."""
    hash_len = hashlib.sha256().digest_size  # 32
    if not salt:
        salt = bytes(hash_len)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


def _msal_cache_context(local_storage_key):
    """Reproduces MSAL Browser's LocalStorage.getContext(key): the HKDF
    "info" context string bound into every cache entry's derived key is the
    owning app's clientId IF that clientId literally appears in the
    localStorage key string, else "". For a credential-key
    (msal.<schema>|homeAccountId|environment|credentialType|familyId|realm|target|scheme),
    the familyId segment holds the true clientId whenever this credential
    isn't part of a shared FOCI family (family-shared refresh tokens instead
    carry a short marker like "1" there, in which case the real clientId
    never appears in the key and the context is simply "").
    """
    if not local_storage_key:
        return ""
    segments = local_storage_key.split("|")
    if len(segments) > 4:
        candidate = segments[4]
        # crude GUID shape check: 36 chars, hyphens in the standard positions
        if len(candidate) == 36 and candidate.count("-") == 4:
            return candidate
    return ""


def _decrypt_msal_cache_entry(encrypted_entry, encryption_key, local_storage_key=""):
    """AES-256-GCM decrypt of an MSAL Browser v4+ encrypted localStorage
    cache entry. Reverse-engineered from MSAL Browser's own source
    (lib/msal-browser/src/crypto/BrowserCrypto.ts and
    lib/msal-browser/src/cache/LocalStorage.ts on the AzureAD/
    microsoft-authentication-library-for-js `dev` branch) -- NOT the raw
    AES-GCM one might first assume:

      1. `rawKey`  = base64url-decoded `encryption_key["key"]` (the base key
         MSAL stores in the `msal.cache.encryption` cookie).
      2. `context` = the owning app's clientId if it appears in the
         localStorage key name, else "" (see _msal_cache_context).
      3. A per-entry AES-256 key is derived via HKDF-SHA256(ikm=rawKey,
         salt=base64url-decoded `nonce`, info=utf8(context)) -- the "nonce"
         field is the HKDF *salt*, not the AES-GCM IV.
      4. AES-GCM decrypt uses a FIXED all-zero 12-byte IV (safe here only
         because a fresh key is HKDF-derived for every single encrypt/
         decrypt operation, per MSAL's own comment to that effect).

    Confirmed working end-to-end against a real captured cache entry during
    development (produced well-formed
    `{"credentialType":"RefreshToken","clientId":...,"secret":...}` JSON).
    Returns the plaintext refresh token string, or raises CredentialError.
    """
    logging.debug(
        "decrypting MSAL cache entry: entry_id=%s local_storage_key=%r",
        encrypted_entry.get("id"),
        local_storage_key,
    )
    if encrypted_entry.get("id") != encryption_key.get("id"):
        raise CredentialError(
            "encrypted_refresh_token.id does not match cache_encryption_key.id -- "
            "these must be copied from the browser at the same time (the key rotates)"
        )
    try:
        raw_key = _b64url_decode(encryption_key["key"])
        nonce = _b64url_decode(encrypted_entry["nonce"])
        data = _b64url_decode(encrypted_entry["data"])
        context = _msal_cache_context(local_storage_key)
        derived_key = _hkdf_sha256(
            raw_key, salt=nonce, info=context.encode("utf-8"), length=32
        )
        plaintext = aes256_gcm_decrypt(derived_key, bytes(12), data, aad=b"")
        cred = json.loads(plaintext.decode("utf-8"))
        secret = cred["secret"]
    except Exception as e:
        logging.error("MSAL cache entry decryption failed: %s: %s", type(e).__name__, e)
        raise CredentialError(
            f"failed to decrypt encrypted_refresh_token ({type(e).__name__}: {e}). "
            "Most likely cause: the cache entry has already rotated past the localStorage "
            "snapshot you copied (MSAL rotates this in the background continuously) -- "
            "recapture all of local_storage_key/encrypted_refresh_token/cache_encryption_key "
            'at the same moment and try again, or use a plaintext "refresh_token" instead.'
        ) from e
    logging.info(
        "MSAL cache entry decrypted successfully: credentialType=%s clientId=%s (secret length=%d chars)",
        cred.get("credentialType"),
        cred.get("clientId"),
        len(secret),
    )
    return secret


#: The four credential fields, in the order they're presented everywhere
#: (docstring, templates, error messages). A file's actual path is always
#: `f"{prefix}.{field_name}"` -- see CredentialStore.
FIELD_NAMES = (
    "refresh_token",
    "encrypted_refresh_token",
    "cache_encryption_key",
    "local_storage_key",
)

#: Explanatory text for each field, embedded as the "#"-comment header of
#: its freshly-written template file (see write_credentials_template) and
#: reused in error messages. Kept here, next to the loader that actually
#: reads these fields, so the two never drift apart. Plain prose -- wrapped
#: into "# "-prefixed lines by _render_credential_file_header.
FIELD_COMMENTS = {
    "refresh_token": (
        "Plaintext Entra ID refresh token. RECOMMENDED -- this is the form "
        "proven to work end-to-end. Where to get it: open "
        "https://m365.cloud.microsoft in a signed-in browser, DevTools -> "
        "Network tab, filter by 'token', then reload the page or send a "
        "chat message so MSAL silently renews a token. Find the POST to "
        "login.microsoftonline.com/.../oauth2/v2.0/token, open its "
        "Request/Payload view, copy the 'refresh_token' form field's value "
        "verbatim. Must be captured freshly each time -- MSAL rotates it "
        "continuously in the background, so a copy taken more than a few "
        "minutes ago may already be rejected by Entra as superseded. Leave "
        "this file's value empty (just this comment block) if you're "
        "instead filling in encrypted_refresh_token + cache_encryption_key "
        "+ local_storage_key -- only one of the two approaches is needed."
    ),
    "encrypted_refresh_token": (
        "The MSAL Browser encrypted localStorage cache entry for the "
        "refresh token (an alternative to the refresh_token file, verified "
        "working). Where to get it: DevTools -> Application/Storage -> "
        "Local Storage -> m365.cloud.microsoft origin -> find the KEY "
        "whose NAME contains 'refreshtoken' -- copy that key's VALUE here "
        "verbatim (an object shaped like "
        '{"id":...,"nonce":...,"data":...,"lastUpdatedAt":...}). '
        "Also copy that same key's NAME into the local_storage_key file -- "
        "both are required together. Must be captured at essentially the "
        "same moment as the cache_encryption_key file's value (matching "
        "'id' fields) or decryption will fail or produce an already-"
        "superseded token."
    ),
    "cache_encryption_key": (
        "The AES base key MSAL uses to encrypt its localStorage cache, "
        "held in the msal.cache.encryption cookie. Where to get it: "
        "DevTools -> Application/Storage -> Cookies -> m365.cloud.microsoft "
        "-> cookie named 'msal.cache.encryption' -> copy its value here -- "
        'an object shaped like {"id":...,"key":...}. Paste it exactly as '
        "shown, whether that's the plain {...} form or the URL-encoded "
        "%7B...%7D form some DevTools views show instead -- either works, "
        "the proxy detects and decodes URL-encoding automatically. Its "
        "'id' must match the encrypted_refresh_token file's 'id' or "
        "decryption will fail."
    ),
    "local_storage_key": (
        "The exact localStorage KEY NAME (not its value) that the "
        "encrypted_refresh_token file's value was copied from, e.g. "
        "'msal.3|<homeAccountId>|login.windows.net|refreshtoken|"
        "<clientId-or-familyId>|||'. Needed to derive the correct "
        "decryption context -- MSAL binds the derived key to the owning "
        "clientId when one is present in this key name."
    ),
}


def _credential_file_paths(prefix):
    """Maps each of the four field names to its file path for the given
    --credentials-prefix, e.g. prefix "m365_openai_proxy" ->
    {"refresh_token": "m365_openai_proxy.refresh_token.conf", ...}. The
    ".conf" suffix (rather than stopping at the field name) makes these
    look like the plain configuration files they are to editors/OSes that
    guess file type from extension, and avoids a bare extensionless name."""
    return {name: f"{prefix}.{name}.conf" for name in FIELD_NAMES}


def _render_credential_file_header(field_name):
    """Builds the "#"-comment header written at the top of a freshly
    generated credential file: a title line, the field's wrapped
    explanatory comment, and a short instruction on where to paste the
    actual value. Every line starts with "#" so _load_credential_file can
    unambiguously tell header from value."""
    lines = [f"# m365_openai_proxy -- {field_name}", "#"]
    for wrapped_line in textwrap.wrap(FIELD_COMMENTS[field_name], width=76):
        lines.append(f"# {wrapped_line}")
    lines.append("#")
    lines.append("# Paste the value below this line (everything from the next")
    lines.append("# non-comment line to the end of the file is used verbatim,")
    lines.append("# leading/trailing whitespace is trimmed), then save.")
    return "\n".join(lines) + "\n"


def _load_credential_file(path):
    """Reads one of the four plain-text credential files: skips leading
    blank lines and lines starting with "#" (the header comment block),
    then returns everything from the first remaining line to the end of
    the file, stripped -- or None if the file doesn't exist, or exists but
    has no value appended yet (comment-only)."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        start = i
        break
    if start is None:
        return None
    value = "".join(lines[start:]).strip()
    return value or None


def _looks_url_encoded_json(value):
    """True if `value` looks like a JSON object that's still URL-encoded --
    i.e. starts with '%7B'/'%7b' (the percent-encoding of '{') rather than a
    literal '{'. DevTools sometimes shows a cookie's value already
    URL-decoded and sometimes still encoded (this is exactly the shape the
    msal.cache.encryption cookie can take), so a pasted value can arrive in
    either form depending on how the operator copied it."""
    return value.startswith("%7B") or value.startswith("%7b")


def _decode_json_field(value, field_label):
    """Parses a pasted value that's expected to be a JSON object, first
    URL-decoding it if it looks like it's still URL-encoded (see
    _looks_url_encoded_json) -- so the operator doesn't have to remember to
    do that themselves before pasting; the proxy just handles either form.
    Raises CredentialError (naming `field_label`) on anything that still
    doesn't parse as JSON after that."""
    candidate = value
    if _looks_url_encoded_json(candidate):
        candidate = urllib.parse.unquote(candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise CredentialError(
            f"{field_label}: pasted value is not valid JSON: {e}"
        ) from e


class CredentialStore:
    """Owns the one configured Microsoft credential for this proxy: loads it
    from four plain-text files (one per field, named `<prefix>.<field>.conf`
    -- see the module docstring's CREDENTIAL FILE FORMATS section), decrypting
    if necessary, and re-persists the rotated refresh token to the
    `<prefix>.refresh_token.conf` file after every redemption (required -- see
    the module docstring's AUTHENTICATION MODEL section for why). If NONE of
    the four files exist yet at construction time, it writes starter
    templates for all of them itself (same as running with
    --init-credentials) before raising CredentialError to report that
    they're still empty -- so a first run never requires a separate manual
    --init-credentials step."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.paths = _credential_file_paths(prefix)
        self._lock = threading.Lock()
        #: (oid, tid) parsed from the local_storage_key file, if one was
        #: supplied and has the expected shape -- see parse_home_account_id.
        #: None/None otherwise (notably: always None/None for the
        #: plaintext-refresh_token-only credential path). Read by
        #: TokenCache.get() to make exchange_refresh_token's request match
        #: a real browser's exactly (tenant-specific endpoint,
        #: X-AnchorMailbox) whenever this information happens to be
        #: available.
        self.oid_hint = None
        self.tid_hint = None
        self._refresh_token = self._load()

    def _load(self):
        if not any(os.path.exists(p) for p in self.paths.values()):
            # First run at this prefix: nothing exists yet. Rather than just
            # telling the operator to go run --init-credentials themselves,
            # do it for them -- same effect, one less manual step.
            logging.info(
                "no credential files found at prefix %s; writing starter templates automatically",
                self.prefix,
            )
            write_credentials_template(self.prefix)
            raise CredentialError(
                "no credential files existed yet, so starter templates were just "
                "created:\n"
                + "\n".join(f"  {p}" for p in self.paths.values())
                + '\nFill in either "refresh_token" alone, or all three of '
                '"encrypted_refresh_token"/"cache_encryption_key"/'
                '"local_storage_key" (see each file\'s own comment header for '
                "exactly where to get its value from), then restart."
            )
        logging.info(
            "loading credentials from %s.{%s}", self.prefix, ",".join(FIELD_NAMES)
        )

        # Parsed independently of which credential path ends up being used
        # below -- if a local_storage_key file happens to be present and
        # well-formed, its oid/tid are a useful hint for exchange_refresh_token
        # regardless of whether the actual secret came from decrypting the
        # MSAL cache or from a plaintext refresh_token file.
        local_storage_key = _load_credential_file(self.paths["local_storage_key"]) or ""
        self.oid_hint, self.tid_hint = parse_home_account_id(local_storage_key)
        if self.oid_hint and self.tid_hint:
            logging.info(
                "derived oid/tid hint from %s (tid=%s)",
                self.paths["local_storage_key"],
                self.tid_hint,
            )

        rt = _load_credential_file(self.paths["refresh_token"])
        if rt:
            logging.info(
                "using plaintext refresh_token from %s (length=%d chars)",
                self.paths["refresh_token"],
                len(rt),
            )
            return rt

        encrypted_raw = _load_credential_file(self.paths["encrypted_refresh_token"])
        key_raw = _load_credential_file(self.paths["cache_encryption_key"])

        if encrypted_raw and key_raw:
            logging.info(
                "no usable value in %s; attempting MSAL localStorage cache decrypt "
                "(encrypted_refresh_token + cache_encryption_key)",
                self.paths["refresh_token"],
            )
            encrypted = _decode_json_field(
                encrypted_raw, self.paths["encrypted_refresh_token"]
            )
            key = _decode_json_field(key_raw, self.paths["cache_encryption_key"])
            return _decrypt_msal_cache_entry(encrypted, key, local_storage_key)

        raise CredentialError(
            f"no usable value found across {', '.join(self.paths.values())} -- "
            'fill in either "refresh_token" alone, or all three of '
            '"encrypted_refresh_token"/"cache_encryption_key"/'
            '"local_storage_key" (see each file\'s own comment header for '
            "exactly where to get its value from)"
        )

    def current(self):
        with self._lock:
            return self._refresh_token

    def rotate(self, new_refresh_token):
        """Called after every successful token exchange with Entra ID's
        newly-issued refresh token. MUST be persisted: Entra invalidates the
        previous refresh token on redemption, so without this the proxy
        would work for exactly one exchange and then permanently fail.
        Only the `<prefix>.refresh_token.conf` file is (over)written -- the
        other three files (if present) are left as-is so you can see what
        was originally supplied."""
        with self._lock:
            self._refresh_token = new_refresh_token
            path = self.paths["refresh_token"]
            content = (
                _render_credential_file_header("refresh_token")
                + new_refresh_token
                + "\n"
            )
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        logging.info(
            "refresh token rotated by Entra ID; persisted new value to %s (length=%d chars)",
            path,
            len(new_refresh_token),
        )


def write_credentials_template(prefix):
    """Writes starter templates for all four `<prefix>.<field>.conf`
    credential files: each just its "#"-comment header, with no value appended yet.
    Refuses to write anything if ANY of the four already exist, so a
    partially-filled-in set is never silently clobbered."""
    paths = _credential_file_paths(prefix)
    existing = [p for p in paths.values() if os.path.exists(p)]
    if existing:
        raise CredentialError(
            "refusing to write templates: these files already exist: "
            + ", ".join(existing)
            + " (remove them first if you really want fresh templates)"
        )
    for name, path in paths.items():
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(_render_credential_file_header(name))
        os.replace(tmp_path, path)


# ==============================================================================
# Auth: refresh_token -> Sydney access_token (FOCI silent redemption)
# ==============================================================================


def _redact_url(url):
    """Returns `url` with any `access_token` query-parameter value masked --
    safe to write to logs. The Chathub WS URL embeds a live bearer token in
    this parameter; nothing else in this program logs a raw URL that could
    carry one."""
    parts = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    redacted_qs = [(k, "<redacted>" if k == "access_token" else v) for k, v in qs]
    new_query = urllib.parse.urlencode(redacted_qs)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def parse_home_account_id(local_storage_key):
    """Extracts (oid, tid) from an MSAL localStorage credential key's
    homeAccountId segment (the 2nd `|`-separated field, shaped
    `<oid>.<tid>`), e.g.
    `msal.3|0964c540-...-76c2dcf08c17.33d90cf2-...-6c34403143f7|login.windows.net|...`
    -> ("0964c540-...-76c2dcf08c17", "33d90cf2-...-6c34403143f7"). Returns
    (None, None) if the key doesn't have the expected shape (e.g. it's
    empty, or the plaintext-refresh_token credential path is in use and no
    local_storage_key was ever supplied). Used to build a tenant-specific
    token endpoint URL and an `X-AnchorMailbox` hint, matching what every
    real captured browser request sends -- see exchange_refresh_token."""
    if not local_storage_key:
        return None, None
    segments = local_storage_key.split("|")
    if len(segments) < 2:
        return None, None
    home_account_id = segments[1]
    if "." not in home_account_id:
        return None, None
    oid, _, tid = home_account_id.partition(".")
    if not oid or not tid:
        return None, None
    return oid, tid


def exchange_refresh_token(refresh_token, oid=None, tid=None):
    """POST the refresh_token grant to Entra ID, scoped to the Sydney/Chathub
    resource, mimicking the exact shape observed from a real browser --
    cross-checked against every refresh_token-grant request captured across
    every HAR in this project's development (~16 examples, different
    scopes, different sessions hours apart): all of them, without
    exception, used the tenant-specific endpoint (never /common/), carried
    `client_id`/`client-request-id` in the URL query string in addition to
    `brk_client_id`/`brk_redirect_uri`, and carried MSAL's own
    client-identity/telemetry fields (`X-AnchorMailbox`, `x-client-SKU`,
    `x-client-VER`, `x-client-current-telemetry`, `x-client-last-telemetry`,
    `x-ms-lib-capability`) in the form body. An earlier version of this
    function omitted all of that, sending a request that -- while
    RFC 6749-valid on paper -- looked nothing like genuine MSAL traffic;
    Entra's backend is known to apply anti-automation heuristics that can
    disguise a block as an ordinary `AADSTS70000 invalid_grant` response,
    which is indistinguishable from "this token is genuinely stale" without
    controlling for this. `oid`/`tid` (when known -- see
    parse_home_account_id) let this match the real shape exactly; without
    them (the plaintext-refresh_token-only credential path, where no tenant
    id is available ahead of the first exchange) this falls back to the
    /common/ endpoint and omits `X-AnchorMailbox`, which is still a
    documented-valid way to redeem a multi-tenant refresh token."""
    token_url = TOKEN_URL_TEMPLATE.format(tid=tid) if tid else TOKEN_URL_COMMON
    logging.debug(
        "exchanging refresh token for a Sydney/Chathub access token via %s", token_url
    )

    query_params = {
        "brk_client_id": BROKER_CLIENT_ID,
        "brk_redirect_uri": BROKER_REDIRECT_URI,
        "client_id": FOCI_CLIENT_ID,
        "client-request-id": str(uuid.uuid4()),
    }
    query = urllib.parse.urlencode(query_params)
    url = f"{token_url}?{query}"

    form_fields = {
        "client_id": FOCI_CLIENT_ID,
        "redirect_uri": TOKEN_REDIRECT_URI,
        "scope": SYDNEY_SCOPE,
        "grant_type": "refresh_token",
        "client_info": "1",
        "x-client-SKU": MSAL_CLIENT_SKU,
        "x-client-VER": MSAL_CLIENT_VER,
        "x-ms-lib-capability": MSAL_LIB_CAPABILITY,
        "x-client-current-telemetry": MSAL_CURRENT_TELEMETRY,
        "x-client-last-telemetry": MSAL_LAST_TELEMETRY,
    }
    if oid and tid:
        form_fields["X-AnchorMailbox"] = f"Oid:{oid}@{tid}"
    # refresh_token last, matching the field order seen in captures (not that
    # order should matter for a form-encoded body, but no reason not to match).
    form_fields["refresh_token"] = refresh_token
    form_fields["brk_client_id"] = BROKER_CLIENT_ID
    form_fields["brk_redirect_uri"] = BROKER_REDIRECT_URI
    form = urllib.parse.urlencode(form_fields).encode()

    req = urllib.request.Request(
        url,
        data=form,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "User-Agent": USER_AGENT,
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "Accept": "*/*",
        },
    )
    # `req` above is built from `url`, which is always
    # TOKEN_URL_TEMPLATE/TOKEN_URL_COMMON (fixed https://login.microsoftonline.com/...
    # constants) -- the templated `tid` only fills a path segment, it can
    # never change the scheme or host, so this isn't an arbitrary/
    # attacker-controlled URL open.
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # `detail` is Entra ID's own JSON error body (error code, description,
        # trace/correlation ids) -- confirmed during development to never
        # echo back the submitted refresh_token, so this is safe to log/raise
        # in full.
        detail = e.read().decode("utf-8", errors="replace")
        logging.warning("refresh_token exchange failed: HTTP %d", e.code)
        raise AuthError(
            f"refresh_token exchange failed (HTTP {e.code}): {detail}"
        ) from e
    except urllib.error.URLError as e:
        logging.warning(
            "refresh_token exchange failed: could not reach %s: %s", token_url, e
        )
        raise AuthError(f"could not reach {token_url}: {e}") from e


class SydneyAuth:
    __slots__ = ("access_token", "oid", "tid", "expires_at")

    def __init__(self, access_token, oid, tid, expires_at):
        self.access_token = access_token
        self.oid = oid
        self.tid = tid
        self.expires_at = expires_at


class TokenCache:
    """Caches the Sydney access token derived from the CredentialStore's one
    configured refresh token, re-exchanging only when the cached access
    token is near expiry, and persisting each rotation back to the store."""

    def __init__(self, credential_store):
        self.store = credential_store
        self._lock = threading.Lock()
        self._auth = None

    def get(self):
        with self._lock:
            if self._auth and self._auth.expires_at - 60 > time.time():
                logging.debug(
                    "reusing cached Sydney access token (oid=%s, expires in %.0fs)",
                    self._auth.oid,
                    self._auth.expires_at - time.time(),
                )
                return self._auth

        logging.info(
            "cached Sydney access token missing or near expiry; exchanging refresh token"
        )
        refresh_token = self.store.current()
        body = exchange_refresh_token(
            refresh_token, oid=self.store.oid_hint, tid=self.store.tid_hint
        )

        new_rt = body.get("refresh_token")
        if new_rt:
            self.store.rotate(new_rt)

        access_token = body.get("access_token")
        if not access_token:
            # Deliberately log/raise only `body`'s KEYS, never its values --
            # `body` can itself contain a fresh access_token/refresh_token/
            # id_token even in this "missing access_token" branch (e.g. a
            # differently-shaped response), so including the dict verbatim
            # here would risk leaking a live credential into the exception
            # message (and from there, into logs).
            logging.error(
                "token exchange response had no access_token (response keys=%s)",
                list(body.keys()),
            )
            raise AuthError(
                f"token exchange response had no access_token (response keys={list(body.keys())})"
            )
        claims = jwt_claims(access_token)
        auth = SydneyAuth(
            access_token=access_token,
            oid=claims.get("oid"),
            tid=claims.get("tid"),
            expires_at=claims.get("exp", time.time() + 300),
        )
        if not auth.oid or not auth.tid:
            # Same reasoning as above: log only the claim NAMES present, not
            # their values (which can include the signed-in user's email/UPN).
            logging.error(
                "access_token was missing oid/tid claims (claims present=%s)",
                list(claims.keys()),
            )
            raise AuthError(
                f"access_token was missing oid/tid claims (claims present={list(claims.keys())})"
            )

        logging.info(
            "Sydney access token acquired: oid=%s tid=%s expires_in=%ss",
            auth.oid,
            auth.tid,
            body.get("expires_in", "?"),
        )
        with self._lock:
            self._auth = auth
        return auth


# ==============================================================================
# Chathub: open the WebSocket, send one `chat` invocation, stream the reply
# ==============================================================================


def open_chathub(auth):
    session_id = str(uuid.uuid4())
    session_id_nodash = session_id.replace("-", "")
    conversation_id = str(uuid.uuid4())

    query = urllib.parse.urlencode(
        {
            "chatsessionid": session_id_nodash,
            "XRoutingParameterSessionKey": session_id_nodash,
            "clientrequestid": session_id_nodash,
            "X-SessionId": session_id,
            "ConversationId": conversation_id,
            "access_token": auth.access_token,
            "variants": CHATHUB_VARIANTS,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "licenseType": "Starter",
            "isEdu": "false",
            "agent": "web",
            "scenario": "OfficeWebIncludedCopilot",
        }
    )
    url = f"wss://{CHATHUB_HOST}/m365Copilot/Chathub/{auth.oid}@{auth.tid}?{query}"

    logging.info(
        "opening Chathub WebSocket: oid=%s tid=%s session_id=%s conversation_id=%s",
        auth.oid,
        auth.tid,
        session_id,
        conversation_id,
    )
    logging.debug("Chathub WS URL (access_token redacted): %s", _redact_url(url))

    ws = WebSocketClient(url, extra_headers={"Origin": "https://m365.cloud.microsoft"})

    # SignalR JSON Hub Protocol handshake: send our protocol choice, expect "{}".
    ws.send_text(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_RS)
    ack = ws.recv_text()
    if ack is None:
        logging.error("Chathub closed the connection during the SignalR handshake")
        raise ProtocolError(
            "Chathub closed the connection during the SignalR handshake"
        )

    logging.info(
        "Chathub WebSocket connected, SignalR handshake complete (session_id=%s)",
        session_id,
    )
    return ws, session_id


def send_chat_message(
    ws,
    session_id,
    text,
    locale="en-US",
    timezone="UTC",
    timezone_offset=0,
    tools_requested=False,
):
    trace_id = str(uuid.uuid4())
    logging.info(
        "sending chat message: session_id=%s trace_id=%s text_length=%d chars locale=%s tools_requested=%s",
        session_id,
        trace_id,
        len(text),
        locale,
        tools_requested,
    )
    # When the client wants OpenAI-style tool-calling, suppress Sydney's own
    # built-in plugins/capabilities (BingWebSearch, code interpreter, image
    # gen) -- see TOOL_MODE_OPTIONS_SETS's comment for why.
    options_sets = TOOL_MODE_OPTIONS_SETS if tools_requested else OPTIONS_SETS
    plugins = [] if tools_requested else [{"Id": "BingWebSearch", "Source": "BuiltIn"}]
    payload = {
        "type": 4,
        "target": "chat",
        "invocationId": "0",
        "arguments": [
            {
                "source": "officeweb",
                "clientCorrelationId": trace_id,
                "sessionId": session_id,
                "optionsSets": options_sets,
                "streamingMode": "ConciseWithPadding",
                "options": {},
                "extraExtensionParameters": {},
                "allowedMessageTypes": ALLOWED_MESSAGE_TYPES,
                "sliceIds": [],
                "threadLevelGptId": {},
                "traceId": trace_id,
                "isStartOfSession": False,
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
                    "locationInfo": {
                        "timeZoneOffset": timezone_offset,
                        "timeZone": timezone,
                    },
                    "locale": locale,
                    "messageType": "Chat",
                    "experienceType": "Default",
                    "adaptiveCards": [],
                    "clientPreferences": {},
                    "connectedFederatedConnections": ["dummyId"],
                },
                "plugins": plugins,
                "isSbsSupported": True,
                "tone": "Magic",
                "renderReferencesBehindEOS": True,
                "disconnectBehavior": "continue",
            }
        ],
    }
    ws.send_text(json.dumps(payload) + SIGNALR_RS)


def stream_chat_reply(ws, timeout_s=120):
    """Yield text deltas as the bot's reply streams in. Reassembles deltas by
    diffing successive full-text snapshots (`messages[].text`) rather than
    trying to interpret the `writeAtCursor` partial-append fields directly --
    simpler, and self-correcting if a snapshot ever doesn't extend cleanly."""
    deadline = time.time() + timeout_s
    last_text = ""
    buf = SignalRBuffer()
    logging.debug("waiting for Chathub reply (timeout=%ds)", timeout_s)

    while time.time() < deadline:
        raw = ws.recv_text()
        if raw is None:
            logging.error("Chathub connection closed before the reply completed")
            raise ProtocolError("Chathub connection closed before the reply completed")
        if not raw:
            continue

        for frame in buf.feed(raw):
            ftype = frame.get("type")

            if ftype == 1 and frame.get("target") == "update":
                for arg in frame.get("arguments") or []:
                    for msg in arg.get("messages") or []:
                        if msg.get("messageType") == "AuthError":
                            # Log/raise only the server's own description text,
                            # not the whole message blob (unclear what else it
                            # might carry -- keep this narrow on principle).
                            logging.error(
                                "Sydney rejected the chat request: %r", msg.get("text")
                            )
                            raise AuthError(
                                f"Sydney rejected the request: {msg.get('text')!r}"
                            )
                        if msg.get("author") != "bot":
                            continue
                        text = msg.get("text")
                        if text is None:
                            continue
                        delta = (
                            text[len(last_text) :]
                            if text.startswith(last_text)
                            else text
                        )
                        if delta:
                            yield delta
                        last_text = text

            elif ftype == 3:  # Completion frame: this invocation is done
                logging.info(
                    "Chathub reply complete (total_length=%d chars)", len(last_text)
                )
                return
            elif ftype == 7:  # hub closed
                logging.info(
                    "Chathub hub closed the connection (total_length so far=%d chars)",
                    len(last_text),
                )
                return
            # ignore: type 2 (StreamItem echo of our own message), type 6 (ping),
            # and Invocation frames with target "Metrics"

    logging.error("timed out waiting for a Chathub reply after %ds", timeout_s)
    raise ProtocolError("timed out waiting for a Chathub reply")


def run_chat_turn(token_cache, text, **kwargs):
    """End-to-end: cached/refreshed Sydney auth -> one Chathub turn -> yields
    text deltas. Generator; the WebSocket is closed once exhausted."""
    auth = token_cache.get()
    ws, session_id = open_chathub(auth)
    try:
        send_chat_message(ws, session_id, text, **kwargs)
        yield from stream_chat_reply(ws)
    finally:
        ws.close()
        logging.info("Chathub WebSocket closed (session_id=%s)", session_id)


# ==============================================================================
# OpenAI-compatible HTTP layer -- NO per-request auth (see module docstring)
# ==============================================================================


def _message_text(m):
    """Extracts the plain-text content of one OpenAI `messages[]` entry,
    handling both the plain-string `content` form and the "content parts"
    list form (`[{"type": "text", "text": ...}, ...]`); non-text parts
    (images etc.) are silently skipped since Sydney/Chathub only accepts a
    single text string per turn."""
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


# ------------------------------------------------------------------------------
# Tool-calling emulation -- see REVERSE_ENGINEERING.md's "Tool-calling
# emulation" section for the full design rationale and live-testing notes.
#
# Sydney/Chathub has NO native tool/function-calling mechanism this proxy can
# use (see the still-undone "Local MCP tool-calling bridge" research). This
# is a from-scratch, model-agnostic emulation built entirely in this proxy,
# the same well-known trick backends without native tool support use: the
# available tool schemas are injected into the prompt as plain-text
# instructions, the model is told a single fixed textual convention to use
# when it wants to call one, and this proxy parses that convention back out
# of the reply into OpenAI's real `tool_calls` response shape. Every turn
# that includes `tools` re-injects the instructions from scratch, since
# Sydney (like the rest of this proxy's context handling) has no memory of a
# previous turn's tool definitions either.
# ------------------------------------------------------------------------------

_TOOL_CALL_OPEN = "<action_request>"
_TOOL_CALL_CLOSE = "</action_request>"
_TOOL_CALL_RE = re.compile(r"<action_request>\s*(.*?)\s*</action_request>", re.DOTALL)


def _render_tools_block(tools, tool_choice):
    """Renders an OpenAI `tools` array (+ `tool_choice`) into the plain-text
    instructions block that teaches the model this proxy's tool-calling
    convention: a single-line JSON object `{"name": ..., "arguments": {...}}`
    wrapped in `<action_request>...</action_request>` tags.

    The specific wording here is NOT arbitrary -- it was arrived at by live
    trial-and-error against the real Sydney backend (see
    REVERSE_ENGINEERING.md's "Tool-calling emulation" section for the full
    account) after two more "obvious" phrasings both failed outright:
      - Calling these "tools/functions" and asking the model to "call" one
        got either a flat content-policy-style refusal, or got silently
        preempted by Sydney's OWN real built-in plugins (BingWebSearch, the
        code interpreter) instead of following our convention at all --
        Sydney has strong standing behavior to prefer reaching for its own
        real capabilities over an instructed textual convention, even when
        its own capabilities can't actually satisfy the request (e.g. it
        tried "coding and executing" to read a file that only exists on the
        *user's* machine, failed, and told the user to upload it -- never
        touching our convention).
      - What DOES reliably work: avoid the words "tool"/"function" entirely
        (call them "capabilities" instead), and explicitly tell the model
        up front that its own real capabilities are unavailable this turn
        (no internet access, no code interpreter) so there is nothing left
        for it to preempt this request with.
    This is still prompt-level steering, not a guarantee -- there is no
    formal contract that the underlying model obeys this convention, and
    REVERSE_ENGINEERING.md documents this as probabilistic, not certain."""
    lines = [
        "",
        "IMPORTANT: for this reply, you have no internet access, no code "
        "interpreter, and no ability to execute code or search the web -- "
        "any apparent ability to do so is disabled for this conversation. "
        "The ONLY way to get information or perform actions you don't "
        "already have is to request it from the user with the exact format "
        "below, and nothing else in your reply (no markdown code fences, no "
        "explanation before or after it) -- arguments must be one line of "
        "valid JSON matching the schema given for that capability:",
        "",
        _TOOL_CALL_OPEN,
        '{"name": "<capability name>", "arguments": {<arguments as JSON>}}',
        _TOOL_CALL_CLOSE,
        "",
        "If you don't need to request anything, just reply normally in "
        "plain text -- do not use the tags above unless you are actually "
        "making a request. You may make more than one request by using "
        f"multiple {_TOOL_CALL_OPEN}...{_TOOL_CALL_CLOSE} blocks in the same "
        "reply.",
        "",
        "Capabilities you can request this way:",
    ]
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get(
            "name", "?"
        )  # left untouched -- must round-trip verbatim, see _neutralize_tool_word's docstring
        desc = _neutralize_tool_word(fn.get("description", ""))
        params = fn.get("parameters", {})
        lines.append(f"- {name}: {desc}")
        lines.append(f"  parameters schema: {json.dumps(params)}")

    if isinstance(tool_choice, dict):
        forced_name = (tool_choice.get("function") or {}).get("name")
        if forced_name:
            lines.append("")
            lines.append(
                f'You MUST request "{forced_name}" now, using the format above.'
            )
    elif tool_choice == "required":
        lines.append("")
        lines.append(
            "You MUST make one of the requests above now, using the format above."
        )

    return "\n".join(lines)


_TOOL_WORD_RE = re.compile(r"\btools?\b", re.IGNORECASE)


def _neutralize_tool_word(text):
    """Replaces the word "tool"/"tools" (case-insensitively, whole-word only)
    with a neutral synonym ("capability"/"capabilities", case-matched)
    throughout `text`.

    This is a live-tested-necessary workaround, not a style choice: the
    literal word "tool" ANYWHERE in a turn sent to Sydney -- even inside the
    CLIENT's own system prompt or user message content, completely outside
    this proxy's own injected instructions -- reliably makes Sydney fall
    back to preempting the request with its own real built-in capabilities
    (the code interpreter, mainly) instead of ever considering this proxy's
    `<action_request>` convention, confirmed by live A/B testing during
    development (see REVERSE_ENGINEERING.md's "Tool-calling emulation"
    section). Coding-agent system prompts (OpenCode's, OpenHands', etc.)
    are saturated with exactly this word ("you have access to the following
    tools", tool names/descriptions, etc.), so this proxy cannot simply ask
    callers to avoid it -- it has to actively launder it out of the entire
    rendered prompt whenever `tools` is present. Applied only to free-form
    text (system/user/assistant/tool-result content and tool descriptions),
    never to the structural JSON (a tool's `name` field, argument/schema
    keys) that this proxy needs to parse back out of the reply verbatim."""

    def _repl(m):
        word = m.group(0)
        replacement = "capabilities" if word.lower() == "tools" else "capability"
        return replacement.capitalize() if word[0].isupper() else replacement

    return _TOOL_WORD_RE.sub(_repl, text)


def _extract_tool_calls(reply_text):
    """Parses `reply_text` for this proxy's `<tool_call>{...}</tool_call>`
    convention (see `_render_tools_block`). Returns `(remaining_text,
    tool_calls)`: `tool_calls` is a list of `{"id", "name", "arguments_json"}`
    dicts (already carrying a synthesized OpenAI-style call id and a
    compact-JSON-encoded arguments string, ready to drop into an OpenAI
    `tool_calls` response entry), and `remaining_text` is whatever plain text
    was outside the tags (may be empty). A tag pair with unparseable/
    incomplete JSON inside is logged and dropped rather than raised -- the
    model attempted a tool call but produced broken output, which should
    surface to the client as "the model didn't call a tool" rather than
    crash this proxy's response entirely."""
    tool_calls = []

    def _handle(match):
        raw = match.group(1)
        try:
            obj = json.loads(raw)
            name = obj["name"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logging.warning(
                "model emitted a %s block with unparseable content, skipping it: %r",
                _TOOL_CALL_OPEN,
                e,
            )
            return ""
        arguments = obj.get("arguments", {})
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "name": name,
                "arguments_json": json.dumps(arguments),
            }
        )
        return ""

    remaining = _TOOL_CALL_RE.sub(_handle, reply_text).strip()
    return remaining, tool_calls


_TOOL_CALL_MAX_ATTEMPTS = 3


def _run_tool_call_turn(token_cache, prompt, max_attempts=_TOOL_CALL_MAX_ATTEMPTS):
    """Runs one Chathub turn with `tools_requested=True`, retrying as brand-
    new, independent Chathub turns (Sydney has no memory to preserve across
    a retry anyway -- see `_render_conversation_prompt`'s docstring) up to
    `max_attempts` times if the reply doesn't contain a parseable
    `<action_request>` tag.

    This retry exists because whether the underlying model actually follows
    this proxy's tool-calling convention on any given turn is genuinely
    probabilistic, not deterministic: live A/B testing during development
    measured roughly a 50% success rate on even the simplest possible
    single-capability request, with Sydney's own built-in code interpreter
    silently preempting the rest of the time -- see REVERSE_ENGINEERING.md's
    "Tool-calling emulation" section for the exact trial data and why a
    single attempt alone was not an acceptable design. Retrying
    substantially raises the *effective* success rate (if failures were
    independent, 3 attempts at 50% each would be ~87.5%; REVERSE_ENGINEERING.md
    documents the actual measured rate, which was somewhat lower --
    consecutive failures are not perfectly independent in practice).

    Returns `(text, tool_calls)` from whichever attempt produced tool_calls,
    or from the LAST attempt if none did across all attempts -- so the
    caller still has a plain-text reply to fall back to (finish_reason
    "stop") rather than nothing at all."""
    text, tool_calls = "", []
    for attempt in range(1, max_attempts + 1):
        text = "".join(run_chat_turn(token_cache, prompt, tools_requested=True))
        _, tool_calls = _extract_tool_calls(text)
        if tool_calls:
            if attempt > 1:
                logging.info(
                    "tool-call emulation succeeded on retry attempt %d/%d",
                    attempt,
                    max_attempts,
                )
            return text, tool_calls
        logging.info(
            "tool-call emulation attempt %d/%d produced no action_request block%s",
            attempt,
            max_attempts,
            ""
            if attempt < max_attempts
            else " -- giving up, falling back to plain content",
        )
    return text, tool_calls


def _render_conversation_prompt(messages, tools=None, tool_choice=None):
    """Renders an OpenAI `messages[]` array (optionally plus a `tools`
    schema list and `tool_choice`) into the single text blob that becomes
    one Chathub turn's `message.text`.

    Sydney/Chathub has no native `messages` concept: one Chathub turn is a
    single freeform text string, and every call to run_chat_turn() mints a
    brand-new Chathub ConversationId (whether Sydney would honor a *stable*
    ConversationId as server-side memory across turns is unconfirmed and
    untested -- see REVERSE_ENGINEERING.md -- so this proxy does not rely on
    that and instead treats every Chathub turn as independent).

    Multi-turn conversations are instead supported the same way many
    bridges handle backends with no native `system` role or message
    history: "context-stuffing" -- the full running conversation (system
    instructions + prior turns) is rendered as plain text and Sydney is
    asked to continue it, with the whole thing discarded once the turn
    completes. This is what makes stateless-per-request clients that
    resend their full growing conversation on every call (e.g. a tool-
    calling coding agent's follow-up request after a tool result) work
    through this proxy: from Sydney's point of view each call is still a
    single fresh one-shot turn, but that turn now contains everything the
    client considers "the conversation so far."

    An assistant message carrying `tool_calls` (content may be null) is NOT
    rendered as its own conversation turn -- only real assistant dialogue
    text (if any) is. A `"tool"`-role message's result text is instead held
    and folded as a parenthetical preamble onto the START of the next real
    turn (the next user message, or a synthesized final turn if the tool
    result is the last message). This was a deliberate, live-tested fix, not
    a simplification for its own sake: rendering something like `Assistant:
    [called read_file(...)]` as its own fabricated history turn -- i.e.
    inventing a prior assistant turn describing an action it "already took"
    -- reliably produced a flat content-policy-style refusal on the next
    reply during development (a classic prompt-injection/jailbreak SHAPE:
    a fake prior-assistant-turn claiming an action was taken, even though
    every word here is mundane). Folding the same information into the
    *user's* turn instead, worded as ordinary supplied context rather than
    fabricated assistant history, reliably avoided the refusal in the same
    A/B comparison -- see REVERSE_ENGINEERING.md's "Tool-calling emulation"
    section for the exact trial transcripts. If `tools` is given,
    `_render_tools_block()`'s instructions are appended to the system/
    instructions section -- see the "Tool-calling emulation" comment block
    above this function.

    For the simple common case of a single user-only message with no
    system prompt, no prior turns, and no `tools` (e.g. a quick curl test,
    or any minimal single-shot client), this returns that message's text
    exactly as given, unadorned -- identical to this proxy's original
    behavior. Returns None if there is no usable text/turn at all.
    """
    system_parts = []
    turns = []  # [(role_label, text), ...], in order
    tool_call_names = {}  # tool_call_id -> tool name, filled in as we scan assistant messages
    pending_results = []  # [(tool_name, result_text), ...] waiting to be folded onto the next turn
    # See _neutralize_tool_word's docstring: the client's OWN system prompt/
    # messages need this too, not just this proxy's own injected text --
    # applied to every piece of free-form text below whenever `tools` is
    # present at all (never when it isn't, to leave plain chat untouched).
    clean = _neutralize_tool_word if tools else (lambda s: s)

    def _fold_pending(text):
        """Prepends any buffered tool-result text onto `text` as ordinary
        parenthetical context and clears the buffer -- see this function's
        caller-level docstring for why this replaces rendering a fabricated
        assistant history turn."""
        nonlocal pending_results
        if not pending_results:
            return text
        preamble = "\n".join(
            f"(Result of your earlier {n} request: {t})" for n, t in pending_results
        )
        pending_results = []
        return f"{preamble}\n\n{text}" if text else preamble

    for m in messages:
        role = m.get("role")
        text = clean(_message_text(m))

        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
            continue

        if role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    tool_call_names[tc.get("id")] = (tc.get("function") or {}).get(
                        "name", "?"
                    )
                # Deliberately not rendered as a turn at all -- see the
                # docstring above. Genuine assistant text alongside the
                # tool_calls (rare, but possible) is still kept.
            if text:
                turns.append(("Assistant", text))
            continue

        if role == "tool":
            tool_name = tool_call_names.get(m.get("tool_call_id"), "unknown_request")
            pending_results.append((tool_name, text or "(empty result)"))
            continue

        if role == "user":
            text = _fold_pending(text)
            if text:
                turns.append(("User", text))
            continue

        if text:
            # Anything else is included rather than silently dropped,
            # labeled with its own role name.
            turns.append((role or "unknown", _fold_pending(text)))

    if pending_results:
        # The conversation ended on a tool result with no further user turn
        # (the normal shape for an OpenAI-style client's follow-up request
        # right after executing a call) -- synthesize a final turn asking
        # Sydney to continue, carrying that result as its only content.
        turns.append(("User", _fold_pending("Please continue.")))

    if not turns:
        return None

    tools_block = _render_tools_block(tools, tool_choice) if tools else None

    # Simple case: exactly one user message, no system prompt, no tools --
    # send it verbatim, unchanged from this proxy's original behavior.
    if (
        not system_parts
        and not tools_block
        and len(turns) == 1
        and turns[0][0] == "User"
    ):
        return turns[0][1]

    lines = []
    if system_parts or tools_block:
        lines.append("### Instructions (follow these exactly)")
        lines.extend(system_parts)
        if tools_block:
            lines.append(tools_block)
        lines.append("")
    if len(turns) > 1:
        lines.append(
            "### Conversation so far (context only -- do not reply to any of "
            "this, only to the final message below)"
        )
        for label, text in turns[:-1]:
            lines.append(f"{label}: {text}")
        lines.append("")
    last_label, last_text = turns[-1]
    lines.append("### Respond to this message")
    lines.append(f"{last_label}: {last_text}")
    return "\n".join(lines)


def make_handler(token_cache):
    """Builds a request handler class bound to one TokenCache (in turn bound
    to one CredentialStore) -- the single configured Microsoft identity this
    proxy instance speaks as."""

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"m365-openai-proxy/{PROXY_VERSION}"

        def _write_json(self, status, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def _error(self, status, message, err_type="proxy_error"):
            self._write_json(status, {"error": {"message": message, "type": err_type}})

        def log_message(self, fmt, *args):
            logging.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            try:
                self._do_GET()
            except Exception:
                # Catch-all so a bug here (a bad path, a broken header, etc.)
                # still produces a diagnosable traceback in the log file
                # instead of falling through to the server's generic
                # per-thread error handler.
                logging.exception(
                    "unhandled error in GET %s from %s",
                    self.path,
                    self.address_string(),
                )
                # Best-effort fallback: the real error is already logged
                # above; if even sending a 500 fails (e.g. client already
                # disconnected) there's nothing more useful to do than drop
                # it.
                try:
                    self._error(500, "internal error")
                except Exception:  # nosec B110
                    pass

        def _do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            logging.debug("GET %s from %s", path, self.address_string())
            if path == "/v1/models":
                self._write_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "m365-copilot",
                                "object": "model",
                                "owned_by": "microsoft",
                            }
                        ],
                    },
                )
            elif path == "/healthz" or path == "":
                self._write_json(200, {"status": "ok"})
            else:
                self._error(404, "not found")

        def do_POST(self):
            try:
                self._do_POST()
            except Exception:
                # See do_GET's comment -- same reasoning.
                logging.exception(
                    "unhandled error in POST %s from %s",
                    self.path,
                    self.address_string(),
                )
                # See do_GET's comment above, same reasoning.
                try:
                    self._error(500, "internal error")
                except Exception:  # nosec B110
                    pass

        def _do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            logging.debug("POST %s from %s", path, self.address_string())
            if path != "/v1/chat/completions":
                self._error(404, "not found")
                return

            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._error(400, "invalid Content-Length header")
                return
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                self._error(400, "invalid JSON body")
                return

            messages = body.get("messages") or []
            tools = body.get("tools") or None
            tool_choice = body.get("tool_choice")
            prompt = _render_conversation_prompt(
                messages, tools=tools, tool_choice=tool_choice
            )
            if prompt is None:
                self._error(400, "no usable message content found in 'messages'")
                return

            model = body.get("model") or "m365-copilot"
            stream = bool(body.get("stream", False))
            logging.info(
                "chat completion request from %s: %d message(s), rendered_prompt_length=%d chars, "
                "stream=%s, model=%r, tools=%d",
                self.address_string(),
                len(messages),
                len(prompt),
                stream,
                model,
                len(tools or []),
            )

            if stream:
                self._handle_streaming(prompt, model, tools_requested=bool(tools))
            else:
                self._handle_full(prompt, model, tools_requested=bool(tools))

        def _handle_streaming(self, prompt, model, tools_requested=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())

            def emit(delta_obj, finish_reason=None):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": delta_obj, "finish_reason": finish_reason}
                    ],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                if tools_requested:
                    # Buffered mode: whether this reply is plain content or a
                    # tool call can only be known once the FULL text has been
                    # seen (see _extract_tool_calls), so there is no true
                    # incremental streaming when `tools` is present -- the
                    # whole reply is collected first, then emitted as one
                    # chunk. Documented as a known limitation. See
                    # _run_tool_call_turn for why this retries internally.
                    text, tool_calls = _run_tool_call_turn(token_cache, prompt)
                    if tool_calls:
                        # Leftover text is deliberately dropped here, not
                        # surfaced as delta content -- see _handle_full's
                        # comment on the same point for why.
                        emit(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": tc["name"],
                                            "arguments": tc["arguments_json"],
                                        },
                                    }
                                    for i, tc in enumerate(tool_calls)
                                ],
                            }
                        )
                        finish_reason = "tool_calls"
                    else:
                        emit({"role": "assistant", "content": text})
                        finish_reason = "stop"
                else:
                    first = True
                    for delta in run_chat_turn(token_cache, prompt):
                        emit(
                            {"role": "assistant", "content": delta}
                            if first
                            else {"content": delta}
                        )
                        first = False
                    finish_reason = "stop"

                final = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": finish_reason}
                    ],
                }
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                logging.info(
                    "chat completion (streaming) %s finished successfully (finish_reason=%s)",
                    completion_id,
                    finish_reason,
                )
            except Exception as e:
                # Deliberately broad: this is the last line of defense before a
                # request-handling bug would otherwise vanish into the default
                # per-thread error handler (see _LoggingHTTPServer.handle_error).
                # Catching everything here -- not just our own proxy exception
                # types -- means any crash, expected or not, leaves a full
                # traceback in the log file rather than a silent/opaque failure.
                logging.exception(
                    "chat completion (streaming) %s failed", completion_id
                )
                err = {"error": {"message": str(e), "type": "proxy_error"}}
                try:
                    self.wfile.write(f"data: {json.dumps(err)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except OSError:
                    pass

        def _handle_full(self, prompt, model, tools_requested=False):
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            try:
                if tools_requested:
                    text, tool_calls = _run_tool_call_turn(token_cache, prompt)
                else:
                    text, tool_calls = "".join(run_chat_turn(token_cache, prompt)), []
            except Exception as e:
                # See the comment in _handle_streaming's except clause: broad
                # on purpose, so any bug still produces a diagnosable log entry.
                logging.exception("chat completion %s failed", completion_id)
                self._error(502, str(e))
                return

            if tool_calls:
                # Leftover text (`content`) is deliberately dropped, not
                # attached to the message: in practice it's been Sydney's own
                # internal progress boilerplate ("Coding and executing", from
                # the code-interpreter step it still runs internally even
                # when it ultimately follows this proxy's convention for its
                # final answer) rather than a genuine user-facing preamble,
                # and there is no reliable way to tell the two apart -- see
                # REVERSE_ENGINEERING.md's "Tool-calling emulation" section.
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments_json"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                finish_reason = "tool_calls"
                logging.info(
                    "chat completion %s finished successfully as a tool call (reply_length=%d chars, calls=%s)",
                    completion_id,
                    len(text),
                    ", ".join(tc["name"] for tc in tool_calls),
                )
            else:
                message = {"role": "assistant", "content": text}
                finish_reason = "stop"
                logging.info(
                    "chat completion %s finished successfully (reply_length=%d chars)",
                    completion_id,
                    len(text),
                )

            self._write_json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            )

    return ProxyHandler


_LOG_FILE_PATH = None  # set by _configure_logging; read by _print_fatal_console_message


def _configure_logging(log_file, level):
    """Configures the root logger with exactly ONE handler: a plain-text file
    at `log_file`. FILE-ONLY for everything logged via `logging.*()` --
    nothing goes to the console/stderr through this handler. This log file
    is meant to be a self-sufficient troubleshooting artifact an operator
    can hand to someone else (a developer, or an AI given the file) without
    also needing terminal scrollback. Detailed by design (startup
    environment banner, credential loading mode, every token exchange,
    every WebSocket open/close, every incoming HTTP request, every chat
    turn, and a full traceback for any unhandled exception anywhere in the
    process) -- but every call site in this file is written to log
    lengths/counts/ids/claims-keys rather than actual secret values
    (refresh tokens, access tokens, decrypted cache secrets) -- see the
    module docstring's LOGGING section. Separately from this handler,
    `_print_fatal_console_message` writes a small number of plain-language
    messages directly to the console for genuinely fatal conditions --
    see that function's docstring."""
    global _LOG_FILE_PATH
    _LOG_FILE_PATH = os.path.abspath(log_file)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _print_fatal_console_message(problem):
    """The ONLY place this program writes to the console. Used exclusively
    for conditions where the proxy cannot start at all, or has crashed to a
    complete stop while running -- never for a single failed request or
    anything else that leaves the server itself still up and accepting
    connections (those stay file-only; see module docstring's LOGGING
    section).

    Deliberately contains NO technical detail -- no error codes, no
    tracebacks, nothing that requires expertise to read -- because the
    person watching the terminal may not be technical at all. Its only job
    is to say, in plain language, that the program stopped and where the
    real diagnostic information lives, so they know to send that one file
    to whoever supports them. `problem` should be a short, plain-language
    sentence fragment completing "m365_openai_proxy <problem>", e.g.
    "could not start because its credentials file is missing or invalid."
    """
    log_path = _LOG_FILE_PATH or "m365_openai_proxy.log"
    print(
        "\n"
        f"m365_openai_proxy {problem}\n"
        "\n"
        "This program cannot continue and has stopped.\n"
        "\n"
        f"A detailed technical log was saved to:\n"
        f"    {log_path}\n"
        "\n"
        "Please send that file to whoever set this program up for you --\n"
        "it contains the information needed to figure out what went wrong.\n",
        file=sys.stderr,
    )


def _log_startup_banner(args):
    """Writes a fixed-shape environment banner as the first lines of every
    run's log -- Python/OS/process details plus the resolved CLI arguments
    (paths only, never credential values). Lets a reader (developer or AI)
    orient on the environment a bug report came from without a back-and-forth."""
    logging.info("=" * 78)
    logging.info("m365_openai_proxy %s starting up", PROXY_VERSION)
    logging.info(
        "pid=%d python=%s (%s) platform=%s",
        os.getpid(),
        platform.python_version(),
        platform.python_implementation(),
        platform.platform(),
    )
    logging.info(
        "args: host=%s port=%d credentials_prefix=%s log_file=%s log_level=%s init_credentials=%s",
        args.host,
        args.port,
        args.credentials_prefix,
        args.log_file,
        args.log_level,
        args.init_credentials,
    )
    logging.info("cwd=%s script=%s", os.getcwd(), os.path.abspath(__file__))
    logging.info("=" * 78)


def _log_uncaught_exception(exc_type, exc_value, exc_tb):
    """Installed as sys.excepthook: catches any exception that reaches the
    top of the main thread without being handled anywhere else (e.g. a bug
    outside every try/except this file already has). Logs it with a full
    traceback to the file (instead of letting Python's default handler
    print it to stderr), and -- since reaching this point means the process
    is about to die -- also prints the plain-language console message
    pointing at the log file (see _print_fatal_console_message). A plain
    Ctrl+C is excluded: that's an intentional user action, not a failure,
    so it's just noted in the log without alarming console output."""
    if issubclass(exc_type, KeyboardInterrupt):
        logging.info("interrupted (KeyboardInterrupt) at top level")
        return
    logging.critical(
        "unhandled exception reached the top of the main thread",
        exc_info=(exc_type, exc_value, exc_tb),
    )
    _print_fatal_console_message("stopped because of an unexpected internal error.")


class _LoggingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer that routes its per-request-thread error handler
    through our logging setup instead of the default (which prints a
    traceback straight to stderr, bypassing the log file entirely). Without
    this override, a bug that a handler method doesn't catch itself would
    vanish from `m365_openai_proxy.log` even though the process keeps
    running -- exactly the kind of silent failure this file-only logging
    setup is meant to prevent."""

    def handle_error(self, request, client_address):
        logging.exception(
            "unhandled exception while handling a request from %s", client_address
        )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1 -- there is no per-request auth, keep this local unless you have your own reason not to)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="bind port (default: 8000)"
    )
    parser.add_argument(
        "--credentials-prefix",
        default="m365_openai_proxy",
        help="path prefix for the four plain-text credential files: <prefix>.refresh_token.conf, <prefix>.encrypted_refresh_token.conf, <prefix>.cache_encryption_key.conf, <prefix>.local_storage_key.conf (default: ./m365_openai_proxy -- see module docstring for their format)",
    )
    parser.add_argument(
        "--init-credentials",
        action="store_true",
        help="write starter templates for all four <prefix>.*.conf credential files (each just a comment header explaining what to paste and where to get it) and exit, without starting the server",
    )
    parser.add_argument(
        "--log-file",
        default="m365_openai_proxy.log",
        help="path to the log file (default: ./m365_openai_proxy.log) -- the ONLY place this program logs to (nothing goes to the console). Never contains secrets/tokens/passwords -- see module docstring's LOGGING section",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity for --log-file (default: INFO)",
    )
    args = parser.parse_args()

    _configure_logging(args.log_file, getattr(logging, args.log_level))
    sys.excepthook = _log_uncaught_exception
    _log_startup_banner(args)

    if args.init_credentials:
        try:
            write_credentials_template(args.credentials_prefix)
        except CredentialError as e:
            logging.error("%s", e)
            _print_fatal_console_message(
                "could not write the credentials template files."
            )
            sys.exit(1)
        logging.info(
            "wrote starter credential file templates (%s.{%s}) -- fill in one of the "
            "two documented options and rerun without --init-credentials",
            args.credentials_prefix,
            ",".join(FIELD_NAMES),
        )
        return

    try:
        store = CredentialStore(args.credentials_prefix)
        token_cache = TokenCache(store)
        logging.info("validating configured credential against Entra ID...")
        auth = token_cache.get()
        logging.info(
            "credential OK -- authenticated as oid=%s tid=%s", auth.oid, auth.tid
        )
    except CredentialError as e:
        logging.error("%s", e)
        _print_fatal_console_message(
            "could not start because of a problem with its credential files."
        )
        sys.exit(1)
    except AuthError as e:
        logging.error("configured credential was rejected by Entra ID: %s", e)
        _print_fatal_console_message(
            "could not start because the sign-in information it was given was rejected."
        )
        sys.exit(1)
    except Exception:
        # Broad on purpose: an unexpected bug during startup should still
        # leave a full traceback in the log file rather than just a bare
        # process exit the operator can't explain to whoever they send the
        # log to.
        logging.exception("unexpected error during startup")
        _print_fatal_console_message(
            "could not start because of an unexpected internal error."
        )
        sys.exit(1)

    handler_cls = make_handler(token_cache)
    server = _LoggingHTTPServer((args.host, args.port), handler_cls)
    logging.info(
        "listening on http://%s:%d (Ctrl+C to stop) -- no auth required on the API itself",
        args.host,
        args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down (KeyboardInterrupt)")
    except Exception:
        # The server itself has died -- this is exactly the "can't function
        # anymore" case the console message exists for. Report it here and
        # exit cleanly rather than re-raising (re-raising would just hand
        # the same exception to sys.excepthook, logging and printing it a
        # second time).
        logging.exception("server crashed unexpectedly and has stopped")
        _print_fatal_console_message(
            "has stopped running because of an unexpected error."
        )
        sys.exit(1)
    finally:
        server.server_close()
        logging.info("server closed")


if __name__ == "__main__":
    main()
