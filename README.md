# m365_openai_proxy.py

A single, self-contained, stdlib-only Python 3 script that exposes an
OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) backed by
`https://m365.cloud.microsoft`'s Copilot chat backend.

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
- the one caveat that applies to *every* credential-harvesting method here:
  MSAL rotates the underlying refresh token in the background continuously,
  so whichever values you copy must be captured close together in time and
  used promptly,
- the full authentication model and why bind address defaults to
  `127.0.0.1`.

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
