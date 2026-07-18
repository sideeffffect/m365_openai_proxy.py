# m365_openai_proxy.py

A single, self-contained, stdlib-only Python 3 script that exposes an
OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) backed by
`https://m365.cloud.microsoft`'s Copilot chat backend.

> **Before you integrate this with anything:** each `/v1/chat/completions`
> call is a fresh, context-free, one-shot Sydney conversation — the proxy
> sends only the *last* message in `messages` and discards the rest. There
> is currently **no multi-turn conversation memory**. See "Known
> limitations" below before wiring this into a chat UI that expects normal
> follow-up-question behavior.

**Fully self-contained.** The entire project is the one file,
`m365_openai_proxy.py`. It uses only the Python 3 standard library — no
`pip install` of anything, for any feature, ever. That includes the
MSAL-encrypted-cache decrypt path, which needs AES-256-GCM: the script
implements that itself in pure Python (validated against the FIPS-197
AES-256 test vector and cross-checked against a reference implementation
during development, then shipped dependency-free). Drop this one file onto
any machine with a Python 3 interpreter and run it — nothing else to
install.

**No Authorization header or API key is required from callers of the HTTP
API.** All Microsoft authentication is handled internally by the proxy,
configured once at startup from four plain-text credential files next to
the script (see below). Each file is just a `#`-comment header explaining
what it is and exactly where in the browser (Local Storage / Cookies /
Network tab) to get it from, followed by the raw value pasted at the
bottom — no JSON escaping needed, even though two of the values are
themselves JSON snippets copied straight out of DevTools.

The four files are named `m365_openai_proxy.<field>.conf`:

- `m365_openai_proxy.refresh_token.conf`
- `m365_openai_proxy.encrypted_refresh_token.conf`
- `m365_openai_proxy.cache_encryption_key.conf`
- `m365_openai_proxy.local_storage_key.conf`

Only one of two combinations needs to actually be filled in (both are
verified working):
- `refresh_token` alone (a plaintext refresh token), or
- `encrypted_refresh_token` + `cache_encryption_key` + `local_storage_key`
  together (MSAL Browser v4+ encrypts its cache entry with HKDF-derived
  AES-GCM, reverse-engineered from MSAL's own source and implemented in
  `_decrypt_msal_cache_entry()`).

**Logging.** Every run writes a detailed log to `m365_openai_proxy.log` next
to the script. This is file-only for normal operation — startup/shutdown,
which credential file was used, every token exchange and Chathub
connection, every incoming HTTP request, every chat turn's outcome.
Secrets, tokens, and passwords are never written to it — only lengths,
ids, and other non-secret metadata. The console stays silent except for
the one case where the proxy genuinely can't continue (it fails to start,
or crashes to a complete stop) — then it prints a short, non-technical
message telling you to send `m365_openai_proxy.log` to whoever supports
this program, since that file has everything needed to diagnose it. See
the script's module docstring's LOGGING section for exactly what's logged.

See the top of `m365_openai_proxy.py`'s module docstring for:

- the full field-by-field credential file format and exactly how to obtain
  each value,
- the full HKDF+AES-GCM decrypt algorithm, implemented from scratch in pure
  Python,
- the exact request shape (tenant-specific token endpoint, MSAL
  identity/telemetry fields) `exchange_refresh_token()` sends and why —
  matching this precisely turned out to matter: an earlier revision that
  omitted it had otherwise-valid tokens rejected by Entra ID as if they
  were stale, which they weren't (see REVERSE_ENGINEERING.md for the full
  writeup),
- the full authentication model and why bind address defaults to
  `127.0.0.1`.

## Known limitations

- **No multi-turn conversation memory.** Only the last `"user"`-role
  message in `messages` is sent; every call starts a brand-new Sydney
  conversation. Prior turns and system prompts are silently discarded.
- **No tool/function calling.** Sydney does have a real MCP-based tool-
  invocation mechanism, reverse-engineered from the officeweb client and
  documented in `REVERSE_ENGINEERING.md`'s "Local MCP tool-calling bridge"
  section — but it is **not implemented here**. Bridging it to OpenAI's
  tool-calling contract runs into a genuine architecture mismatch (Sydney's
  invocation is synchronous and mid-turn; OpenAI tool-calling is async
  across separate HTTP requests). If you're evaluating this for an agentic
  coding client like OpenCode that needs to edit files/run commands via
  tool calls, assume that will not work.
- `usage` (token counts) in every response is always zero — no token
  counting is implemented.
- One Chathub WebSocket is opened and closed per HTTP request — no
  connection pooling or reuse.
- Sydney's own per-conversation rate limiting isn't specially handled or
  surfaced; if you hit a quota, whatever Sydney returns is passed through
  as-is.
- Credential values (especially a plaintext refresh token grabbed from the
  Network tab) can go stale if MSAL rotates them in the background before
  you finish pasting them in — capture the credential files close together
  in time as good practice, though this is a secondary risk, not the
  primary failure mode it was once thought to be (see
  REVERSE_ENGINEERING.md).

## Quick start

```bash
# 1. Write starter templates for all four credential files (each just a
#    comment header explaining what to paste and where to get it):
python3 m365_openai_proxy.py --init-credentials

# 2. Open m365_openai_proxy.refresh_token.conf and paste your refresh
#    token below the comment block -- OR, if using the encrypted-cache
#    option instead, fill in m365_openai_proxy.encrypted_refresh_token.conf
#    + .cache_encryption_key.conf + .local_storage_key.conf the same way.
#    Each file's own comment says exactly where to get its value from.

# 3. Run it (validates the credential against Entra ID on startup, and
#    starts logging to m365_openai_proxy.log):
python3 m365_openai_proxy.py --port 8000

# 4. Talk to it like any OpenAI-compatible endpoint -- no auth header:
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "m365-copilot", "messages": [{"role": "user", "content": "hello"}]}'
```

The proxy overwrites the `m365_openai_proxy.refresh_token.conf` file after
every token exchange with Entra's newly-rotated refresh token (Entra
invalidates the previous one on each redemption) — the other three files
are left untouched.

Run `python3 m365_openai_proxy.py --help` for all flags, including
`--host`/`--port`, `--credentials-prefix`, `--log-file`/`--log-level`.

See `REVERSE_ENGINEERING.md` for the full protocol reverse-engineering
writeup this implementation is based on (Chathub/SignalR wire format, the
FOCI token-family auth chain, the MSAL cache-encryption algorithm, etc).

## Requirements

Python 3 standard library only, no third-party packages. Developed and
tested against Python 3.12; no version-specific stdlib features are
knowingly used, but only 3.12 has been verified.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
