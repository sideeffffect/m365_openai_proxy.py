# m365_openai_proxy.py

A single, self-contained, stdlib-only Python 3 script that exposes an
OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) backed by
`https://m365.cloud.microsoft`'s Copilot chat backend.

> **Before you integrate this with anything:** multi-turn conversations now
> reuse Sydney's own server-side conversation memory when possible (live-
> confirmed — see REVERSE_ENGINEERING.md's "Sydney-native conversation
> continuity" section), falling back automatically to the original
> "context-stuffing" behavior (rendering the *entire* `messages` array into
> one text blob per Chathub turn) whenever that can't be established —
> `tools`-using requests always take the context-stuffing path for now (see
> "Known limitations"). Tool/function calling (`tools` / `tool_calls`) is
> implemented, but it is **emulated by prompting, on a backend with no
> native concept of it, and it is probabilistic — not a guarantee**. See
> "Known limitations" before depending on it for a real agent loop. **If
> your client is OpenHands, prefer its own client-side "mock function
> calling" mode over this proxy's `tools` emulation — it is dramatically
> more reliable (3/3 vs. ~coin-flip in testing); see "Using this with a
> coding agent" below.**

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

- **Multi-turn memory now prefers Sydney's own native conversation state,
  with context-stuffing kept as an automatic fallback.** A live experiment
  confirmed Sydney honors a `ConversationId` reused across a brand-new,
  independent Chathub WebSocket connection as real server-side conversation
  memory — see REVERSE_ENGINEERING.md's "Sydney-native conversation
  continuity" section for the full transcript. When an incoming request's
  `messages[]` array is recognized as an exact continuation of a
  conversation this proxy already relayed (and the request has no `tools`
  — see below), only the newest message is sent on the reused
  `ConversationId`, instead of re-rendering and resending the whole growing
  transcript every time. Any time that can't be established with
  confidence — the first turn of a conversation, edited/branched history,
  `tools` present, or the proxy having restarted (this is in-memory only,
  never persisted) — it falls back to the original behavior: render the
  whole `messages` array into one text blob and send it as a single turn on
  a brand-new `ConversationId`. This is fully additive: it never changes
  what a request can produce, only how much has to be resent to get it.
  Disable it entirely with `--disable-conversation-continuity` if you ever
  need the old always-context-stuff behavior.
- **Tool/function calling is emulated by prompting, and is probabilistic.**
  Sydney has no native `tools`/`tool_calls` mechanism at all. When a request
  includes `tools`, this proxy injects plain-text instructions teaching the
  model a fixed convention (`<action_request>{"name":...,"arguments":{...}}
  </action_request>`) and parses that back out of the reply into a real
  OpenAI `tool_calls` response — see `m365_openai_proxy.py`'s module
  docstring and `REVERSE_ENGINEERING.md`'s "Tool-calling emulation" section
  for the full live-testing account. In short: Sydney's own built-in code
  interpreter frequently preempts this convention instead of following it
  (even for things it structurally can't do, like reading a file from
  *your* machine), and even under the best measured conditions a single
  attempt only followed the convention about half the time. This proxy
  retries automatically (up to 3 attempts per turn) to raise the effective
  success rate, and folds tool results back in as a way that avoids a
  refusal-triggering failure mode found during testing — but this remains
  fundamentally probabilistic, not a guaranteed contract. Expect an
  occasional tool call to just not happen (the model replies with plain
  text instead) even after retries, especially on longer/more complex
  system prompts. Sydney's own REAL tool-invocation mechanism (Local MCP)
  is reverse-engineered and documented in `REVERSE_ENGINEERING.md`'s "Local
  MCP tool-calling bridge" section but remains **unimplemented** — it would
  sidestep all of the above, at the cost of a genuine architecture mismatch
  (Sydney's invocation is synchronous/mid-turn; OpenAI tool-calling is
  async across separate HTTP requests) that's a substantial separate
  project, not a quick patch.
- `usage` (token counts) in every response is always zero — no token
  counting is implemented.
- One Chathub WebSocket is opened and closed per HTTP request — no
  connection pooling or reuse.
- Sydney's own per-conversation rate limiting (the `throttling:
  {maxNumUserMessagesInConversation, ...}` field) isn't specially handled
  or surfaced; if you hit that quota, whatever Sydney returns is passed
  through as-is. Its separate, apparently account/backend-wide "too much
  volume right now" rate limit IS now surfaced properly, though (found
  live while testing conversation continuity above): it used to arrive in
  a WebSocket frame shape this proxy silently ignored, so a throttled turn
  used to complete as a normal, empty, HTTP 200 response with no
  indication anything was wrong — now it raises a clear error instead
  (`ThrottledError`, surfaced as a 429/`upstream_throttled` with Sydney's
  own refusal text -- the status code OpenAI clients already back off and
  retry on; a silent empty reply, the other observed throttle shape, is
  surfaced the same way).
- Credential values (especially a plaintext refresh token grabbed from the
  Network tab) can go stale if MSAL rotates them in the background before
  you finish pasting them in — capture the credential files close together
  in time as good practice, though this is a secondary risk, not the
  primary failure mode it was once thought to be (see
  REVERSE_ENGINEERING.md).

## Using this with a coding agent

The goal of this project is to let an existing coding-agent CLI drive its
edits/chat using `m365.cloud.microsoft`'s Copilot as the model backend,
without that agent knowing it isn't talking to a normal OpenAI-compatible
API. Whether a given agent needs `tools`/`tool_calls` at the API level (and
therefore depends on this proxy's probabilistic emulation of them) or drives
file/shell operations itself from plain-text output determines how well it
will actually work:

- **[Aider](https://aider.chat/)** — works well, with no dependency on tool-
  calling emulation at all. Aider never asks the model to emit `tool_calls`;
  it asks for a diff/whole-file rewrite in plain text and applies it itself,
  and it resends the full running conversation on every request (rendered
  via this proxy's context-stuffing). Point it at this proxy with an
  OpenAI-compatible model config, e.g.:

  ```bash
  aider --openai-api-base http://127.0.0.1:8000/v1 --openai-api-key sk-unused \
    --model openai/m365-copilot
  ```

  (Aider's config still wants *some* string in `--openai-api-key`; this
  proxy ignores it entirely — see AUTHENTICATION MODEL above.)

- **[OpenCode](https://opencode.ai/)** — **tested against the real OpenCode
  CLI and did not work.** OpenCode is built on the Vercel AI SDK's
  `generateText({ tools })` loop with no plain-text fallback, so it depends
  entirely on this proxy's emulated `tool_calls` (see "Known limitations"
  above). Three separate full end-to-end sessions were run against a real
  OpenCode CLI (via a custom `@ai-sdk/openai-compatible` provider pointed at
  this proxy) on a simple "read this file and add a function to it" task;
  all three failed to produce a single working tool call — OpenCode's real
  system prompt plus its full tool schema list runs 30-40KB, and neither a
  recency-bias mitigation nor raising the retry budget to 5 changed the
  outcome (see REVERSE_ENGINEERING.md's "production-scale end-to-end
  validation" section for the full account, including the exact config
  used). Configuring OpenCode against this proxy is described there if you
  want to try it yourself, but the honest current answer is: it does not
  work yet — unlike OpenHands, OpenCode has no equivalent client-side "mock
  function calling" fallback to fall back on.

- **[OpenHands CLI](https://docs.openhands.dev/)** — **the recommended way
  to use this proxy with an agentic coding CLI, via its own client-side
  "mock function calling" mode rather than this proxy's `tools` emulation —
  but not unconditionally reliable; read the caveat below before depending
  on it for real debugging work.** OpenHands' SDK has a
  `native_tool_calling=False` flag on its `LLM` config that converts tool
  schemas to text in the prompt itself and parses the reply back into real
  tool calls **entirely on the OpenHands side** — it sends this proxy
  plain `messages` with no `tools` field at all, so this proxy's own
  probabilistic emulation never even runs.

  An initial test succeeded 3 of 3 sessions. A much larger, deliberately
  adversarial follow-up load test (26 full sessions across 7 batches —
  single-file edits, multi-step tasks requiring a `terminal` verification
  step, multi-file edits, three genuinely concurrent sessions against the
  same proxy instance, and an iterative debug loop) found:
  - **17 of 17 (100%) succeeded** on ordinary implement/edit/verify task
    shapes — single edits, edit-then-verify chains, multi-file edits, and
    concurrent sessions all passed completely, each independently verified
    by inspecting the actual file(s) on disk afterward (not just trusting
    the agent's own summary). Confirmed via the proxy's own log that every
    one of the 88 requests these sessions generated carried `tools=0`, with
    zero proxy-side exceptions.
  - **Only 1 of 9 (11%) succeeded on a specific, now-reproduced task
    shape**: asking the model to review or fix code containing a function
    whose name contradicts its own behavior (e.g. a `subtract(a, b)` that
    actually returns `a + b`). This reproduced regardless of how the task
    was worded, whether a test file was involved, or whether any terminal
    command was ever run — three independent variations all landed at
    essentially the same near-total failure rate. The failure mode itself
    is new and different from anything documented elsewhere in this
    project: Sydney returns a **completely empty completion** (confirmed
    directly in the proxy's own log, `reply_length=0 chars`), not a
    refusal message and not a code-interpreter workaround. **Important
    caveat found right after this test**: the same account was separately
    confirmed to hit an explicit, Sydney-labeled volume-based rate limit
    shortly afterward, and these particular batches ran last in the test
    session (highest cumulative request count) — so whether the trigger is
    really "misleadingly-named code" or actually (or also) request-volume
    throttling that coincided with when these batches happened to run was
    never isolated. Treat the 17/17-vs-1/9 numbers as real and
    reproducible, but the specific "misleading function name" explanation
    as unconfirmed — see REVERSE_ENGINEERING.md's dated "Correction"
    subsection for the full reasoning.

  **Practical takeaway**: this configuration is genuinely excellent for
  straightforward "implement this / edit this / verify it" work — the
  100%-on-17-sessions result is real and reproducible — but treat
  debugging/bug-fixing workloads specifically as an open reliability risk
  until this failure mode is better understood, and be aware that heavy
  request volume against this account can also trigger Sydney's own
  explicit rate limiting (`"We're temporarily unable to respond to this
  volume of requests"` — a real, labeled error, not a silent failure) if
  you run many sessions back-to-back in a short window. See
  REVERSE_ENGINEERING.md's "OpenHands mock function calling" and "deep,
  adversarial load-testing" sections for the exact setup, all trial data,
  and the differential tests plus the volume-confound correction.

  If you don't want to go through that persisted-config route, OpenHands'
  *native* tool-calling mode (the default, via `--override-with-envs`
  pointed at this proxy) does still work sometimes — 1 of 2 sessions
  succeeded — but is subject to this proxy's own emulation reliability
  caveats above.

- **[Goose CLI](https://github.com/aaif-goose/goose/)** — **tested against
  the real Goose CLI (the `goose` Rust binary from `download_cli.sh`, not
  the Electron Desktop app — the `goose` command can resolve to either
  depending on install order) and did not work: 0 of 4 full sessions
  succeeded** on the same task. Goose's default extension set offers the
  model **18 tools** at once (more than either OpenCode's 10 or OpenHands'
  6), rendering to a ~19.6KB prompt, and every attempt across 3 sessions
  fell back to the flat-refusal failure mode. Restricting Goose to just its
  `developer` extension (`--no-profile --with-builtin developer`, 5 tools,
  ~6.7KB prompt) changed the failure *shape* to Sydney's own code-
  interpreter self-preempting instead of refusing — consistent with the
  tool-count/prompt-size hypothesis noted in the OpenCode section above —
  but still did not produce a working tool call in that trial either.
  Configure Goose against this proxy via a custom-provider JSON (see
  REVERSE_ENGINEERING.md for the exact config and invocations used) if you
  want to experiment further, but the honest current answer is the same as
  OpenCode's: it does not work yet.

  Goose also has its own analogous mechanism to OpenHands' mock function
  calling — its **"Toolshim"** (`GOOSE_TOOLSHIM=1`) — which also sends
  `tools: []` to the provider and tries to parse tool calls straight out of
  the plain-text reply, only falling back to a separate interpreter model
  (Ollama, or Goose's own bundled local llama.cpp — neither can be pointed
  at an arbitrary OpenAI-compatible URL like this proxy) if that fails.
  **Tested with no Ollama server running at all: 0 of 3 sessions
  succeeded, but this time with a fully confirmed root cause, not just an
  observed failure.** Goose's own toolshim system-prompt text is hardcoded
  into the binary and uses the word "tool" five times — exactly the
  trigger already established to reliably derail Sydney into code-
  interpreter self-preemption — and this proxy has no way to launder that
  specific text (see REVERSE_ENGINEERING.md's "Goose's own 'Toolshim'"
  section for the full mechanism, confirmed directly against both this
  proxy's own log and Goose's own CLI log).

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
`--host`/`--port`, `--credentials-prefix`, `--log-file`/`--log-level`,
`--disable-conversation-continuity`.

See `REVERSE_ENGINEERING.md` for the full protocol reverse-engineering
writeup this implementation is based on (Chathub/SignalR wire format, the
FOCI token-family auth chain, the MSAL cache-encryption algorithm, etc).
`experiments/` holds small, ad-hoc scripts used while developing the
Sydney-native conversation continuity feature — `probe_conversation_reuse.py`
(live-tests against the real backend that a reused `ConversationId` carries
real server-side memory), `dump_frames.py` (raw SignalR frame dump, used to
find the rate-limit handling gap), and `test_continuity_offline.py` (a
network-free test of the HTTP-layer wiring itself, with `run_chat_turn`
stubbed out — see REVERSE_ENGINEERING.md for what each one found). None of
them are part of the shipped proxy or required to run it.

## Requirements

Python 3 standard library only, no third-party packages. CI smoke-tests
this script (byte-compile, plus a `--help` run that imports the module and
so exercises the pure-Python AES-256-GCM self-check) on Python 3.9 through
3.13 on Linux, and on the oldest and newest of those (3.9 and 3.13) on
macOS and Windows as well. Functional end-to-end use — which needs live
Microsoft credentials and so can't run in CI — was developed and verified
against Python 3.12.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
