#!/usr/bin/env python3
"""
m365_openai_proxy.py -- a minimal OpenAI-compatible HTTP API backed by
https://m365.cloud.microsoft's Copilot chat backend ("Sydney" / Chathub).

It exposes the same backend two ways on one host/port: the OpenAI-style HTTP
API (`/v1/chat/completions`, `/v1/models`) and, by default, an MCP (Model
Context Protocol) endpoint at `POST /mcp` over the Streamable HTTP transport,
offering `ask_copilot`, `describe_image` and `generate_image` tools (turn it
off with --disable-mcp). See the "MCP (Model Context Protocol) layer" section
below.

SELF-CONTAINED: this single file uses ONLY the Python 3 standard library --
no `pip install` of anything, ever, for any feature (including the MSAL
encrypted-cache decrypt path, which needs AES-256-GCM; this file implements
that itself in pure Python -- see the "Pure-Python AES-256-GCM" section
below). Drop this one file onto any machine with a Python 3 interpreter and
run it; nothing else needs to be installed.

Supported interpreters: Python 3.7 through 3.13 (see `MIN_PYTHON` below,
which is checked at startup). The 3.7 floor comes from
`http.server.ThreadingHTTPServer`, added in that version.

------------------------------------------------------------------------------
AUTHENTICATION MODEL -- READ THIS FIRST
------------------------------------------------------------------------------
This proxy's OpenAI-style HTTP API (`/v1/chat/completions`, `/v1/models`)
and its MCP endpoint (`/mcp`) take NO Authorization header and NO API key
from their callers, by design. All of Microsoft's authentication is handled
internally by this program.

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

If both a plaintext `refresh_token` and a usable encrypted trio exist at
once (typical once you've ever used option 2, since the rotation above then
keeps recreating `refresh_token.conf` on every run), the proxy picks
whichever one was written to more recently (by file mtime), not always
`refresh_token`. This matters when a rotated `refresh_token.conf` goes
stale or gets its underlying token superseded/expired (e.g. Entra's
AADSTS700084 for SPA-issued refresh tokens, which are capped at a fixed
24h lifetime no rotation can extend) and you recapture a fresh
`encrypted_refresh_token`/`cache_encryption_key` pair to replace it: the
newer recapture now wins automatically, instead of the stale
`refresh_token.conf` silently continuing to be used just because it still
exists.

------------------------------------------------------------------------------
KNOWN LIMITATIONS
------------------------------------------------------------------------------
- **Multi-turn memory now prefers Sydney's own native conversation state,
  with context-stuffing kept as an automatic fallback for whatever doesn't
  fit that.** A live experiment (`scripts/probe_conversation_reuse.py`,
  see REVERSE_ENGINEERING.md's "Sydney-native conversation continuity"
  section) confirmed that Sydney DOES honor a `ConversationId` reused
  across a brand-new, independent Chathub WebSocket connection as real
  server-side conversation memory -- a turn taught a secret word, then
  closed, was correctly recalled by a second, unrelated connection reusing
  the same `ConversationId` with no history resent, while a control turn
  using a fresh `ConversationId` correctly showed no memory at all. Based
  on that, `ConversationSessionStore` (see its docstring, just above
  `make_handler`) tracks, per proxy process, a best-effort mapping from "the
  exact `messages` array a client has seen so far" to the Sydney
  `ConversationId` that already holds that history server-side. When an
  incoming request's `messages` array extends a previously-seen one (the
  common case for essentially every OpenAI-style client, since they all
  resend their full growing history), this proxy sends ONLY the new
  message(s) as the next Chathub turn on that reused `ConversationId`,
  instead of re-rendering and re-sending the entire conversation every time.
  Matching is by LONGEST already-seen prefix, so a client that appends more
  than one message per round -- or re-serializes the previous assistant reply
  differently than this proxy emitted it (exactly what a tool-calling agent
  does when it reconstructs the assistant `tool_calls` message and adds a
  separate execution-result message) -- is still recognized, which the
  original strict `messages[:-1]` matcher could not do. Requests WITH `tools`
  are eligible too: the tool-calling emulation reuses Sydney's conversation
  for a single delta turn and automatically falls back to a fresh,
  schema-reinjected turn if that doesn't land (see the section comment above
  `_conversation_fingerprint`).

  This is fully additive and safe-by-construction: it is never used to
  produce output that couldn't have been produced before it existed.
  Anything that doesn't match a tracked session -- the first turn of a
  conversation, a genuinely branched/edited history, a different credential,
  or a session this process has never seen (including after a restart -- the
  store is in-memory only, never persisted) -- simply falls back to this
  proxy's
  original, always-correct behavior: render the *entire* incoming
  `messages` array into one text blob and send it as a single turn on a
  brand-new `ConversationId`, exactly as if this feature didn't exist. A
  tracked session is also strictly SINGLE-USE (popped, not just read, on a
  match) specifically so that BRANCHED history -- two different follow-up
  messages sent from the same earlier point, e.g. a client regenerating a
  response, or two requests racing -- can never both reuse the same
  `ConversationId`: only the first one to arrive gets the native-memory
  fast path, the second gets a clean cache miss and falls back to a
  brand-new conversation instead of corrupting either branch with the
  other's content (a real bug caught live by
  `tests/test_continuity.py`'s "branch" case during
  development, not just a theoretical concern). A turn that tries to
  continue a tracked session and fails is caught, the session is forgotten,
  and (for non-streaming requests only -- see `_run_plain_turn`'s docstring
  for why streaming can't safely do the same) the SAME request
  transparently retries once as a brand-new conversation rather than
  surfacing an error. Disable this entirely with
  `--disable-conversation-continuity` if you want the old
  always-context-stuff behavior. Two things this does NOT change: (1)
  Sydney's own server-side conversation size/quota limits still apply and
  still aren't surfaced by this proxy; (2) a tracked session is local to
  this one proxy PROCESS (in memory, not persisted) -- restarting the proxy
  always starts cold, with every conversation falling back to fresh
  context-stuffing until it's seen once more.
- **Image INPUT (understanding) and image OUTPUT (generation) both work --
  but generation is NOT on `/v1/chat/completions`.**
  Send an image with the OpenAI vision shape -- a `user` message whose
  `content` is a parts array carrying an `image_url` part with an inline
  `data:image/...;base64,...` URI -- and Sydney's GPT-V reads it and answers
  as ordinary text (live-verified 2026-08-02: solid red/green/blue PNGs each
  described correctly). Under the hood the image is POSTed to substrate's
  `UploadFile` endpoint and referenced from the chat turn's user message as an
  `ImageFile` `messageAnnotations` entry -- see `upload_image_to_sydney`,
  `_extract_message_images`, and REVERSE_ENGINEERING.md's "Vision input".
  Only inline `data:` URIs are accepted (a remote `http(s)://` image URL is
  skipped, to avoid server-side request forgery), and `tools` + images in one
  request is not supported.
  Image GENERATION works too, but NOT on `/v1/chat/completions` -- it has its
  own endpoint, `POST /v1/images/generations` (OpenAI's own image API, whose
  response format is base64 image data), and the MCP `generate_image` tool
  (which returns a real MCP image content block). A chat-completions response
  can only carry text, so offering it there would mean either a URL the caller
  cannot authenticate to or megabytes of base64 stuffed into `content`; asking
  for an image on the chat endpoint therefore still fails, now with a message
  pointing at the two places that do work. Under the hood the file is fetched
  with a SECOND access token, for the `designerappservice` resource, minted
  from the very same FOCI refresh token (see `fetch_generated_image`,
  `TokenCache.get(scope=...)`, and REVERSE_ENGINEERING.md's "Fetching a
  generated image").
  Two honest caveats: generation is reached by ASKING for it in a chat turn, so
  Sydney can decline or answer in prose instead (surfaced as HTTP 502
  `upstream_no_image`, carrying Sydney's own wording); and there is a DAILY
  per-account image cap, observed live, which Sydney reports only as prose
  ("Sorry, I can't generate any more images today") and which its own
  `throttling.metering` block does NOT reflect -- `ImageGeneration` still read
  `100` with the cap already hit.
  On the chat endpoint an image-only turn is surfaced as HTTP 502
  `unsupported_upstream_content` (see `UnsupportedContentError`) -- which two
  earlier revisions got wrong in instructive ways, both now fixed: it was
  first misreported as HTTP 429 "you are being throttled, wait and retry"
  (wrong in every part, and it sent clients into a retry loop that could never
  succeed), and the explanation then blamed a browser Office cookie session
  the proxy lacks (disproven live -- see above).
- `usage` (prompt/completion/total tokens) in every chat-completions response
  is hardcoded to zero -- this proxy does no token counting at all. (On
  `/v1/images/generations`, where OpenAI's own schema makes `usage` optional,
  it is OMITTED instead of faked.)
- Sydney's `throttling` block also carries a `metering` sub-object listing
  15 named, metered CAPABILITIES (`CodeInterpreter`, `TenantDataAccess`,
  `PersonalDataAccess`, `ImageGeneration`, ...) with a `remainingAllowance`
  each. It is logged (see `_metering_summary`) and nothing else: the
  allowance numbers were measured NOT to mean what they appear to
  (`CodeInterpreter` read 0 on a turn whose code interpreter demonstrably
  ran, and no value moved across 8 consecutive turns), so this proxy never
  branches on them. Note this block rides on the type-2 StreamItem frame,
  not the type-1 `update` frames.
- One Chathub WebSocket is opened, used for exactly one turn, and closed
  per HTTP request -- no connection pooling or reuse across requests.
- Sydney's own throttling info (the Chathub protocol's `throttling` field)
  is now LOGGED once per distinct state per turn (`Sydney throttling/quota
  state: used=... max=... headroom=...`, see `_throttling_summary`), but is
  not surfaced through the HTTP API and never changes this proxy's
  behavior: nothing backs off, refuses, or warns a client because of it.
  Measured live (2026-08-02): it reports a PER-CONVERSATION user-message
  count against a cap of **600**, plus a third counter,
  `numLongDocSummaryUserMessagesInConversation`. It resets to 1 on every new
  `ConversationId`.

  Read that cap carefully before relying on it: at 600 messages per
  conversation it is effectively unreachable in normal use, and it is NOT
  the limit that actually bites. The throttling operators really hit -- the
  silent empty reply after a burst of requests (see
  `_looks_like_throttled_empty_reply`) -- is account-level, fires across
  many separate short conversations, and is not exposed on the wire at all.
  This field cannot predict it. The value of logging it is therefore
  diagnostic and partly negative: a log can now show that a failed run was
  nowhere near any per-conversation ceiling, which rules that explanation
  out instead of leaving it open.
- **Tool/function calling is EMULATED and probabilistic, not a guarantee.**
  Sydney exposes no native OpenAI-style `tools`/`tool_calls` mechanism *to a
  client* that this proxy can use (its real one, Local MCP, is a separate,
  unimplemented, much harder project -- see below).

  Read that precisely, because a blunter earlier version of this sentence
  ("Sydney has no native ... mechanism") was disproven on the wire on
  2026-08-02. Sydney very much HAS one and uses it internally for its own
  built-in tools: an image-generation turn was captured emitting
  `{"function": {"name": "image_gen", "arguments": "{...}"},
  "id": "call_zvq9VI82lh3kvZdNlbDl5c", "type": "function"}` -- exactly
  OpenAI's `tool_calls` entry shape, `call_`-prefixed id and all (see
  `_native_invocation_names`, which now logs these). What remains unknown,
  and is the interesting open question, is whether a CLIENT-declared tool can
  be registered into that namespace; nothing in this proxy assumes it can.
  Until that is answered the emulation below stays exactly as it is. See
  REVERSE_ENGINEERING.md's "Sydney's own capability surface, probed".

  This proxy instead teaches the model ONE OF
  TWO independent textual conventions per attempt, cycling through both
  across retries (`_TOOL_CALL_MODES`, `_run_tool_call_turn()`), and parses
  whichever one the model actually used back into a real OpenAI `tool_calls`
  response:
    - **"code" mode** (`_render_tools_block_code_mode()` /
      `_extract_code_mode_calls()`): tells the model its Python execution
      environment already has one extra function loaded,
      `invoke_capability(name, arguments)`, and asks it to call it from
      ordinary code exactly the way it already strongly prefers to solve
      things -- Sydney's own code interpreter is left ENABLED for this mode
      (not suppressed) since the whole point is to lean into that habit
      rather than fight it. The reply is parsed via a real Python AST walk
      (`ast.parse()` + `ast.literal_eval()`), not a regex, so the model can
      write whatever ordinary code it wants around the call(s).
    - **"action_request" mode** (`_render_tools_block()` /
      `_extract_tool_calls()`): the original convention -- a JSON object
      wrapped in `<action_request>...</action_request>` tags, with Sydney's
      own code-interpreter-ish `OPTIONS_SETS` entries and `BingWebSearch`
      plugin explicitly suppressed so there's nothing else for it to reach
      for instead.
  Both remain entirely prompt-level steering with NO formal contract -- live
  testing during development found:
    - Neither convention dominates the other in every situation: a small
      live A/B trial found "code" mode winning heavily on a simple
      single-capability request (6/6 vs "action_request"'s 0/6 in the same
      session) but "action_request" doing BETTER at realistic
      coding-agent scale (4/5 vs "code" mode's 2/5) -- see
      REVERSE_ENGINEERING.md's "code-mode tool-calling emulation" section
      for the exact trial data. This is why `_run_tool_call_turn()` cycles
      through BOTH conventions across its retry attempts rather than
      retrying one fixed convention -- they appear to fail in different,
      largely uncorrelated ways (Sydney's own code interpreter
      self-preempting vs. a flat refusal), so trying the other convention
      on the next attempt is a meaningfully different roll of the dice, not
      just "try again and hope."
    - The literal word "tool"/"tools" ANYWHERE in the rendered prompt --
      including in the CLIENT's own system prompt, which this proxy cannot
      control -- measurably makes both conventions worse. This proxy
      actively launders that word out of the entire prompt when `tools` is
      present (`_neutralize_tool_word()`), which helps but does not fix it
      outright.
    - Continuing a conversation *after* a tool result required its own fix,
      applying to both conventions: naively rendering "the assistant
      already called X" as a fabricated prior turn reliably triggered a
      flat refusal on the next reply (a classic prompt-injection SHAPE,
      even with entirely mundane content). Folding the tool result into the
      next turn as ordinary user-supplied context instead of a fabricated
      assistant/tool history entry fixed this specific failure mode --
      phrased to match whichever convention that turn is using (see
      `_fold_pending()`).
  See REVERSE_ENGINEERING.md's "Tool-calling emulation" and "code-mode
  tool-calling emulation" sections for the full trial-by-trial account,
  including real end-to-end testing against the actual OpenHands, OpenCode,
  and Goose CLIs (not just synthetic curl requests). That picture HAS since
  been re-validated live against the current two-convention design -- see
  REVERSE_ENGINEERING.md's "Harmonized-build agent re-validation" section
  (and the README's "Compatibility at a glance" table): Aider and OpenHands
  (mock-function-calling) work; OpenHands native tool-calling succeeded too,
  with `code` and `action_request` modes each landing calls the other
  missed; OpenCode and Goose still do not work, with the failure now
  confirmed to survive BOTH conventions (a genuine model-behavior mismatch
  on large injected tool schemas, not a single-convention artifact).

  **IMPORTANT if your client is OpenHands: prefer OpenHands' own
  client-side "mock function calling" mode over this proxy's emulation
  above -- but it is NOT unconditionally reliable, see below.**
  OpenHands' SDK has a `native_tool_calling=False` flag on its `LLM`
  config that converts tools to text and parses the reply entirely on
  OpenHands' own side, sending this proxy NO `tools` field at all (so this
  proxy's emulation above never runs). Initial test: 3 of 3 full sessions
  succeeded. A much larger follow-up load test (26 sessions, 7 batches --
  single-edit, multi-step-with-terminal-verification, multi-file,
  concurrent sessions, and an iterative debug loop) found **17 of 17
  (100%) success on ordinary implement/edit/verify task shapes including
  concurrency**, dramatically more reliable than either this proxy's
  emulation or OpenHands' native tool-calling mode (1 of 2 sessions) --
  but only **1 of 9 (11%) success reasoning about/fixing code containing a
  function whose name contradicts its own behavior** (e.g. a `subtract`
  that adds), which reproducibly (regardless of wording, tests, or tool
  use) makes Sydney return a genuinely empty completion -- a third failure
  mode, distinct from refusal-text and code-interpreter self-preemption.
  This flag isn't exposed through the OpenHands CLI's normal settings/
  env-var surface in the tested version -- it requires directly seeding a
  persisted `agent_settings.json` file -- see REVERSE_ENGINEERING.md's
  "OpenHands' own client-side 'mock function calling'" and "deep,
  adversarial load-testing" sections for the exact setup and full trial
  data. Does not transfer to OpenCode (no equivalent client-side fallback
  exists) or to Goose (it DOES have an analogous mechanism, "Toolshim",
  but it doesn't help here -- see next).

  Goose's own "Toolshim" (`GOOSE_TOOLSHIM=1`) has the same architecture as
  OpenHands' mock function calling -- sends `tools: []` to this proxy and
  tries to parse tool calls straight out of the plain-text reply first,
  only falling back to a separate interpreter model (Ollama, or Goose's
  own bundled local llama.cpp -- neither reachable at an arbitrary OpenAI-
  compatible URL like this proxy) if that fails -- but it did NOT help:
  tested 0 of 3 sessions succeeded, with Ollama deliberately not running at
  all. Root cause, confirmed via Goose's own CLI log (not just inferred):
  Goose's toolshim system-prompt text is hardcoded into the binary and uses
  the word "tool" five times, the same trigger already established to
  derail Sydney into code-interpreter self-preemption -- this proxy has no
  way to launder text a CLIENT injects into its own system prompt when
  `tools` is empty, since `_neutralize_tool_word()` is currently gated on
  `tools` being present. See REVERSE_ENGINEERING.md's "Goose's own
  'Toolshim'" section for the full mechanism and log evidence.

  Sydney's own REAL tool-invocation mechanism (Local MCP, over the same
  Chathub connection -- `mcp_discover`/`mcp_describe`/`invoke_local_plugin`
  SignalR targets, reverse-engineered from the officeweb client's own
  source) is documented in REVERSE_ENGINEERING.md's "Local MCP tool-calling
  bridge" section but remains unimplemented -- bridging it properly runs
  into a genuine architecture mismatch (Sydney's invocation is synchronous,
  mid-turn, and presumably timeout-bound) that is a substantial separate
  project, not a quick patch, and would sidestep everything above.

- **A completely empty reply from Sydney is treated as an error, not a
  silent success -- this usually means Microsoft-side request throttling,
  not that the model had nothing to say.** Discovered during this proxy's
  own tool-calling A/B testing: after roughly 40-50 requests in a few
  minutes, Sydney started returning Chathub turns that complete normally
  (no error, no `AuthError`) but with zero characters of content -- for a
  PLAIN tools-free chat turn just as much as a tool-calling one, so this is
  a general Sydney-side behavior, not specific to anything this proxy does
  with `tools`. It did not clear within 45 seconds, only after several
  minutes. `_looks_like_throttled_empty_reply()` detects this and both
  `_handle_full`/`_handle_streaming` now surface it as an explicit error
  (a `429`/`upstream_throttled` JSON error, or an SSE error event for
  streaming) instead of a silent `200`-with-empty-content response that
  would otherwise be indistinguishable from "the model genuinely answered
  with nothing." If you hit this, back off for a few minutes before
  retrying -- see REVERSE_ENGINEERING.md's "Sydney-side request throttling"
  section for the full timeline this was based on.

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

    python3 m365_openai_proxy.py --port 8001

    curl http://127.0.0.1:8001/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -d '{"model": "m365-copilot", "messages": [{"role": "user", "content": "hello"}]}'

    # note: no Authorization header -- none is expected or checked.
"""

import argparse
import ast
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

# The oldest interpreter this proxy supports; see README "Requirements". Kept
# as the single source of truth for the floor -- CI's `vermin --target` gate
# and the compatibility matrix are pinned to the same number.
#
# Checked here, immediately after the imports and before anything else runs,
# so a too-old interpreter gets one plain sentence instead of an obscure
# failure much later. That is not hypothetical: a Python-3.9-only kwarg on a
# `hashlib.sha1()` call inside the WebSocket handshake once let startup,
# authentication, `--help` and the entire test suite all succeed on 3.7, then
# failed *every chat turn* with `TypeError: openssl_sha1() takes no keyword
# arguments`. This is the console-output exception noted in the logging
# rules: the proxy genuinely cannot continue, and cannot even log.
#
# Note this can only help interpreters new enough to *parse* this file --
# f-strings mean Python 3.6+. Anything older fails with a SyntaxError before
# a single line of it executes, which no in-file guard can intercept.
MIN_PYTHON = (3, 7)
if sys.version_info < MIN_PYTHON:  # pragma: no cover - depends on interpreter
    sys.exit(
        "m365_openai_proxy needs Python {}.{} or newer, but this is Python {}.\n"
        "Re-run it with a newer interpreter, for example:\n"
        "    python3.11 {}".format(
            MIN_PYTHON[0],
            MIN_PYTHON[1],
            platform.python_version(),
            sys.argv[0] if sys.argv and sys.argv[0] else "m365_openai_proxy.py",
        )
    )

# Single source of truth for the version string reported in the startup
# banner (see _log_startup_banner) and in the HTTP Server header.
PROXY_VERSION = "0.14.0"

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

#: `messageType` values on a bot `messages[]` entry that mean "this entry IS
#: (part of) the answer the caller asked for", and whose `text` therefore
#: belongs in the reply -- see `stream_chat_reply`.
#:
#: The prose answer entry carries NO `messageType` key at all (observed live;
#: it is identified instead by `contentOrigin:"DeepLeo"` + `adaptiveCards`),
#: hence the `None`. `"Chat"` is accepted defensively (it is the name the
#: protocol uses for an ordinary chat message -- it is what the client itself
#: stamps on the outgoing user message -- so an answer arriving explicitly
#: typed that way must not be dropped). `"Disengaged"` carries Sydney's own
#: canned "I'd rather not continue this conversation" refusal text, which is
#: meant to be shown to the user, so it counts as answer content too.
#:
#: `"GeneratedCode"` is here for a LOAD-BEARING reason, verified live with
#: `scripts/probe_generated_code_messages.py`: in "code" tool-calling mode the
#: fenced ```python block containing the `invoke_capability(...)` call -- the
#: entire thing `_extract_code_mode_calls` parses a tool call out of -- arrives
#: as a `messageType:"GeneratedCode"` entry and NOT in the prose answer entry
#: (which, in that same turn, holds only Sydney's prose). Treating it as chrome
#: silently breaks all code-mode tool calling. See REVERSE_ENGINEERING.md's
#: "Telling the answer apart from Sydney's own UI chrome".
ANSWER_MESSAGE_TYPES = frozenset({None, "Chat", "Disengaged", "GeneratedCode"})

#: Bot `messages[]` entries with one of these `messageType`s are Sydney's own
#: transient UI chrome -- progress/status placeholders ("Gathering details...",
#: "Working on it...", "Looking into it...", and "Coding and executing", which
#: is a `Progress` entry sitting right next to the `GeneratedCode` entry that
#: is genuine content), internal search bookkeeping, follow-up suggestions,
#: citation-list end markers, and so on. They stream on the SAME
#: `target:"update"` channel as the answer and must never reach the caller:
#: yielding them prepends status noise to the assistant's content (and
#: previously did -- see `stream_chat_reply`).
#:
#: This set exists only to keep the log quiet about the ones we EXPECT to see:
#: anything that is neither here nor in `ANSWER_MESSAGE_TYPES` is skipped too,
#: but logged as a warning, since a genuine answer wrongly skipped would
#: otherwise look like an unexplained empty reply.
TRANSIENT_MESSAGE_TYPES = frozenset(
    {
        "Progress",
        "InternalLoaderMessage",
        "InternalSearchQuery",
        "InternalSearchResult",
        "ReferencesListComplete",
        "Suggestion",
        "RenderCardRequest",
        "AdsQuery",
        "SemanticSerp",
        "SearchQuery",
        "MemoryUpdate",
        "EndOfRequest",
        "DeveloperLogs",
    }
)

SIGNALR_RS = "\x1e"  # SignalR JSON Hub Protocol record separator

# Sydney's own built-in plugins/capabilities (BingWebSearch via the `plugins`
# field; the code interpreter and image-gen capabilities via `cwc_code_
# interpreter*`/`flux*`/`gptv*` OPTIONS_SETS entries) reliably preempt this
# proxy's "action_request" tool-calling convention -- confirmed live during
# development: a weather question got a flat refusal, and a basic-arithmetic
# "use the calculator tool" request got silently answered via Sydney's own
# code interpreter instead of our requested `<action_request>` convention
# (see REVERSE_ENGINEERING.md's "Tool-calling emulation" section). When
# using the "action_request" convention, this proxy asks Sydney with these
# capabilities stripped, so the model has nothing to reach for except our
# injected convention. The other convention this proxy can use, "code" mode
# (see _TOOL_CALL_MODES), does the opposite on purpose -- it LEANS INTO
# these same capabilities rather than fighting them, so it does NOT use this
# list; see _render_tools_block_code_mode's docstring.
TOOL_MODE_OPTIONS_SETS = [
    o
    for o in OPTIONS_SETS
    if not any(kw in o for kw in ("code_interpreter", "flux", "gptv"))
]

# ------------------------------------------------------------------------------
# Vision input (image understanding / GPT-V)
#
# Sydney accepts an image the user asks about ("what is on this image?") in two
# steps, reverse-engineered from a real browser session (see
# REVERSE_ENGINEERING.md's "Vision input" section):
#
#   1. The raw image is POSTed, base64-encoded, to substrate's `UploadFile`
#      endpoint (same host and same bearer token this proxy already mints for
#      the Chathub WebSocket). The response returns a `docId`.
#   2. The ordinary `chat` turn then carries that `docId` in
#      `message.messageAnnotations` as an `ImageFile` annotation; the gptv
#      `optionsSets` that make Sydney actually look at it are already in
#      OPTIONS_SETS (`cwcfluxgptv`, `gptvnorm2048`,
#      `flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch`), so nothing else
#      about the chat invocation changes.
#
# The reply is ordinary streamed text, so it flows back through
# `stream_chat_reply` unchanged.
UPLOAD_FILE_URL = "https://substrate.office.com/m365Copilot/UploadFile"

# ------------------------------------------------------------------------------
# Image generation (image OUT)
#
# Sydney generates images fine -- it invokes its own `image_gen` tool and
# streams `contentGenerationProgressList` entries that end with a real
# `ImageReferenceUrls` link -- but it emits NO answer text for such a turn, so
# there is nothing for a chat-completions response to carry. That is why
# `/v1/chat/completions` still reports an image-only turn as an error (see
# `UnsupportedContentError`), and why image generation lives on its own
# endpoint instead: OpenAI's `/v1/images/generations`, whose response format is
# base64 image bytes rather than text.
#
# Fetching the generated file needs a SECOND access token, for a different
# resource than Chathub -- minted from the very same FOCI refresh token, so no
# extra credential is required from the operator. See REVERSE_ENGINEERING.md's
# "Fetching a generated image" for the full derivation and its live controls.
DESIGNER_SCOPE = (
    "https://designerappservice.officeapps.live.com/.default "
    "openid profile offline_access"
)

#: Host serving generated images. The fetch URL arrives verbatim from Sydney;
#: this is asserted against it before the proxy will fetch, so a
#: server-supplied URL can never redirect this proxy's authenticated request at
#: an arbitrary host.
DESIGNER_HOST = "designerapp.officeapps.live.com"

#: The `optionsSets` the browser sends as multipart form parts on the
#: `UploadFile` POST itself (distinct from the chat turn's OPTIONS_SETS).
UPLOAD_IMAGE_OPTIONS_SETS = [
    "cwcgptvsan",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
]

#: Defensive caps on vision input, applied before anything is uploaded. Sydney
#: enforces its own limits server-side; these just stop a malformed or hostile
#: request from making this proxy buffer or forward something absurd.
MAX_INPUT_IMAGES = 8
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB per image, pre-base64

#: Recognized raster image MIME types -> the `fileType` Sydney's annotation
#: metadata wants (its extension, no dot). Anything else is rejected rather
#: than guessed at.
_IMAGE_MIME_TO_FILETYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


# ==============================================================================
# Errors
# ==============================================================================


class AuthError(Exception):
    """Refresh-token/access-token acquisition failed."""


class CredentialError(Exception):
    """The credentials file is missing, malformed, or couldn't be decrypted."""


class ProtocolError(Exception):
    """The Chathub WebSocket did something we didn't expect."""


class ThrottledError(ProtocolError):
    """Sydney refused the turn with its own rate-limit message (e.g. "We're
    temporarily unable to respond to this volume of requests. Please try
    again later.") -- see `stream_chat_reply`'s handling of `StreamItem`
    frames for where this is detected."""


class UnsupportedContentError(ProtocolError):
    """Sydney answered with generated NON-TEXT content (an image, so far) and
    no answer text at all, so there is nothing this text-only API can return.

    This exists to keep that case from being misreported as throttling.
    Sydney's image generation streams `Progress` entries carrying a
    `contentGenerationProgressList` (and, at the end, a real
    `ImageReferenceUrls` link) but never emits an answer-text entry -- so
    before this error existed, an image request produced an empty reply,
    which `_looks_like_throttled_empty_reply` then reported as HTTP 429
    "Microsoft is temporarily throttling this account ... wait a bit and try
    again". That advice was wrong in every part: nothing was throttled, and
    waiting changes nothing -- the same request fails identically forever.

    The image itself IS now retrievable -- just not through this API. Use
    `POST /v1/images/generations` or the MCP `generate_image` tool, both built
    on `generate_image()`/`fetch_generated_image()`; this error's message points
    the caller at them. What remains true is that a chat-completions response
    has nowhere to put the bytes, which is why this endpoint still refuses
    rather than inlining megabytes of base64 into `content`.

    (An earlier revision of this docstring claimed the file was out of reach
    entirely -- that fetching it needed the
    browser's Office (OHP) cookie session -- inferred from the
    `ImageReferenceUrls` link returning HTTP 401 both anonymously and with
    this proxy's own Sydney bearer token. The 401s are real; the inference was
    wrong, and was disproven live: the link wants a DIFFERENT bearer token,
    minted from the same FOCI refresh token for the
    `designerappservice.officeapps.live.com` resource, plus the URL's
    `fileToken` query param moved into a `filetoken` header. That fetch
    returns a valid 2.7 MB PNG. See REVERSE_ENGINEERING.md's "Fetching a
    generated image" for the full recipe and its controls.)"""


class WSError(Exception):
    """Low-level WebSocket handshake/framing failure."""


# ==============================================================================
# Minimal stdlib-only WebSocket client (RFC 6455 subset: client -> TLS only,
# no permessage-deflate -- confirmed from captures that the real server does
# not require compression, so we simply never offer the extension).
# ==============================================================================

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def websocket_accept_value(key):
    """RFC 6455 `Sec-WebSocket-Accept` for a given `Sec-WebSocket-Key`.

    Kept as a module-level function, rather than inlined into the handshake,
    so the offline suite can exercise it directly against RFC 6455's published
    example vector on every interpreter the proxy supports. Inlined, it was
    reachable only by opening a real TLS connection, so nothing in CI ran it
    -- which is exactly how a Python-3.9-only spelling of the `hashlib.sha1()`
    call below once shipped and broke every chat turn on 3.7/3.8 while
    `py_compile`, `--help` and the whole test suite stayed green.

    SHA-1 is mandated by RFC 6455 and is not a security control here: the
    result is a fixed, public transformation of a nonce we just sent in
    cleartext, used only to prove the peer really spoke WebSocket rather than
    being some other server (or a cache) that happened to answer. bandit's
    preferred way to say that -- passing `usedforsecurity=False` -- exists
    only on Python 3.9+ and raises TypeError below it, so this says it in a
    `# nosec` comment instead and keeps the call itself portable.
    """
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()  # nosec B324
    return base64.b64encode(digest).decode()


class WebSocketClient:
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
        if resp_headers.get("sec-websocket-accept") != websocket_accept_value(key):
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
    return value.startswith(("%7B", "%7b"))


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
        encrypted_raw = _load_credential_file(self.paths["encrypted_refresh_token"])
        key_raw = _load_credential_file(self.paths["cache_encryption_key"])
        have_encrypted = bool(encrypted_raw and key_raw)

        # A plaintext refresh_token and a usable encrypted trio can both be
        # present at once: rotate() (below) overwrites refresh_token.conf
        # after every successful exchange, so once the encrypted path has
        # ever been used a refresh_token.conf sticks around from then on,
        # unconditionally. If the operator later re-captures a fresh
        # encrypted_refresh_token/cache_encryption_key -- typically because
        # the previously-rotated plaintext refresh_token expired or was
        # superseded (observed live: Entra's AADSTS700084, "The refresh
        # token was issued to a single page app ... fixed, limited lifetime
        # of 1.00:00:00") -- that recapture must not be silently shadowed by
        # the now-stale refresh_token.conf just because it still exists.
        # Break the tie by recency: whichever credential's file(s) were
        # last written to is the operator's most recent action and wins.
        prefer_encrypted = False
        if rt and have_encrypted:
            rt_mtime = os.path.getmtime(self.paths["refresh_token"])
            encrypted_mtime = max(
                os.path.getmtime(self.paths["encrypted_refresh_token"]),
                os.path.getmtime(self.paths["cache_encryption_key"]),
            )
            prefer_encrypted = encrypted_mtime > rt_mtime
            if prefer_encrypted:
                logging.info(
                    "%s is older than %s/%s; the encrypted credential was "
                    "updated more recently and takes precedence over the "
                    "now-stale plaintext refresh_token",
                    self.paths["refresh_token"],
                    self.paths["encrypted_refresh_token"],
                    self.paths["cache_encryption_key"],
                )

        if rt and not prefer_encrypted:
            logging.info(
                "using plaintext refresh_token from %s (length=%d chars)",
                self.paths["refresh_token"],
                len(rt),
            )
            return rt

        if have_encrypted:
            logging.info(
                "%sattempting MSAL localStorage cache decrypt "
                "(encrypted_refresh_token + cache_encryption_key)",
                ""
                if prefer_encrypted
                else f"no usable value in {self.paths['refresh_token']}; ",
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


def exchange_refresh_token(refresh_token, oid=None, tid=None, scope=None):
    """POST the refresh_token grant to Entra ID, mimicking the exact shape
    observed from a real browser --
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
    documented-valid way to redeem a multi-tenant refresh token.

    `scope` defaults to `SYDNEY_SCOPE` (the Chathub resource). The ONLY other
    value this proxy uses is `DESIGNER_SCOPE`, to fetch a generated image --
    that is the same FOCI redemption against a different resource, and every
    other parameter is byte-identical (see REVERSE_ENGINEERING.md's "Fetching
    a generated image"). Callers must go through `TokenCache.get(scope=...)`
    rather than calling this directly: each redemption ROTATES the shared
    refresh token, so the exchange has to be serialized across ALL scopes."""
    scope = scope or SYDNEY_SCOPE
    token_url = TOKEN_URL_TEMPLATE.format(tid=tid) if tid else TOKEN_URL_COMMON
    logging.debug(
        "exchanging refresh token for an access token (scope=%r) via %s",
        scope,
        token_url,
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
        "scope": scope,
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
    __slots__ = ("access_token", "expires_at", "oid", "tid")

    def __init__(self, access_token, oid, tid, expires_at):
        self.access_token = access_token
        self.oid = oid
        self.tid = tid
        self.expires_at = expires_at


class TokenCache:
    """Caches the access tokens derived from the CredentialStore's one
    configured refresh token, re-exchanging only when a cached access token is
    near expiry, and persisting each rotation back to the store.

    Cached PER SCOPE (`get(scope=...)`), because this proxy needs two different
    audiences from the same credential: `SYDNEY_SCOPE` for Chathub (and the
    substrate `UploadFile` endpoint, same audience) and `DESIGNER_SCOPE` to
    fetch a generated image. Both redemptions go through the SAME
    `_refresh_lock`, which is not an optimization but a correctness
    requirement: Entra invalidates the previous refresh token on every
    redemption and each redemption returns a new one, so two scopes redeeming
    concurrently would have the second rejected as `invalid_grant` AND race
    each other's `store.rotate()` writes -- corrupting the credential file
    badly enough to need a fresh browser capture. A per-scope lock would
    reintroduce exactly the bug the single lock was added to fix."""

    def __init__(self, credential_store):
        self.store = credential_store
        #: Guards reads/writes of self._auth (held only briefly).
        self._lock = threading.Lock()
        #: Serializes the token exchange itself so at most one thread ever
        #: redeems the refresh token at a time, ACROSS ALL SCOPES -- see the
        #: class docstring and get() for why.
        self._refresh_lock = threading.Lock()
        #: scope string -> SydneyAuth
        self._auth = {}

    def _cached_auth(self, scope):
        """Returns the cached SydneyAuth for `scope` if it exists and isn't
        within 60s of expiry, else None."""
        with self._lock:
            auth = self._auth.get(scope)
        if auth and auth.expires_at - 60 > time.time():
            logging.debug(
                "reusing cached access token (oid=%s, scope=%r, expires in %.0fs)",
                auth.oid,
                scope,
                auth.expires_at - time.time(),
            )
            return auth
        return None

    def get(self, scope=None):
        scope = scope or SYDNEY_SCOPE
        cached = self._cached_auth(scope)
        if cached is not None:
            return cached

        # Serialize the actual refresh: Entra ID invalidates the previous
        # refresh token on every redemption, so two threads redeeming the same
        # token concurrently (the normal shape when several requests arrive
        # while the token is cold or expired -- this is a ThreadingHTTPServer)
        # would have the second redemption rejected as invalid_grant, and
        # their two store.rotate() writes would race. Only one thread performs
        # the exchange; any others block here and then find the freshly-cached
        # token on the re-check below instead of redeeming a second time.
        # NOTE this lock is deliberately NOT per-scope: see class docstring.
        with self._refresh_lock:
            cached = self._cached_auth(scope)
            if cached is not None:
                logging.debug(
                    "another thread refreshed the access token (scope=%r) while "
                    "we waited",
                    scope,
                )
                return cached

            return self._exchange_locked(scope)

    def _exchange_locked(self, scope):
        """Performs the refresh-token -> access-token exchange for `scope` and
        caches the result. MUST be called with self._refresh_lock held (see
        get())."""
        logging.info(
            "cached access token (scope=%r) missing or near expiry; exchanging "
            "refresh token",
            scope,
        )
        refresh_token = self.store.current()
        body = exchange_refresh_token(
            refresh_token,
            oid=self.store.oid_hint,
            tid=self.store.tid_hint,
            scope=scope,
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

        if scope == SYDNEY_SCOPE:
            auth = self._sydney_auth_from_claims(access_token, body)
        else:
            # A non-Chathub resource's token is not necessarily a readable JWT:
            # the designerappservice one is a 5-part JWE (encrypted), so
            # `jwt_claims` cannot see an `oid`/`tid`/`exp` in it at all
            # (live-verified -- see REVERSE_ENGINEERING.md's "Fetching a
            # generated image"). Take the lifetime from the response's own
            # `expires_in` instead, and the identity from the credential
            # store's hints -- nothing that uses these tokens needs either
            # claim, they are carried only for logging symmetry.
            expires_in = body.get("expires_in")
            auth = SydneyAuth(
                access_token=access_token,
                oid=self.store.oid_hint,
                tid=self.store.tid_hint,
                expires_at=time.time()
                + (int(expires_in) if str(expires_in).isdigit() else 300),
            )

        logging.info(
            "access token acquired: scope=%r oid=%s tid=%s expires_in=%ss opaque=%s",
            scope,
            auth.oid,
            auth.tid,
            body.get("expires_in", "?"),
            access_token.count(".") != 2,
        )
        with self._lock:
            self._auth[scope] = auth
        return auth

    @staticmethod
    def _sydney_auth_from_claims(access_token, body):
        """Builds the Chathub `SydneyAuth` from the token's own JWT claims.
        `oid`/`tid` are REQUIRED here (they go into the Chathub WebSocket URL
        path), unlike for the other scopes above."""
        claims = jwt_claims(access_token)
        auth = SydneyAuth(
            access_token=access_token,
            oid=claims.get("oid"),
            tid=claims.get("tid"),
            expires_at=claims.get("exp", time.time() + 300),
        )
        if not auth.oid or not auth.tid:
            # Log only the claim NAMES present, not their values (which can
            # include the signed-in user's email/UPN).
            logging.error(
                "access_token was missing oid/tid claims (claims present=%s)",
                list(claims.keys()),
            )
            raise AuthError(
                f"access_token was missing oid/tid claims (claims present={list(claims.keys())})"
            )
        return auth


# ==============================================================================
# Chathub: open the WebSocket, send one `chat` invocation, stream the reply
# ==============================================================================


def open_chathub(auth, conversation_id=None):
    """Opens one Chathub WebSocket. `conversation_id`, if given, is used
    verbatim as the Chathub `ConversationId` -- passing the SAME value
    across separate calls (separate WebSocket connections, separate HTTP
    requests) is how a Sydney-native conversation continuation reuses
    Sydney's own server-side conversation memory instead of starting a
    fresh conversation each time (confirmed live: see
    REVERSE_ENGINEERING.md's "Sydney-native conversation continuity"
    section -- a second, independent WebSocket connection reusing a prior
    call's `ConversationId` got a reply demonstrating knowledge of that
    prior call's turn, with no history resent). If omitted, a fresh random
    one is minted, exactly as this function always did before that
    capability existed."""
    session_id = str(uuid.uuid4())
    session_id_nodash = session_id.replace("-", "")
    conversation_id = conversation_id or str(uuid.uuid4())

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


def _multipart_body(fields):
    """Encodes `fields` (a list of (name, value) pairs, value being either a
    str for a plain form field or a (filename, bytes, content_type) tuple for
    a file part) as a `multipart/form-data` body. Returns `(content_type,
    body_bytes)`. Pure stdlib -- there is no `multipart` encoder in the
    standard library, so this hand-rolls the wire format the same way the rest
    of this file hand-rolls WebSocket/SignalR framing."""
    boundary = "----m365proxyformboundary" + uuid.uuid4().hex
    crlf = b"\r\n"
    out = []
    for name, value in fields:
        out.append(b"--" + boundary.encode())
        if isinstance(value, tuple):
            filename, data, content_type = value
            disp = f'form-data; name="{name}"; filename="{filename}"'
            out.append(b"Content-Disposition: " + disp.encode())
            out.append(b"Content-Type: " + content_type.encode())
            out.append(b"")
            out.append(data if isinstance(data, bytes) else str(data).encode())
        else:
            out.append((f'Content-Disposition: form-data; name="{name}"').encode())
            out.append(b"")
            out.append(value if isinstance(value, bytes) else str(value).encode())
    out.append(b"--" + boundary.encode() + b"--")
    out.append(b"")
    body = crlf.join(out)
    return "multipart/form-data; boundary=" + boundary, body


def upload_image_to_sydney(auth, conversation_id, image):
    """Uploads one image to substrate's `UploadFile` endpoint and returns the
    `ImageFile` message annotation that a `chat` turn attaches to reference it
    (see the "Vision input" constants block above).

    `image` is a dict with `data` (raw bytes), `filename`, and `file_type`
    (the extension with no dot, e.g. "png"). The bearer token is the same
    Sydney/Chathub access token this proxy already mints -- `UploadFile` lives
    on the same `substrate.office.com` host, so no separate audience is
    needed. Raises `ProtocolError` on any non-success response."""
    # `FileBase64` is a plain multipart FORM FIELD whose value is the entire
    # `data:<mime>;base64,<...>` data URI STRING -- not raw bytes, not bare
    # base64 (confirmed byte-for-byte from a live browser capture). Sending
    # anything else makes the server's image sanitizer reject the POST with
    # `{"fileSanitizer":"None","result":{"value":"InvalidRequest"}}`. The
    # OpenAI caller already hands us exactly this data URI, so it's forwarded
    # verbatim.
    fields = [("scenario", "UploadImage"), ("conversationId", conversation_id)]
    fields.append(("FileBase64", image["data_uri"]))
    for opt in UPLOAD_IMAGE_OPTIONS_SETS:
        fields.append(("optionsSets", opt))
    content_type, body = _multipart_body(fields)

    logging.info(
        "uploading input image to Sydney: filename=%s file_type=%s bytes=%d "
        "conversation_id=%s",
        image["filename"],
        image["file_type"],
        len(image["data"]),
        conversation_id,
    )
    req = urllib.request.Request(
        UPLOAD_FILE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Authorization": "Bearer " + auth.access_token,
            "User-Agent": USER_AGENT,
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "Accept": "*/*",
            # These three are load-bearing: without X-Variants the server
            # feature-gates image upload off and rejects the POST with an empty
            # 403 (observed live). X-AnchorMailbox / X-Scenario match the exact
            # browser request shape.
            "X-AnchorMailbox": f"Oid:{auth.oid}@{auth.tid}",
            "X-Scenario": "OfficeWebIncludedCopilot",
            "X-Variants": "feature.EnableImageSupportInUploadFile",
        },
    )
    # UPLOAD_FILE_URL is a fixed https://substrate.office.com constant; the
    # only caller-influenced input is the multipart body, never the URL.
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        logging.warning("UploadFile failed: HTTP %d", e.code)
        raise ProtocolError(
            f"image upload rejected by Sydney (HTTP {e.code}): {detail}"
        ) from e
    except urllib.error.URLError as e:
        raise ProtocolError(f"could not reach Sydney UploadFile: {e}") from e

    doc_id = result.get("docId")
    if not doc_id:
        keys = ", ".join(sorted(result.keys()))
        raise ProtocolError(f"Sydney UploadFile returned no docId (keys: {keys})")
    logging.info(
        "input image uploaded: docId=%s server_fileName=%s fileType=%s",
        doc_id,
        result.get("fileName"),
        result.get("fileType"),
    )
    return {
        "id": doc_id,
        "messageAnnotationType": "ImageFile",
        "messageAnnotationMetadata": {
            "@type": "File",
            "annotationType": "File",
            "fileType": image["file_type"],
            "fileName": image["filename"],
        },
    }


def fetch_generated_image(token_cache, url):
    """Downloads one image Sydney generated, returning `(bytes, mime_type)`.

    `url` is an `ImageReferenceUrls` entry straight off the wire (see
    `_note_generated_content`). Three things about this request are non-obvious
    and all three were established live -- see REVERSE_ENGINEERING.md's
    "Fetching a generated image" for the derivation and its controls:

      1. It needs a DIFFERENT access token than Chathub, for the
         `designerappservice` resource -- minted from the very same FOCI
         refresh token, so no extra credential is involved
         (`TokenCache.get(scope=DESIGNER_SCOPE)`; that token is an encrypted
         JWE, not a readable JWT).
      2. The URL's `fileToken` query parameter must be MOVED into a
         `filetoken` request header. Leaving it in the query string gets
         HTTP 400.
      3. The `Bearer ` prefix on `Authorization` is optional here (the browser
         omits it; both work).

    The host is asserted against `DESIGNER_HOST` before anything is sent. That
    matters: `url` is server-supplied, and this is the one place this proxy
    would attach a real access token to a URL it did not construct itself -- so
    a hostile or mangled `ImageReferenceUrls` must not be able to redirect a
    credentialed request anywhere else. Redirects are refused for the same
    reason.

    Neither the URL nor the `fileToken` is ever logged: that token grants
    access to the file, so it falls under the module docstring's "never log
    secrets" rule. Only sizes and the host are logged."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != DESIGNER_HOST:
        # Deliberately does NOT include `url` in the message: it carries the
        # capability-bearing fileToken.
        logging.error(
            "refusing to fetch generated content from an unexpected host "
            "(scheme=%r host=%r, expected https://%s)",
            parsed.scheme,
            parsed.hostname,
            DESIGNER_HOST,
        )
        raise ProtocolError(
            "Copilot returned a generated-content URL on an unexpected host "
            f"({parsed.hostname!r}); refusing to send credentials to it"
        )

    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    file_tokens = query.pop("fileToken", None) or []
    if not file_tokens or not file_tokens[0]:
        raise ProtocolError(
            "generated-content URL carried no fileToken, which this endpoint "
            "requires as a header"
        )
    fetch_url = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query, doseq=True),
            "",
        )
    )

    auth = token_cache.get(scope=DESIGNER_SCOPE)
    req = urllib.request.Request(
        fetch_url,
        method="GET",
        headers={
            "Authorization": "Bearer " + auth.access_token,
            "filetoken": file_tokens[0],
            "User-Agent": USER_AGENT,
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "Accept": "*/*",
        },
    )
    logging.info("fetching generated content from %s", DESIGNER_HOST)
    # The host was asserted above and redirects are refused below, so this is
    # not an arbitrary-URL open despite `url` being server-supplied.
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=90) as resp:  # nosec B310
            data = resp.read()
            mime = resp.headers.get("Content-Type") or "application/octet-stream"
    except urllib.error.HTTPError as e:
        logging.warning("generated-content fetch failed: HTTP %d", e.code)
        raise ProtocolError(
            f"could not download the generated file (HTTP {e.code})"
        ) from e
    except urllib.error.URLError as e:
        raise ProtocolError(f"could not reach {DESIGNER_HOST}: {e}") from e

    mime = mime.split(";", 1)[0].strip().lower()
    logging.info(
        "generated content downloaded: bytes=%d content_type=%s", len(data), mime
    )
    if not data:
        raise ProtocolError("the generated file downloaded as zero bytes")
    return data, mime


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses redirects, so a credentialed generated-content fetch can never
    be bounced off `DESIGNER_HOST` to somewhere else (see
    `fetch_generated_image`, which asserts the host of the URL it is given --
    an assertion a followed redirect would silently invalidate)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_chat_message(
    ws,
    session_id,
    text,
    locale="en-US",
    timezone="UTC",
    timezone_offset=0,
    tool_mode=None,
    image_annotations=None,
):
    trace_id = str(uuid.uuid4())
    logging.info(
        "sending chat message: session_id=%s trace_id=%s text_length=%d chars "
        "locale=%s tool_mode=%s images=%d",
        session_id,
        trace_id,
        len(text),
        locale,
        tool_mode,
        len(image_annotations or []),
    )
    # "action_request" mode suppresses Sydney's own code-interpreter-ish
    # OPTIONS_SETS entries (it fights against them, see
    # TOOL_MODE_OPTIONS_SETS's docstring); "code" mode deliberately does NOT
    # (it leans into them instead -- see
    # _render_tools_block_code_mode's docstring for why).
    options_sets = (
        TOOL_MODE_OPTIONS_SETS if tool_mode == "action_request" else OPTIONS_SETS
    )
    plugins = [] if tool_mode else [{"Id": "BingWebSearch", "Source": "BuiltIn"}]
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
    if image_annotations:
        # Attach uploaded input images to THIS turn's user message. The gptv
        # optionsSets that make Sydney read them are already in OPTIONS_SETS.
        payload["arguments"][0]["message"]["messageAnnotations"] = image_annotations
    ws.send_text(json.dumps(payload) + SIGNALR_RS)


#: The two keys inside Sydney's `throttling` block that carry the
#: per-conversation user-message quota. Naming them only fixes their position
#: at the front of the logged summary and lets the derived headroom figure be
#: computed; every OTHER key the block carries is logged too (see
#: `_throttling_summary`), because what that block actually contains in
#: practice is exactly the open question this logging exists to answer.
_THROTTLING_USED_KEY = "numUserMessagesInConversation"
_THROTTLING_MAX_KEY = "maxNumUserMessagesInConversation"


def _throttling_scalar_repr(value):
    """Renders one `throttling` value for the log, keeping to this project's
    metadata-only logging convention: scalars are shown as-is (they are
    counters/flags, not secrets), while anything longer or structured is
    reduced to its type and size rather than dumped. Nothing in this block has
    ever been observed to hold a payload, but it is server-controlled and
    logging it verbatim would be the one place this proxy prints an
    unbounded, unexamined server value into the log file."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value) if len(value) <= 80 else f"<str len={len(value)}>"
    if isinstance(value, (list, tuple, dict)):
        return f"<{type(value).__name__} len={len(value)}>"
    return f"<{type(value).__name__}>"


def _throttling_summary(info):
    """Renders Sydney's per-turn `throttling` block as one compact log line,
    e.g. `used=3 max=30 headroom=27 someFlag=False`.

    `used`/`max`/`headroom` come from the two documented quota keys; every
    other key present is appended verbatim (sorted, via
    `_throttling_scalar_repr`) so a field this proxy has never seen still
    shows up in the log the first time Sydney sends it, instead of being
    silently dropped by a hardcoded key list.

    Live-observed shape (2026-08-02, a real M365 Copilot account):
    `{"maxNumUserMessagesInConversation": 600,
      "numUserMessagesInConversation": 1,
      "numLongDocSummaryUserMessagesInConversation": 0}` -- i.e. fully
    populated, and carrying a third counter that was not previously documented
    anywhere in this project. That third key is exactly why this renders every
    key present instead of the two it knows about.

    `headroom` is only emitted when BOTH counters are real ints, so a
    partially-populated block still logs whatever it does carry rather than
    nothing at all."""
    parts = []
    used = info.get(_THROTTLING_USED_KEY)
    limit = info.get(_THROTTLING_MAX_KEY)
    used_ok = isinstance(used, int) and not isinstance(used, bool)
    limit_ok = isinstance(limit, int) and not isinstance(limit, bool)
    if used_ok:
        parts.append(f"used={used}")
    if limit_ok:
        parts.append(f"max={limit}")
    if used_ok and limit_ok:
        parts.append(f"headroom={limit - used}")
    # Every key NOT already rendered as a counter above is logged generically
    # -- including a counter key itself when it held an unexpected type (a
    # bool, say, which `isinstance(x, int)` would otherwise wave through as
    # `used=1`). Skipping those unconditionally would make the one shape most
    # worth noticing the one shape that vanishes from the log.
    rendered = set()
    if used_ok:
        rendered.add(_THROTTLING_USED_KEY)
    if limit_ok:
        rendered.add(_THROTTLING_MAX_KEY)
    for key in sorted(info):
        if key in rendered:
            continue
        value = info[key]
        if key == _METERING_KEY and isinstance(value, dict):
            parts.append(f"{key}[{_metering_summary(value)}]")
            continue
        parts.append(f"{key}={_throttling_scalar_repr(value)}")
    return " ".join(parts) if parts else "(empty)"


#: The `throttling` sub-object naming Sydney's own metered CAPABILITIES, and
#: the per-capability key holding the allowance. Live-observed 2026-08-02 on
#: the type-2 StreamItem frame (NOT on the type-1 `update` frames, which carry
#: only the three message counters) -- see `_metering_summary`.
_METERING_KEY = "metering"
_METERING_ALLOWANCE_KEY = "remainingAllowance"


def _metering_summary(metering):
    """Renders the `throttling.metering` block -- Sydney's own list of metered
    capabilities -- as `Name=allowance` pairs, e.g.
    `CodeInterpreter=0 FileReference=3 LLMOnly=100`.

    This is the closest thing to a capability manifest Sydney puts on the
    wire. Live-observed (2026-08-02, one real M365 Copilot account) with 15
    entries: `LLMOnly`, `ImageGeneration`, `ImageAnalysis`, `VisualCreator`,
    `GraphicArt`, `CodeInterpreter`, `TenantDataAccess`, `PersonalDataAccess`,
    `FileReference`, `DeepResearch`, `DeepWork`, `CopilotTuning`,
    `NotebookCowork`, `WXPAgentMode`, `CostQuota`.

    DO NOT read an allowance as availability, and do not make this proxy
    branch on one. Two measurements say the numbers do not mean what they
    look like: `CodeInterpreter` read 0 on a turn whose code interpreter
    demonstrably ran and returned a correct answer, and every value was
    byte-identical across 8 consecutive turns (`LLMOnly` stayed 100,
    `ImageGeneration` stayed 100) rather than counting down. The NAMES are
    solid evidence of a real capability taxonomy; the numbers are not yet
    interpretable. This is logged for diagnosis only -- see
    REVERSE_ENGINEERING.md's "Sydney's own capability surface, probed".

    Renders every capability and every sub-key present, for the same reason
    `_throttling_summary` does: this list is server-controlled and the point
    is to notice when it changes."""
    parts = []
    for name in sorted(metering):
        entry = metering[name]
        if not isinstance(entry, dict):
            parts.append(f"{name}={_throttling_scalar_repr(entry)}")
            continue
        allowance = entry.get(_METERING_ALLOWANCE_KEY)
        rendered = (
            [f"{name}={_throttling_scalar_repr(allowance)}"]
            if _METERING_ALLOWANCE_KEY in entry
            else []
        )
        rendered += [
            f"{name}.{k}={_throttling_scalar_repr(entry[k])}"
            for k in sorted(entry)
            if k != _METERING_ALLOWANCE_KEY
        ]
        parts.extend(rendered or [f"{name}=(empty)"])
    return " ".join(parts) if parts else "(empty)"


def _native_invocation_names(invocation):
    """Extracts the function names from a bot message's `invocation` field.

    This field is direct wire evidence that Sydney has a REAL, native,
    OpenAI-shaped function-calling mechanism for its own built-in tools --
    something this proxy's docstring asserted did not exist. Captured
    verbatim (2026-08-02) from an image-generation turn:

        {"function": {"name": "image_gen",
                      "arguments": "{\\"orientation\\":\\"landscape\\"}"},
         "id": "call_zvq9VI82lh3kvZdNlbDl5c", "type": "function"}

    -- i.e. exactly OpenAI's `tool_calls` entry shape, `call_`-prefixed id and
    all. Whether a CLIENT-declared tool can be injected into that namespace is
    a separate, unanswered question (Sydney's Local MCP bridge is the
    candidate mechanism -- see REVERSE_ENGINEERING.md); nothing here assumes
    it can. This parses the names purely so the log can show which built-in
    tool a turn actually used.

    The field is doubly JSON-encoded -- a JSON string holding an array of JSON
    strings, each holding one call object -- so this unwraps defensively and
    returns [] for anything it doesn't recognize rather than raising: a
    logging aid must never be able to break a chat turn."""
    if not isinstance(invocation, str):
        return []
    try:
        outer = json.loads(invocation)
    except ValueError:
        return []
    names = []
    for entry in outer if isinstance(outer, list) else [outer]:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except ValueError:
                continue
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


#: `status` value on a `contentGenerationProgressList` entry meaning "this
#: generated file is finished and its `ImageReferenceUrls` is now populated".
#: Earlier entries for the same file carry an empty `ImageReferenceUrls` list
#: (live-observed: 3 `Loading image` progress entries, only the last two with
#: a URL), so a fetch must use the FINAL one.
_CONTENT_READY_STATUS = 2


def _note_generated_content(
    msg, generated_content, seen_invocations, generated_urls=None
):
    """Records, from one bot message, (a) any NON-TEXT content Sydney
    generated for this turn, (b) the fetch URL of each finished file, and
    (c) any of its own built-in tools it invoked.

    All three ride on `Progress` entries -- Sydney's UI chrome, which
    `stream_chat_reply` otherwise discards -- so without this they are
    invisible. (a) is load-bearing: it is what lets an image-only turn be
    reported accurately instead of as a rate limit (see
    `UnsupportedContentError`). (b) is what `/v1/images/generations` returns.
    (c) is diagnostic only.

    `generated_urls`, when given, is appended to with each finished file's URL.
    NOTE these URLs are capability-bearing (their `fileToken` query parameter
    grants access to that file), so they are NEVER logged -- see
    `fetch_generated_image` and the module docstring's LOGGING section."""
    for item in msg.get("contentGenerationProgressList") or []:
        if not isinstance(item, dict):
            continue
        # `contentType` is the specific kind ("image"); `contentOrigin` on the
        # enclosing message is the producing capability ("ImageGeneration").
        kind = item.get("contentType") or msg.get("contentOrigin") or "unknown"
        generated_content[str(kind)] = generated_content.get(str(kind), 0) + 1
        if generated_urls is None or item.get("status") != _CONTENT_READY_STATUS:
            continue
        for url in item.get("ImageReferenceUrls") or []:
            if isinstance(url, str) and url and url not in generated_urls:
                generated_urls.append(url)
    for name in _native_invocation_names(msg.get("invocation")):
        if name in seen_invocations:
            continue
        seen_invocations.add(name)
        logging.info(
            "Sydney invoked one of its OWN built-in tools: name=%r (native "
            "OpenAI-shaped function call -- see _native_invocation_names)",
            name,
        )


#: Message for the image-only / non-text-only turn (see
#: `UnsupportedContentError`). Deliberately states that retrying is pointless:
#: the bug this replaces told callers the exact opposite.
_UNSUPPORTED_CONTENT_MSG = (
    "Copilot answered with generated non-text content (%s) and no text at "
    "all, so there is nothing a chat-completions response can carry. This is "
    "NOT a rate limit and retrying will not help. For a generated IMAGE, use "
    "POST /v1/images/generations (or the MCP `generate_image` tool), which "
    "return the image itself; otherwise ask for a text answer instead."
)


def _raise_if_non_text_only(yielded_len, generated_content):
    """Turns "the turn produced only non-text content" into a specific error
    instead of an empty reply that later looks like throttling. A no-op when
    any answer text arrived (a normal reply that merely happened to include a
    chart is a success) or when nothing was generated (a genuinely empty
    reply, which IS the throttle signature -- see
    `_looks_like_throttled_empty_reply`)."""
    if yielded_len or not generated_content:
        return
    summary = ", ".join(f"{k} x{v}" for k, v in sorted(generated_content.items()))
    logging.error(
        "Sydney generated only non-text content (%s) and no answer text -- "
        "surfacing as an explicit error, NOT as a throttle",
        summary,
    )
    raise UnsupportedContentError(_UNSUPPORTED_CONTENT_MSG % summary)


def _finish_turn(yielded_len, generated_content, generated_urls, content_sink):
    """Shared tail of `stream_chat_reply`'s two terminal frames (Completion and
    hub-Close): hands any generated non-text content to a caller that asked for
    it, and otherwise applies the text-only API's "an image-only turn is an
    error" rule.

    The two are mutually exclusive by design -- providing a `content_sink` IS
    the caller declaring it can represent non-text content, so raising at it
    would be wrong."""
    if content_sink is not None:
        content_sink["kinds"] = generated_content
        content_sink["images"] = list(generated_urls or [])
        logging.info(
            "turn generated non-text content: kinds=%s finished_files=%d",
            generated_content or "{}",
            len(content_sink["images"]),
        )
        return
    _raise_if_non_text_only(yielded_len, generated_content)


def stream_chat_reply(ws, timeout_s=120, content_sink=None):
    """Yield text deltas as the bot's reply streams in. Reassembles deltas by
    diffing successive full-text snapshots (`messages[].text`) rather than
    trying to interpret the `writeAtCursor` partial-append fields directly --
    simpler, and self-correcting if a snapshot ever doesn't extend cleanly.

    Only entries that are actually part of the ANSWER are yielded. Sydney
    interleaves its own transient status placeholders ("Gathering details...",
    "Working on it...", "Coding and executing") into the very same
    `target:"update"` frames, distinguished ONLY by carrying a `messageType`
    (`Progress`, `InternalLoaderMessage`, ...) where the real answer entry
    carries none -- so they must be filtered by type, not by text. See
    `ANSWER_MESSAGE_TYPES`/`TRANSIENT_MESSAGE_TYPES`.

    Snapshots are diffed PER `messageId`, because those interleaved entries are
    separate messages with their own ids: a single shared "last text" makes a
    switch between two messages look like a snapshot that doesn't extend the
    previous one, which the diff can only resolve by re-yielding the whole
    text.

    The same `target:"update"` frames also carry Sydney's own `throttling`
    block (a sibling of `messages`, not a message entry) holding its
    per-conversation quota counters. That is logged -- once per distinct state
    per turn, see `_throttling_summary` -- and otherwise left alone: it does
    not affect what this generator yields, and is not surfaced through the
    HTTP API. It exists in the log so a run that ends in Sydney's silent
    empty-reply throttle (see `_looks_like_throttled_empty_reply`) can be told
    apart from one that merely ran out of per-conversation quota.

    `content_sink`, when given, is a dict this fills in with
    `{"kinds": {...}, "images": [url, ...]}` for whatever NON-TEXT content the
    turn generated. Passing one also declares "this caller can handle non-text
    content", which suppresses `UnsupportedContentError`: an image-only turn is
    a normal, successful outcome for `/v1/images/generations` and the MCP
    `generate_image` tool, and an error for the text-only chat API. Leave it
    None and the pre-existing behavior is unchanged in every respect."""
    deadline = time.time() + timeout_s
    # Latest full-text snapshot of each answer message, keyed by `messageId`.
    snapshots = {}
    # Total answer text yielded so far -- the "did any answer text arrive?"
    # signal (and the logged length). Deliberately NOT a length of any one
    # message: see the type-2 branch below.
    yielded_len = 0
    # Last `throttling` block logged this turn, so a repeat logs only on
    # change. Live-observed (scripts/dump_frames.py, 2026-08-02): Sydney sends
    # it in exactly ONE frame per turn -- 1 of 10 frames carried it -- so this
    # is defensive rather than load-bearing today. It stays because the cost is
    # one comparison and the alternative (assume-once) would silently hide a
    # mid-turn quota change, which is precisely the event worth seeing.
    last_throttling = None
    #: Non-text content Sydney generated this turn (kind -> count), and the
    #: built-in tools it invoked. Both come off `Progress` chrome entries that
    #: are otherwise discarded -- see `_note_generated_content`.
    generated_content = {}
    seen_invocations = set()
    #: Fetch URLs of finished generated files, collected only when the caller
    #: asked for them (`content_sink`); None otherwise so the text-only path
    #: does not even accumulate these capability-bearing URLs.
    generated_urls = [] if content_sink is not None else None
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
                    throttling = arg.get("throttling")
                    if isinstance(throttling, dict) and throttling != last_throttling:
                        last_throttling = throttling
                        logging.info(
                            "Sydney throttling/quota state: %s",
                            _throttling_summary(throttling),
                        )
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
                        # Before the messageType filter below discards chrome:
                        # image generation and Sydney's own native tool calls
                        # ride on `Progress` entries and are invisible after it.
                        _note_generated_content(
                            msg, generated_content, seen_invocations, generated_urls
                        )
                        msg_type = msg.get("messageType")
                        if msg_type not in ANSWER_MESSAGE_TYPES:
                            # Sydney's own status/progress chrome, not answer
                            # text. Log lengths only, never the text itself:
                            # these are non-secret, but the logging convention
                            # here is metadata-only for message payloads.
                            log = (
                                logging.debug
                                if msg_type in TRANSIENT_MESSAGE_TYPES
                                else logging.warning
                            )
                            log(
                                "ignoring non-answer bot message: "
                                "messageType=%r text_length=%d",
                                msg_type,
                                len(msg.get("text") or ""),
                            )
                            continue
                        text = msg.get("text")
                        if text is None:
                            continue
                        last_text = snapshots.get(msg.get("messageId"), "")
                        delta = (
                            text[len(last_text) :]
                            if text.startswith(last_text)
                            else text
                        )
                        if delta:
                            yield delta
                            yielded_len += len(delta)
                        snapshots[msg.get("messageId")] = text

            elif ftype == 2:
                # The StreamItem carries its OWN `throttling` block, and it is
                # a strict superset of the one on the `update` frames above:
                # only this one has the `metering` capability list (live-
                # observed 2026-08-02 -- see `_metering_summary`). Logged
                # regardless of `yielded_len`, unlike the failure check below,
                # since a successful turn's quota state is just as diagnostic
                # as a failed one's.
                item = frame.get("item") or {}
                throttling = item.get("throttling")
                if isinstance(throttling, dict) and throttling != last_throttling:
                    last_throttling = throttling
                    logging.info(
                        "Sydney throttling/quota state: %s",
                        _throttling_summary(throttling),
                    )

                # The rest of the StreamItem is normally just an echo of the
                # user's own message
                # (persisted/enriched -- see module comments elsewhere), safe
                # to ignore. But when a turn is refused before any normal
                # `target:"update"` streaming ever starts (observed live:
                # Sydney's own rate limiting, "We're temporarily unable to
                # respond to this volume of requests. Please try again
                # later.", with `turnState: "Failed"`), the ONLY place that
                # failure text appears is here -- the type-1/`update` path
                # above never carries it. Without this check the turn would
                # silently "succeed" with an empty reply and no visible
                # cause. Guarded on `not yielded_len` so a normal completed
                # reply's own final StreamItem echo is never mistaken for a
                # failure -- and, since progress placeholders no longer count
                # as answer text, a turn that emitted only a "Gathering
                # details..." placeholder before failing is now correctly
                # reported as the failure it is instead of being masked by it.
                if not yielded_len:
                    for msg in item.get("messages") or []:
                        if (
                            msg.get("author") == "bot"
                            and msg.get("turnState") == "Failed"
                        ):
                            reason = msg.get("text") or "(no message from Sydney)"
                            logging.error("Sydney refused/failed the turn: %r", reason)
                            raise ThrottledError(f"Sydney refused the turn: {reason!r}")

            elif ftype == 3:  # Completion frame: this invocation is done
                logging.info(
                    "Chathub reply complete (total_length=%d chars)", yielded_len
                )
                _finish_turn(
                    yielded_len, generated_content, generated_urls, content_sink
                )
                return
            elif ftype == 7:  # hub closed
                logging.info(
                    "Chathub hub closed the connection (total_length so far=%d chars)",
                    yielded_len,
                )
                _finish_turn(
                    yielded_len, generated_content, generated_urls, content_sink
                )
                return
            # ignore: type 6 (ping), and Invocation frames with target "Metrics"

    logging.error("timed out waiting for a Chathub reply after %ds", timeout_s)
    raise ProtocolError("timed out waiting for a Chathub reply")


def run_chat_turn(
    token_cache, text, conversation_id=None, images=None, content_sink=None, **kwargs
):
    """End-to-end: cached/refreshed Sydney auth -> one Chathub turn -> yields
    text deltas. Generator; the WebSocket is closed once exhausted.
    `conversation_id`, if given, is passed straight through to
    `open_chathub()` -- see its docstring for why reusing one across calls
    matters.

    `images`, if given, is a list of input images (see
    `_extract_message_images`): each is uploaded to Sydney BEFORE the turn is
    sent (`upload_image_to_sydney`) and attached to the user message as an
    `ImageFile` annotation, so Sydney's GPT-V reads them. The upload reuses the
    same `conversation_id` the turn will run under; a fresh one is minted here
    when the caller didn't supply one, so the upload and the WebSocket agree.

    `content_sink` is passed straight through to `stream_chat_reply` -- see
    there; it is how `/v1/images/generations` collects a generated image
    instead of getting an `UnsupportedContentError`."""
    auth = token_cache.get()
    conversation_id = conversation_id or str(uuid.uuid4())
    image_annotations = None
    if images:
        image_annotations = [
            upload_image_to_sydney(auth, conversation_id, img) for img in images
        ]
    ws, session_id = open_chathub(auth, conversation_id=conversation_id)
    try:
        send_chat_message(
            ws, session_id, text, image_annotations=image_annotations, **kwargs
        )
        yield from stream_chat_reply(ws, content_sink=content_sink)
    finally:
        ws.close()
        logging.info("Chathub WebSocket closed (session_id=%s)", session_id)


#: How the caller's image description is worded to Sydney. Image generation is
#: reached by ASKING for it in a chat turn -- Sydney has no separate
#: image-generation API -- so a bare noun phrase like "a red bicycle" (which is
#: exactly what OpenAI's `/v1/images/generations` `prompt` normally is) would
#: otherwise just get a prose answer about bicycles. Like the tool-calling
#: emulation, this is prompt-level steering with no formal contract: Sydney can
#: always answer in prose instead, which `generate_image` surfaces as an
#: explicit error rather than an empty success.
_IMAGE_PROMPT_TEMPLATE = "Generate an image based on this description: %s"

#: Cap on how many generated files a single image request will download, so a
#: surprising server response can't make this proxy fetch an unbounded number
#: of multi-megabyte files. Sydney returns one image per turn in practice.
MAX_GENERATED_IMAGES = 4


def generate_image(token_cache, prompt, n=1):
    """Asks Copilot to generate an image and returns `[(bytes, mime), ...]`.

    Two steps, both live-verified (see REVERSE_ENGINEERING.md's "Fetching a
    generated image"): run an ordinary Chathub turn that asks for an image and
    collect the finished files' URLs off its `Progress` frames
    (`content_sink`), then download each one with the separate
    `designerappservice` token (`fetch_generated_image`).

    `n` images are requested by running `n` INDEPENDENT turns: Sydney produces
    one image per turn and has no batch parameter, so this is the only honest
    way to serve `n > 1`. Capped at `MAX_GENERATED_IMAGES`.

    Raises `UnsupportedContentError` when a turn produced no image at all --
    Sydney answered in prose, refused on content policy, or was throttled.
    That is a real and not-rare outcome (this is prompt-steering, see
    `_IMAGE_PROMPT_TEMPLATE`), and it must be an explicit error rather than an
    empty-but-successful response."""
    wanted = max(1, min(int(n or 1), MAX_GENERATED_IMAGES))
    logging.info(
        "image generation requested: n=%d prompt_length=%d", wanted, len(prompt)
    )
    out = []
    for attempt in range(wanted):
        sink = {}
        text = "".join(
            run_chat_turn(
                token_cache, _IMAGE_PROMPT_TEMPLATE % prompt, content_sink=sink
            )
        )
        urls = sink.get("images") or []
        if not urls:
            # Sydney answered SOMETHING but generated no file. Its own words are
            # the single most useful diagnostic here (a content-policy refusal
            # reads completely differently from a prose answer), so pass a
            # trimmed version through rather than a generic message.
            detail = " ".join(text.split())[:300] or "(no text either)"
            logging.error(
                "image generation produced no image on attempt %d/%d "
                "(kinds=%s, reply_length=%d)",
                attempt + 1,
                wanted,
                sink.get("kinds") or "{}",
                len(text),
            )
            raise UnsupportedContentError(
                "Copilot did not generate an image for that prompt. It is "
                "asked for one in an ordinary chat turn, so it can decline or "
                f'answer in prose instead. It said: "{detail}"'
            )
        for url in urls[: MAX_GENERATED_IMAGES - len(out)]:
            out.append(fetch_generated_image(token_cache, url))
    logging.info(
        "image generation finished: images=%d total_bytes=%d",
        len(out),
        sum(len(d) for d, _ in out),
    )
    return out


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


def _decode_data_uri_image(url):
    """Decodes a `data:image/...;base64,...` URI into `(bytes, file_type)`,
    or returns `(None, None)` if it isn't a base64 image data URI this proxy
    supports. `file_type` is the extension with no dot (e.g. "png")."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    header, _, payload = url[len("data:") :].partition(",")
    if not payload:
        return None, None
    mime = header.split(";", 1)[0].strip().lower()
    file_type = _IMAGE_MIME_TO_FILETYPE.get(mime)
    if file_type is None:
        return None, None
    if "base64" not in header.lower():
        return None, None
    try:
        data = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None, None
    return data, file_type


def _extract_message_images(messages):
    """Collects input images the caller attached to the LATEST user turn, as
    OpenAI-style `image_url` content parts:

        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,...."}}

    (the `image_url` value may also be a bare URL string). Only inline
    `data:` URIs are supported -- an `http(s)://` image URL is skipped with a
    warning, because fetching an arbitrary caller-supplied URL server-side is
    a request-forgery footgun this proxy deliberately avoids.

    Returns a list of `{data, filename, file_type}` dicts (empty if none),
    drawn only from the last message whose role is `user`: Sydney attaches
    input images to the current turn, and in a growing OpenAI transcript only
    the newest user turn is the current one. Applies MAX_INPUT_IMAGES /
    MAX_INPUT_IMAGE_BYTES defensively."""
    last_user = None
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = m
    if last_user is None:
        return []
    content = last_user.get("content")
    if not isinstance(content, list):
        return []

    images = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        iu = part.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else iu
        data, file_type = _decode_data_uri_image(url)
        if data is None:
            logging.warning(
                "skipping an image_url content part: only inline base64 "
                "data: URIs of a supported image type are accepted"
            )
            continue
        if len(data) > MAX_INPUT_IMAGE_BYTES:
            logging.warning(
                "skipping an input image of %d bytes (over the %d-byte cap)",
                len(data),
                MAX_INPUT_IMAGE_BYTES,
            )
            continue
        images.append(
            {
                # The exact data: URI string is what Sydney's UploadFile wants
                # as its FileBase64 field (see upload_image_to_sydney); `data`
                # is the decoded bytes, kept only for the size cap and logging.
                "data_uri": url,
                "data": data,
                "file_type": file_type,
                "filename": f"image_{len(images) + 1}.{file_type}",
            }
        )
        if len(images) >= MAX_INPUT_IMAGES:
            logging.warning(
                "reached the %d-image input cap; ignoring any further images",
                MAX_INPUT_IMAGES,
            )
            break
    return images


# ------------------------------------------------------------------------------
# Sydney-native conversation continuity
#
# Live-confirmed (see REVERSE_ENGINEERING.md's "Sydney-native conversation
# continuity" section for the exact probe and transcript): opening a BRAND
# NEW Chathub WebSocket -- fresh session id, fresh trace/request ids, no
# history resent -- but reusing a PRIOR call's `ConversationId` gets a reply
# that demonstrates knowledge of that prior call's turn. Sydney's server-side
# conversation memory is keyed on `ConversationId` itself, not on keeping one
# WebSocket connection alive, so it's usable across separate, independent
# `/v1/chat/completions` HTTP requests exactly like this proxy already
# handles everything else.
#
# The OpenAI chat-completions API is stateless, though: the client resends
# its whole growing `messages[]` array every call and this proxy is given no
# conversation id of its own to key on. So recognizing "this request is a
# continuation of a conversation I already relayed to Sydney" has to be done
# by fingerprinting `messages[]` itself -- see _conversation_fingerprint,
# _match_continuation, and ConversationSessionStore below. A request is
# treated as a continuation when some LONGEST prefix of its `messages[]`
# byte-matches (in the fields that matter) the exact `messages[]` array a
# previous request sent -- i.e. the client took the previous response,
# appended its own rendering of it plus one or more new turns, and resent the
# whole thing. When that's true, only the new message(s) past that prefix
# need to be sent to Sydney (see _render_continuation_delta) -- Sydney
# already remembers everything before them. The matched prefix's trailing
# assistant turn (the client's copy of the reply Sydney already produced into
# that conversation) is dropped from the delta; only the tool results / new
# user turns the client genuinely added are sent.
#
# Keying on the client's resent INPUT -- not on `messages[] + [our reply]`,
# as an earlier design did -- is what makes this robust to a client that
# re-serializes the assistant turn differently than this proxy emitted it (a
# tool-calling agent reconstructing the `tool_calls` message, say): the
# fingerprint never depends on the reply's wording, and "longest prefix"
# tolerates any number of appended messages per round, not just one.
#
# Any time a continuation can't be established -- the very first turn of a
# conversation, a genuinely branched/edited history, a different credential,
# or this process having restarted (the store is in-memory only) -- this
# proxy falls back to the pre-existing context-stuffing behavior with a
# brand-new `ConversationId`, exactly as if this feature didn't exist. A
# false negative here just costs one extra context-stuffed turn; it is never
# a correctness problem. Turned off entirely with
# --disable-conversation-continuity.
#
# Requests WITH `tools` are handled too (the exclusion an earlier design kept
# has been lifted). The tool-calling emulation is probabilistic and normally
# re-injects its whole instructions block on every turn as an independent,
# stateless Chathub call; a continuation instead reuses the live Sydney
# conversation for a SINGLE delta turn with NO schema re-injected -- betting
# that Sydney still remembers the schema it was taught on the conversation's
# first turn (_render_continuation_delta sends only a compact convention
# reminder). If that bet doesn't pay off -- the delta turn produces no
# parseable call, or the Chathub turn errors -- _run_tool_call_turn falls
# back to a fresh, fully schema-reinjected, multi-attempt turn under a
# brand-new conversation. So a tools continuation can only ever save a
# round-trip, never change or lose the answer the fresh path would produce;
# the matched session is popped before the attempt, so a failed continuation
# leaves nothing stale behind.
# ------------------------------------------------------------------------------


def _conversation_fingerprint(messages):
    """Stable identity for "this exact `messages[]` array", used as the
    ConversationSessionStore's key (both to `remember` a turn and, via every
    prefix, to look one up -- see `_prefix_fingerprints`/`_match_continuation`).
    Hashes each message's role, name, rendered text, `tool_calls`, and
    `tool_call_id` in order -- anything else in a message dict (extra
    client-specific fields) is ignored, but any difference in these fields,
    their order, or the message count changes the fingerprint.

    Deliberately strict, on purpose: a false negative (two fingerprints
    differ when the conversation is "really" the same) just costs one extra
    context-stuffed turn -- always safe. A false positive (two different
    conversations hashing the same) would risk answering from the wrong
    conversation's Sydney-side memory -- never safe. SHA-256 over an
    unambiguous per-message encoding (NUL between fields, unit-separator
    between messages) makes an accidental collision between two
    genuinely-different conversations not a practical concern."""
    hasher = hashlib.sha256()
    for m in messages:
        _update_message_hash(hasher, m)
    return hasher.hexdigest()


def _update_message_hash(hasher, m):
    """Folds one message into `hasher` using the exact per-message encoding
    `_conversation_fingerprint` relies on (NUL between fields, unit-separator
    after the message). Factored out so `_prefix_fingerprints` can compute
    every prefix's fingerprint in a single pass while staying byte-for-byte
    identical to `_conversation_fingerprint(messages[:k])` -- the two MUST
    agree for prefix matching (see `_match_continuation`) to be sound."""
    hasher.update(str(m.get("role", "")).encode("utf-8", "replace"))
    hasher.update(b"\0")
    hasher.update(str(m.get("name", "")).encode("utf-8", "replace"))
    hasher.update(b"\0")
    hasher.update(_message_text(m).encode("utf-8", "replace"))
    hasher.update(b"\0")
    tool_calls = m.get("tool_calls")
    if tool_calls:
        hasher.update(json.dumps(tool_calls, sort_keys=True).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(str(m.get("tool_call_id", "")).encode("utf-8", "replace"))
    hasher.update(b"\x1f")  # unit separator between messages


def _prefix_fingerprints(messages):
    """Returns a list `fps` of length `len(messages) + 1` where
    `fps[k] == _conversation_fingerprint(messages[:k])` for every k. Computed
    in ONE pass by snapshotting the running hash after each message
    (`hashlib` objects support `.copy()`), so matching every prefix of an
    incoming request costs O(total bytes), not O(n * total bytes) -- see
    `_match_continuation`, which needs every prefix's fingerprint to find the
    longest already-seen one."""
    hasher = hashlib.sha256()
    fps = [hasher.copy().hexdigest()]  # k == 0: the empty prefix
    for m in messages:
        _update_message_hash(hasher, m)
        fps.append(hasher.copy().hexdigest())
    return fps


def _match_continuation(store, messages, oid):
    """Aggressive continuation matcher: find the LONGEST prefix of `messages`
    that maps to a live Sydney conversation in `store`, POP it (single-use,
    for branch safety -- see ConversationSessionStore), and return
    `(session, matched_fingerprint, delta_messages)`; or None on no match.

    "Longest first" means the smallest possible delta -- Sydney already holds
    the most in its own server-side memory. `delta_messages` is everything
    after the matched prefix with any LEADING `assistant`-role messages
    removed: those are the client's own re-rendering of the reply Sydney
    already produced into that conversation, so re-sending them would either
    be redundant or (worse) trip the fabricated-prior-turn refusal shape (see
    `_render_conversation_prompt`'s docstring). Everything after those -- the
    tool results and/or new user turns the client actually added -- is the
    genuine delta.

    Unlike the original exact `messages[:-1]` matcher, this tolerates a client
    that appends MORE than one message per round and/or re-serializes the
    assistant turn differently than this proxy emitted it (exactly what
    OpenHands' mock-function-calling loop does -- it reconstructs the
    assistant `tool_calls` message and injects a separate execution-result
    message each round), which the strict matcher could never recognize as a
    continuation.

    Returns None (falling back to a fresh, fully context-stuffed turn -- always
    safe) if no prefix matches, or if the only thing after the matched prefix
    is the assistant reply itself with nothing genuinely new to send."""
    fps = _prefix_fingerprints(messages)
    # Try the longest prefix first (smallest delta). A prefix must leave at
    # least one message as the delta, so k tops out at len(messages) - 1.
    for k in range(len(messages) - 1, 0, -1):
        session = store.lookup(fps[k], oid)  # POPS only on an actual match
        if session is None:
            continue
        remaining = messages[k:]
        i = 0
        while i < len(remaining) and remaining[i].get("role") == "assistant":
            i += 1
        delta_messages = remaining[i:]
        if not delta_messages:
            # Matched, but the client added nothing past our own reply -- the
            # entry is already popped, so this cleanly falls back to a fresh
            # turn (never a wrong answer, just one context-stuffed turn).
            return None
        return session, fps[k], delta_messages
    return None


def _render_continuation_delta(
    delta_messages, tools=None, tool_choice=None, mode="action_request"
):
    """Renders ONLY the new messages (`_match_continuation`'s `delta_messages`)
    as the next turn on a reused Sydney `ConversationId`. Sydney already holds
    everything before them in its own server-side memory, so -- unlike
    `_render_conversation_prompt` -- this sends just the delta: no transcript,
    and when `tools` are present NO full schema re-injection either, only a
    compact reminder of the calling convention (the bet the whole
    tools-continuation path makes is that Sydney remembers the schema it was
    taught on the conversation's first turn; if that bet ever fails the caller
    falls back to a fresh, schema-reinjected turn -- see `_run_tool_call_turn`).

    Tool-result (`role: "tool"`) messages are folded into ordinary
    parenthetical context worded to match `mode`, exactly as
    `_render_conversation_prompt._fold_pending` does, so a continuation reads
    the same way a fresh turn's tail would and avoids the fabricated-history
    refusal shape. `tool_choice` is accepted for signature parity with the
    fresh renderer; the convention reminder is fixed wording and doesn't vary
    with it. Returns None if there's no usable text at all (the caller then
    falls back to a fresh, context-stuffed turn)."""
    clean = _neutralize_tool_word if tools else (lambda s: s)
    tool_call_names = {}
    pending_results = []
    chunks = []

    def fold(text):
        if not pending_results:
            return text
        if mode == "code":
            preamble = "\n".join(
                f"({_CODE_MODE_INVOKE_NAME}('{n}', ...) returned: {t})"
                for n, t in pending_results
            )
        else:
            preamble = "\n".join(
                f"(Result of your earlier {n} request: {t})" for n, t in pending_results
            )
        pending_results.clear()
        return f"{preamble}\n\n{text}" if text else preamble

    for m in delta_messages:
        role = m.get("role")
        text = clean(_message_text(m))
        if role == "assistant":
            # A trailing assistant turn inside the delta is unusual (leading
            # ones are dropped by _match_continuation), but keep any genuine
            # text and harvest tool-call names for folding a following result.
            for tc in m.get("tool_calls") or []:
                tool_call_names[tc.get("id")] = (tc.get("function") or {}).get(
                    "name", "?"
                )
            if text:
                chunks.append(text)
            continue
        if role == "tool":
            name = tool_call_names.get(m.get("tool_call_id"), "unknown_request")
            pending_results.append((name, text or "(empty result)"))
            continue
        # user / system / anything else: fold any buffered tool result onto it
        text = fold(text)
        if text:
            chunks.append(text)

    if pending_results:
        chunks.append(fold("Please continue."))

    body = "\n\n".join(c for c in chunks if c)
    if not body:
        return None
    if not tools:
        return body
    if mode == "code":
        reminder = (
            "If you need one of the capabilities described earlier in this "
            f"conversation, write and run a short Python snippet that calls "
            f"{_CODE_MODE_INVOKE_NAME}(name, arguments) and reads the result, "
            "exactly as before. Otherwise, just answer normally in plain text."
        )
    else:
        reminder = (
            "If you need to take an action, reply with ONLY one or more "
            f"{_TOOL_CALL_OPEN}...{_TOOL_CALL_CLOSE} blocks as described earlier "
            "in this conversation and nothing else. Otherwise, reply normally "
            "in plain text."
        )
    return f"{body}\n\n{reminder}"


class ConversationSession:
    """One entry in ConversationSessionStore: the stable Sydney
    `ConversationId` a completed turn used, plus enough bookkeeping to
    decide whether it's still safe to reuse."""

    __slots__ = ("conversation_id", "created_at", "last_used_at", "oid", "turn_count")

    def __init__(self, conversation_id, oid, turn_count=1):
        self.conversation_id = conversation_id
        self.oid = oid
        self.created_at = time.time()
        self.last_used_at = self.created_at
        #: how many Chathub turns this ConversationId has now been used for
        #: across this proxy's whole tracked chain, purely for logging --
        #: carried forward across each pop-and-remember hop by _plan_chat_turn
        #: and _run_plain_turn/_stream_plain_turn, not reset to 1 each time.
        self.turn_count = turn_count


class ConversationSessionStore:
    """Recognizes when a `/v1/chat/completions` request is an exact
    continuation of a conversation this proxy already relayed to Sydney, so
    that turn can reuse Sydney's own server-side conversation memory (a
    stable Chathub `ConversationId`) instead of context-stuffing the whole
    growing transcript into one big turn every single call -- see the
    section comment above for the full design rationale.

    Keyed by `_conversation_fingerprint()`, not by any client-supplied id --
    the OpenAI API is stateless and gives this proxy nothing else to key on.
    `oid` is checked on lookup too, as cheap defense-in-depth against
    reusing a session across a credential change this process didn't expect
    (in practice one running proxy process speaks as one fixed identity, so
    this should never actually differ).

    Each entry is SINGLE-USE: `lookup()` POPS the matching entry rather than
    just peeking at it. This matters for correctness, not just bookkeeping
    hygiene -- confirmed by `tests/test_continuity.py`'s
    "branch" case catching this as a real bug during development. Sydney's
    own conversation state is a single, linear, mutable timeline: once one
    request has extended it with a given next message, that `ConversationId`
    server-side no longer matches what any OTHER, differently-worded next
    message from the same historical point would expect. If a stale entry
    were left in place after being used, a client that (accidentally or by
    design, e.g. regenerating a response, or two retries racing) sends a
    DIFFERENT follow-up from the same earlier point would incorrectly reuse
    that same `ConversationId` -- Sydney would then answer from a
    server-side history that includes a turn the client's own local
    `messages[]` array knows nothing about, a real correctness bug, not
    just a wasted optimization. Popping means a second, divergent request
    from the same point gets a clean cache miss and safely falls back to a
    brand-new conversation instead.

    Bounded in size (MAX_SESSIONS, LRU eviction) and in time (IDLE_TTL_S) so
    a long-running proxy process can't accumulate un-consumed entries
    without limit -- this store is correctness-neutral in the OTHER
    direction (an evicted entry just costs one extra context-stuffed turn,
    never a wrong answer), so simple bounds are enough; nothing here needs
    to be persisted across a proxy restart or be perfectly precise.
    Thread-safe: `make_handler` wires one shared instance into a
    `ThreadingHTTPServer` -- popping under the same lock as everything else
    also means two concurrent requests racing to continue the same
    conversation can't both win; the loser gets a clean miss instead of a
    corrupted double-send onto the same `ConversationId`."""

    MAX_SESSIONS = 500
    IDLE_TTL_S = 2 * 60 * 60  # 2 hours

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # fingerprint -> ConversationSession

    def lookup(self, fingerprint, oid):
        """Returns the matching ConversationSession and POPS it (see class
        docstring for why this must be single-use), or None on a miss (no
        entry, or an `oid` mismatch -- an `oid` mismatch does NOT pop the
        entry, since it wasn't actually consumed by this call)."""
        with self._lock:
            self._evict_expired_locked()
            session = self._sessions.get(fingerprint)
            if session is None or session.oid != oid:
                return None
            del self._sessions[fingerprint]
            return session

    def remember(self, fingerprint, conversation_id, oid, turn_count=1):
        with self._lock:
            self._evict_expired_locked()
            if (
                fingerprint not in self._sessions
                and len(self._sessions) >= self.MAX_SESSIONS
            ):
                self._evict_lru_locked()
            self._sessions[fingerprint] = ConversationSession(
                conversation_id, oid, turn_count
            )

    def forget(self, fingerprint):
        """Evicts one cached session -- called when a Chathub turn that
        tried to reuse it failed, so the client's very next attempt
        (which necessarily resends the identical prefix, since that's the
        OpenAI-client contract this whole mechanism relies on)
        gets a clean cache miss and falls back to a brand-new conversation,
        rather than repeatedly retrying the same possibly-broken
        `ConversationId` forever. A no-op if `fingerprint` is None or
        already gone."""
        if fingerprint is None:
            return
        with self._lock:
            self._sessions.pop(fingerprint, None)

    def _evict_expired_locked(self):
        cutoff = time.time() - self.IDLE_TTL_S
        expired = [k for k, s in self._sessions.items() if s.last_used_at < cutoff]
        for k in expired:
            del self._sessions[k]

    def _evict_lru_locked(self):
        if not self._sessions:
            return
        oldest_key = min(self._sessions, key=lambda k: self._sessions[k].last_used_at)
        del self._sessions[oldest_key]


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

_CODE_MODE_INVOKE_NAME = "invoke_capability"
_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)

# Two independent conventions this proxy can teach the model, tried in this
# order across retry attempts (see _TOOL_CALL_MODES/_run_tool_call_turn):
#   "code" -- ask the model to call a pre-loaded Python function,
#     invoke_capability(name, arguments), from ordinary code it writes and
#     "runs" via Sydney's own code interpreter -- i.e. lean INTO Sydney's
#     strong, repeatedly-observed preference for solving things by writing
#     and executing Python, instead of fighting it. See
#     _render_tools_block_code_mode's docstring and REVERSE_ENGINEERING.md's
#     "Code-mode tool-calling emulation" section for the live-testing
#     rationale and results.
#   "action_request" -- the original convention: a JSON object wrapped in
#     <action_request>...</action_request> tags, with Sydney's own code
#     interpreter/plugins explicitly suppressed so there's nothing else for
#     it to reach for. Kept as a fallback since it's a completely different
#     mechanism from "code" mode and occasionally succeeds when "code" mode
#     doesn't (or vice versa) -- see REVERSE_ENGINEERING.md.


def _render_tools_block(tools, tool_choice):
    """Renders an OpenAI `tools` array (+ `tool_choice`) into the plain-text
    instructions block for the "action_request" convention: a single-line
    JSON object `{"name": ..., "arguments": {...}}` wrapped in
    `<action_request>...</action_request>` tags. See
    `_render_tools_block_code_mode` for the other ("code") convention.

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
        (
            "IMPORTANT: for this reply, you have no internet access, no code "
            "interpreter, and no ability to execute code or search the web -- "
            "any apparent ability to do so is disabled for this conversation. "
            "The ONLY way to get information or perform actions you don't "
            "already have is to request it from the user with the exact format "
            "below, and nothing else in your reply (no markdown code fences, no "
            "explanation before or after it) -- arguments must be one line of "
            "valid JSON matching the schema given for that capability:"
        ),
        "",
        _TOOL_CALL_OPEN,
        '{"name": "<capability name>", "arguments": {<arguments as JSON>}}',
        _TOOL_CALL_CLOSE,
        "",
        (
            "If you don't need to request anything, just reply normally in "
            "plain text -- do not use the tags above unless you are actually "
            "making a request. You may make more than one request by using "
            f"multiple {_TOOL_CALL_OPEN}...{_TOOL_CALL_CLOSE} blocks in the same "
            "reply."
        ),
        "",
        "Capabilities you can request this way:",
    ]
    lines.extend(_render_capability_list(tools))
    lines.extend(
        _render_tool_choice_directive(
            tool_choice,
            forced='You MUST request "{name}" now, using the format above.',
            required="You MUST make one of the requests above now, using the format above.",
        )
    )
    return "\n".join(lines)


def _render_tools_block_code_mode(tools, tool_choice):
    """Renders an OpenAI `tools` array (+ `tool_choice`) into the plain-text
    instructions block for the "code" convention: rather than fighting
    Sydney's strong, repeatedly-observed preference for solving things by
    writing and "running" Python code (see `_render_tools_block`'s docstring
    for the failed attempts to suppress that behavior), this convention
    leans directly into it. The model is told its Python environment
    already has one extra function loaded, `invoke_capability(name,
    arguments)`, and is asked to call it from ordinary code exactly the way
    it already tends to solve things -- no JSON-in-tags convention, no
    "your other capabilities are disabled" framing.

    `_extract_code_mode_calls()` parses the resulting code (from inside
    Sydney's own ```-fenced code blocks, which it reliably uses for
    anything it treats as "code to run") for calls to this function via a
    real Python AST walk, so the model is free to write whatever ordinary
    code it wants around the call(s) -- loops, comments, multiple calls,
    print()-ing the result, etc. -- and they'll still be found. See
    REVERSE_ENGINEERING.md's "Code-mode tool-calling emulation" section for
    the live-testing rationale and results this design is based on.

    Still prompt-level steering, not a guarantee -- same caveat as
    `_render_tools_block`."""
    lines = [
        "",
        (
            "Your Python code execution environment has one extra function "
            "already loaded and ready to use -- you do not need to define it "
            "yourself, it's already there:"
        ),
        "",
        f"    {_CODE_MODE_INVOKE_NAME}(name, arguments)",
        "",
        (
            "Write and run ordinary Python code exactly as you already would "
            "for any other task. Whenever you need one of the capabilities "
            f"listed below, call {_CODE_MODE_INVOKE_NAME}(name, arguments) with "
            "the capability's name (a string) and its arguments (a dict "
            "matching the schema shown for it), then read its return value "
            "before continuing -- for example:"
        ),
        "",
        (
            f"    result = {_CODE_MODE_INVOKE_NAME}("
            "'<capability name>', {<arguments matching its schema>})"
        ),
        "    print(result)",
        "",
        (
            "If you don't need any of these capabilities for this reply, just "
            "answer normally in plain text without writing any code at all. "
            "You can call more than one of them in the same script if you need "
            "to, and can write as much ordinary code around the call(s) as you "
            "need."
        ),
        "",
        "Capabilities available this way:",
    ]
    lines.extend(_render_capability_list(tools))
    lines.extend(
        _render_tool_choice_directive(
            tool_choice,
            forced=f"You MUST call {_CODE_MODE_INVOKE_NAME}('{{name}}', ...) now.",
            required=f"You MUST call {_CODE_MODE_INVOKE_NAME}(...) now, for one of "
            "the capabilities above.",
        )
    )
    return "\n".join(lines)


def _render_tool_choice_directive(tool_choice, forced, required):
    """Shared by both `_render_tools_block()` and
    `_render_tools_block_code_mode()`: renders OpenAI's `tool_choice` field
    into trailing directive lines (empty when `tool_choice` doesn't force
    anything). `forced` is a format string with a `{name}` placeholder for
    the specific forced capability; `required` is the "must use one of
    them" wording -- each convention supplies its own phrasing."""
    if isinstance(tool_choice, dict):
        forced_name = (tool_choice.get("function") or {}).get("name")
        if forced_name:
            return ["", forced.format(name=forced_name)]
    elif tool_choice == "required":
        return ["", required]
    return []


def _render_capability_list(tools):
    """Shared by both `_render_tools_block()` and
    `_render_tools_block_code_mode()`: renders each tool's name (left
    untouched -- must round-trip verbatim, see _neutralize_tool_word's
    docstring), neutralized description, and JSON parameters schema as a
    `"- name: description"` / `"  parameters schema: {...}"` pair of
    lines."""
    lines = []
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name", "?")
        desc = _neutralize_tool_word(fn.get("description", ""))
        params = fn.get("parameters", {})
        lines.append(f"- {name}: {desc}")
        lines.append(f"  parameters schema: {json.dumps(params)}")
    return lines


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
    (the code interpreter, mainly) instead of ever considering either of
    this proxy's tool-calling conventions ("action_request" or "code"),
    confirmed by live A/B testing during development (see
    REVERSE_ENGINEERING.md's "Tool-calling emulation" section). Coding-agent
    system prompts (OpenCode's, OpenHands', etc.) are saturated with exactly
    this word ("you have access to the following tools", tool
    names/descriptions, etc.), so this proxy cannot simply ask callers to
    avoid it -- it has to actively launder it out of the entire rendered
    prompt whenever `tools` is present. Applied only to free-form text
    (system/user/assistant/tool-result content and tool descriptions),
    never to the structural JSON (a tool's `name` field, argument/schema
    keys) that this proxy needs to parse back out of the reply verbatim."""

    def _repl(m):
        word = m.group(0)
        replacement = "capabilities" if word.lower() == "tools" else "capability"
        return replacement.capitalize() if word[0].isupper() else replacement

    return _TOOL_WORD_RE.sub(_repl, text)


def _extract_tool_calls(reply_text):
    """Parses `reply_text` for the "action_request" convention's
    `<action_request>{...}</action_request>` tags (see `_render_tools_block`).
    Returns `(remaining_text, tool_calls)`: `tool_calls` is a list of
    `{"id", "name", "arguments_json"}` dicts (already carrying a synthesized
    OpenAI-style call id and a compact-JSON-encoded arguments string, ready
    to drop into an OpenAI `tool_calls` response entry), and `remaining_text`
    is whatever plain text was outside the tags (may be empty). A tag pair
    with unparseable/incomplete JSON inside is logged and dropped rather
    than raised -- the model attempted a tool call but produced broken
    output, which should surface to the client as "the model didn't call a
    tool" rather than crash this proxy's response entirely."""
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


def _extract_invoke_args(call_node):
    """Given an `ast.Call` node for `invoke_capability(...)`, returns
    `(name, arguments)` by literal-evaluating whichever AST nodes hold them
    -- handles both positional (`invoke_capability('x', {...})`) and keyword
    (`invoke_capability(name='x', arguments={...})`) call styles, and any
    mix of the two. Raises `ValueError` if the call doesn't actually carry a
    usable name/arguments pair (e.g. the model computed them from a
    variable rather than writing a literal) -- the caller treats that as
    "this particular call couldn't be parsed", not a hard failure."""
    positional = list(call_node.args)
    keywords = {kw.arg: kw.value for kw in call_node.keywords if kw.arg}

    name_node = keywords.get("name")
    if name_node is None and positional:
        name_node = positional[0]
    arguments_node = keywords.get("arguments")
    if arguments_node is None and len(positional) > 1:
        arguments_node = positional[1]

    if name_node is None:
        raise ValueError("invoke_capability(...) call has no name argument")
    name = ast.literal_eval(name_node)
    arguments = ast.literal_eval(arguments_node) if arguments_node is not None else {}
    # ValueError, not TypeError (TRY004): every failure mode of parsing an
    # LLM-authored invoke_capability(...) call is reported to the caller as a
    # ValueError, and callers catch exactly that. A malformed argument is bad
    # *data*, not a type error in our own code.
    if not isinstance(name, str):
        raise ValueError(  # noqa: TRY004 - see comment above
            "invoke_capability(...) name argument is not a string"
        )
    if not isinstance(arguments, dict):
        raise ValueError(  # noqa: TRY004 - see comment above
            "invoke_capability(...) arguments argument is not a dict"
        )
    return name, arguments


def _extract_code_mode_calls(reply_text):
    """Parses `reply_text` for calls to the "code" convention's function,
    `invoke_capability(name, arguments)` (see
    `_render_tools_block_code_mode`), inside any ```-fenced code block(s) in
    the reply -- Sydney's own code-interpreter convention reliably wraps
    generated code this way (e.g. "Coding and executing```python\n...\n```"),
    confirmed across many live captures during development; if no fenced
    block is found at all, the entire reply is tried as a last resort, in
    case the model wrote bare code with no fence.

    Each candidate block is parsed as a real Python AST via `ast.parse()`
    and walked for `Call` nodes whose function is a bare `Name` matching
    `_CODE_MODE_INVOKE_NAME` -- this means the model is free to write
    completely ordinary Python around the call(s) (comments, loops,
    multiple calls, whatever) and they'll still be found, unlike a naive
    regex over the raw text. A block that isn't valid Python at all, or an
    `invoke_capability(...)` call whose arguments aren't literal enough for
    `ast.literal_eval` (see `_extract_invoke_args`), is skipped rather than
    raised -- same "degrade to no call, don't crash" policy as
    `_extract_tool_calls`.

    Returns `(remaining_text, tool_calls)` in the same shape as
    `_extract_tool_calls` for symmetry, though `remaining_text` is unused by
    every current caller (same as it already effectively was for the
    "action_request" convention -- see `_run_tool_call_turn`'s callers,
    which always fall back to the RAW reply text, not a stripped version,
    whenever no calls were found)."""
    tool_calls = []
    blocks = _CODE_FENCE_RE.findall(reply_text) or [reply_text]

    for block in blocks:
        try:
            tree = ast.parse(block)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == _CODE_MODE_INVOKE_NAME
            ):
                continue
            try:
                name, arguments = _extract_invoke_args(node)
            except ValueError as e:
                logging.warning(
                    "model emitted an %s(...) call this proxy couldn't parse "
                    "the arguments of, skipping it: %r",
                    _CODE_MODE_INVOKE_NAME,
                    e,
                )
                continue
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "name": name,
                    "arguments_json": json.dumps(arguments),
                }
            )

    return reply_text, tool_calls


_TOOL_CALL_MAX_ATTEMPTS = 3
_TOOL_CALL_MODES = ("code", "action_request", "code")


def _looks_like_throttled_empty_reply(text):
    """Returns True if `text` is empty or whitespace-only.

    A completely empty reply from Sydney is NOT a normal "the model chose
    to say nothing" outcome to treat as a valid (if boring) answer -- live
    testing during development found this is what Sydney silently returns
    once Microsoft-side throttling kicks in after a burst of requests in a
    short window: no explicit error, no AuthError message, the Chathub
    turn just completes normally (`Chathub reply complete (total_length=0
    chars)` in this proxy's own log) with zero content, and this happens
    for a *plain* chat turn just as much as a tool-calling one -- it is not
    specific to anything this proxy does with `tools`. It was reproduced
    reliably after roughly 40-50 requests in a few minutes during this
    proxy's own tool-calling emulation A/B testing, and did not clear after
    45 seconds, only after a few minutes (see REVERSE_ENGINEERING.md's
    "Sydney-side request throttling" section for the exact timeline).

    The Chathub protocol's own `throttling` field (now parsed and logged --
    see `_throttling_summary`) turns out NOT to be related, which is a
    genuinely useful negative result rather than a gap. Live measurement
    (2026-08-02) found it reports a PER-CONVERSATION user-message count
    against a cap of 600, and that it resets to 1 on every new
    `ConversationId`. So it cannot detect this condition even in principle:
    the empty-reply throttle fires after ~40-50 requests spread across many
    short conversations, where this counter never climbs past a handful
    before resetting. The two limits are simply different -- this one is
    per-conversation and effectively unreachable in normal use; the one that
    actually bites is account-level and is not exposed on the wire at all.
    Detection therefore stays reactive (an empty reply), and this function
    remains the only signal for it.

    Treating this as a normal empty answer would silently confuse whoever's
    on the other end of the API -- a coding agent has no way to distinguish
    "the model genuinely had nothing to say" from "you're being throttled,
    back off and retry later" if both come back as an ordinary 200 with
    empty content. See this function's callers for how it's surfaced
    instead (a clear error rather than a silent empty success)."""
    return not text.strip()


#: The one user-facing message for the empty-reply throttle shape above --
#: shared by every surface point (non-streaming 429 body, streaming SSE
#: error event) so clients see the same text no matter which path hit it.
_THROTTLED_EMPTY_MSG = (
    "Sydney returned a completely empty reply. This usually means Microsoft "
    "is temporarily throttling this account after a burst of requests -- "
    "wait a bit (a minute or more) and try again."
)


def _extract_calls_for_mode(mode, text):
    """Dispatches to the extractor matching `mode` (see _TOOL_CALL_MODES) --
    keeps _run_tool_call_turn's retry loop mode-agnostic."""
    if mode == "code":
        return _extract_code_mode_calls(text)
    return _extract_tool_calls(text)


def _openai_tool_call_entries(tool_calls, with_index=False):
    """Renders extracted tool calls (`{"id", "name", "arguments_json"}`
    dicts, see the extractors above) as OpenAI response `tool_calls`
    entries. `with_index=True` adds the `index` key that streaming chunk
    deltas carry but full responses don't -- the only difference between
    the two response shapes."""
    entries = []
    for i, tc in enumerate(tool_calls):
        entry = {
            "id": tc["id"],
            "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments_json"]},
        }
        if with_index:
            entry = {"index": i, **entry}
        entries.append(entry)
    return entries


def _run_tool_call_turn(token_cache, plan, max_attempts=_TOOL_CALL_MAX_ATTEMPTS):
    """Runs one Chathub turn per attempt (up to `max_attempts`), each as a
    brand-new, independent Chathub turn (Sydney has no memory to preserve
    across a retry anyway -- see `_render_conversation_prompt`'s docstring),
    retrying if the reply doesn't contain a parseable tool call. Unlike a
    single fixed convention, each attempt's PROMPT is rendered fresh from
    `messages`/`tools`/`tool_choice` using whichever convention
    `_TOOL_CALL_MODES` assigns that attempt number (cycling through "code"
    and "action_request" -- see that tuple's definition and
    REVERSE_ENGINEERING.md's "Code-mode tool-calling emulation" section for
    why trying more than one independent convention, rather than retrying
    the same one, meaningfully raises the effective success rate): a prompt
    that fails to get a parseable call under one convention on one attempt
    may well succeed under the other on the very next attempt, since the
    two conventions fail in different, largely uncorrelated ways (Sydney's
    own code interpreter self-preempting vs. a flat refusal).

    This retry-and-vary-convention design exists because whether the
    underlying model actually follows either of this proxy's tool-calling
    conventions on any given turn is genuinely probabilistic, not
    deterministic -- see REVERSE_ENGINEERING.md's "Tool-calling emulation"
    and "Code-mode tool-calling emulation" sections for the exact trial data
    driving this design, including why a single attempt (or a single fixed
    convention retried unchanged) was not judged an acceptable design.

    Returns `(text, tool_calls, conversation_id, turn_count)`: the reply text,
    the parsed tool calls (empty if none landed), the Sydney `ConversationId`
    the returned reply actually lives on, and how many turns deep that
    conversation now is -- so the caller can remember it for
    ConversationSessionStore continuity. Falls back to the LAST attempt's
    plain text (finish_reason "stop") if no attempt produced a call, rather
    than nothing at all.

    When `plan` is a continuation (see `_match_continuation`) this first tries
    a SINGLE delta turn on the reused conversation with NO schema re-injected
    -- relying on Sydney's own memory of the schema from the conversation's
    first turn (`_render_continuation_delta` sends only a compact reminder of
    the convention). If that yields a parseable call it's returned at once; if
    it produces no parseable call, or the Chathub turn errors, this falls back
    to the ordinary fresh path below: a brand-new, fully-rendered
    (schema-reinjected) turn under a fresh conversation, tried across
    multiple attempts/conventions. So a tools continuation can only save a
    round-trip, never lose an answer the fresh path would have produced -- the
    matched session was already popped by `_match_continuation`, so a failed
    continuation leaves nothing stale behind."""
    if plan.is_continuation:
        mode = _TOOL_CALL_MODES[0]  # "code" -- matches _plan_chat_turn's render
        logging.info(
            "tool-call continuation on reused conversation_id=%s (turn=%d): "
            "sending delta (delta_length=%d chars), no schema re-injected",
            plan.conversation_id,
            plan.turn_count,
            len(plan.prompt),
        )
        try:
            text = "".join(
                run_chat_turn(
                    token_cache,
                    plan.prompt,
                    conversation_id=plan.conversation_id,
                    tool_mode=mode,
                )
            )
            _, tool_calls = _extract_calls_for_mode(mode, text)
            if tool_calls:
                logging.info(
                    "tool-call continuation succeeded on conversation_id=%s",
                    plan.conversation_id,
                )
                return text, tool_calls, plan.conversation_id, plan.turn_count
            logging.info(
                "tool-call continuation on conversation_id=%s produced no "
                "parseable call -- falling back to a fresh, schema-reinjected turn",
                plan.conversation_id,
            )
        except Exception:
            logging.warning(
                "tool-call continuation turn failed (conversation_id=%s) -- "
                "falling back to a fresh, schema-reinjected turn",
                plan.conversation_id,
                exc_info=True,
            )

    text, tool_calls = "", []
    conversation_id = None
    for attempt in range(1, max_attempts + 1):
        mode = _TOOL_CALL_MODES[(attempt - 1) % len(_TOOL_CALL_MODES)]
        prompt = _render_conversation_prompt(
            plan.messages, tools=plan.tools, tool_choice=plan.tool_choice, mode=mode
        )
        # Each attempt is a brand-new, independent Chathub conversation (Sydney
        # has no memory to preserve across a retry) -- an explicit fresh id per
        # attempt so the SUCCESSFUL one can be remembered for later continuation.
        conversation_id = str(uuid.uuid4())
        logging.info(
            "tool-call emulation attempt %d/%d: mode=%s rendered_prompt_length=%d chars",
            attempt,
            max_attempts,
            mode,
            len(prompt),
        )
        text = "".join(
            run_chat_turn(
                token_cache, prompt, conversation_id=conversation_id, tool_mode=mode
            )
        )
        _, tool_calls = _extract_calls_for_mode(mode, text)
        if tool_calls:
            if attempt > 1:
                logging.info(
                    "tool-call emulation succeeded on retry attempt %d/%d (mode=%s)",
                    attempt,
                    max_attempts,
                    mode,
                )
            return text, tool_calls, conversation_id, 1
        logging.info(
            "tool-call emulation attempt %d/%d (mode=%s) produced no call%s",
            attempt,
            max_attempts,
            mode,
            ""
            if attempt < max_attempts
            else " -- giving up, falling back to plain content",
        )
    return text, tool_calls, conversation_id, 1


def _render_conversation_prompt(
    messages, tools=None, tool_choice=None, mode="action_request"
):
    """Renders an OpenAI `messages[]` array (optionally plus a `tools`
    schema list, `tool_choice`, and a tool-calling `mode` -- "action_request"
    or "code", see the module-level comment above `_render_tools_block`)
    into the single text blob that becomes one Chathub turn's `message.text`.

    Sydney/Chathub has no native `messages` concept: one Chathub turn is a
    single freeform text string. This function renders a FRESH conversation
    turn -- the whole running transcript, context-stuffed into one blob --
    for whichever call ends up minting a brand-new Chathub `ConversationId`:
    either the genuine first turn of a conversation, or any turn
    ConversationSessionStore didn't recognize as a continuation (see that
    class and the "Sydney-native conversation continuity" section comment
    above `_conversation_fingerprint` for when Sydney's own server-side
    memory is used instead -- confirmed live to work, see
    REVERSE_ENGINEERING.md -- via `_render_continuation_delta`, which sends
    only the new message(s) past the matched prefix rather than this
    function's full-transcript rendering).

    Context-stuffing here is still what many bridges use for backends with
    no native `system` role or message history, and is still exactly what
    makes a stateless-per-request client's full growing conversation (e.g.
    a tool-calling coding agent's follow-up request after a tool result)
    work through this proxy on a cache miss: from Sydney's point of view
    that call is still a single fresh one-shot turn, but the turn now
    contains everything the client considers "the conversation so far."

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
    section for the exact trial transcripts. The exact wording of that fold
    differs by `mode` (see `_fold_pending` below) so it stays consistent
    with whichever convention this turn is using. If `tools` is given, the
    instructions matching `mode` are appended to the system/instructions
    section.

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
        assistant history turn. Phrased to match `mode` so a "code"-mode
        turn reads as a continuation of that turn's own code-execution
        narrative rather than an unrelated JSON-request framing."""
        nonlocal pending_results
        if not pending_results:
            return text
        if mode == "code":
            preamble = "\n".join(
                f"({_CODE_MODE_INVOKE_NAME}('{n}', ...) returned: {t})"
                for n, t in pending_results
            )
        else:
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

    tools_block = None
    if tools:
        tools_block = (
            _render_tools_block_code_mode(tools, tool_choice)
            if mode == "code"
            else _render_tools_block(tools, tool_choice)
        )

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
    if tools_block:
        # Repeat a SHORT version of the instructions block immediately before
        # the final message, in addition to the full version placed earlier
        # (see `tools_block` above). This is a deliberate recency-bias
        # mitigation, not redundancy for its own sake: real coding-agent
        # system prompts (OpenCode's, OpenHands') run 30-60KB+ once their
        # full tool schema list is included, which puts a lot of unrelated
        # content between the original reminder and the point where the
        # model actually starts generating -- live-tested during development
        # to correlate with the convention being followed far less reliably
        # at that scale than in this proxy's small (~2KB) test prompts (see
        # REVERSE_ENGINEERING.md's "Tool-calling emulation at production
        # scale" section). Kept short on purpose so it doesn't itself become
        # a wall of buried text. Worded to match `mode`.
        if mode == "code":
            lines.append(
                "### Reminder before you reply\n"
                "If you need one of the capabilities above, write and run a "
                f"short Python snippet that calls {_CODE_MODE_INVOKE_NAME}"
                "(name, arguments) and reads the result, exactly as described "
                "above. Otherwise, just answer normally in plain text."
            )
        else:
            lines.append(
                "### Reminder before you reply\n"
                "You have no internet access or code execution this turn. If you "
                "need information or to take an action, reply with ONLY one or "
                f"more {_TOOL_CALL_OPEN}...{_TOOL_CALL_CLOSE} blocks as described "
                "above and nothing else. Otherwise, reply normally in plain text."
            )
        lines.append("")
    lines.append("### Respond to this message")
    lines.append(f"{last_label}: {last_text}")
    return "\n".join(lines)


class _ChatTurnPlan:
    """Everything needed to run one Chathub turn for a `/v1/chat/completions`
    request and then update the ConversationSessionStore afterwards --
    built once by `_plan_chat_turn()` and used by both `_handle_full` and
    `_handle_streaming`."""

    __slots__ = (
        "conversation_id",
        "delta_messages",
        "images",
        "is_continuation",
        "lookup_fingerprint",
        "messages",
        "oid",
        "prompt",
        "should_track",
        "tool_choice",
        "tools",
        "turn_count",
    )

    def __init__(
        self,
        prompt,
        conversation_id,
        is_continuation,
        lookup_fingerprint,
        should_track,
        messages,
        tools,
        tool_choice,
        oid,
        turn_count,
        delta_messages=None,
        images=None,
    ):
        self.prompt = prompt
        self.conversation_id = conversation_id
        self.is_continuation = is_continuation
        self.lookup_fingerprint = lookup_fingerprint
        self.should_track = should_track
        self.messages = messages
        self.tools = tools
        self.tool_choice = tool_choice
        self.oid = oid
        #: Input images (see `_extract_message_images`) attached to the latest
        #: user turn, uploaded and referenced when the turn runs; None/empty
        #: for an ordinary text turn.
        self.images = images
        #: The new-messages slice (leading assistant turns already removed)
        #: this continuation will send as its delta, or None for a fresh turn.
        #: Kept so the tools path can re-render the delta per convention `mode`
        #: without recomputing the prefix match. See `_match_continuation`.
        self.delta_messages = delta_messages
        #: turn_count to `remember()` this conversation under if this turn
        #: succeeds -- i.e. how many turns deep the reused Sydney
        #: `ConversationId` will be AFTER this one, purely for logging (see
        #: ConversationSession.turn_count).
        self.turn_count = turn_count


def _fresh_conversation_turn(messages, tools, tool_choice):
    """Builds the prompt + brand-new Sydney `ConversationId` for an ordinary,
    non-continuation turn -- the pre-existing behavior (context-stuff the
    whole `messages[]` array into one turn under a fresh conversation).
    Returns `(prompt, conversation_id)`, or `(None, None)` if there's no
    usable text in `messages` at all."""
    prompt = _render_conversation_prompt(messages, tools=tools, tool_choice=tool_choice)
    if prompt is None:
        return None, None
    return prompt, str(uuid.uuid4())


def _plan_chat_turn(token_cache, messages, tools, tool_choice, conversation_sessions):
    """Builds a `_ChatTurnPlan` for one `/v1/chat/completions` request:
    recognizes a Sydney-native continuation when possible (see
    ConversationSessionStore), else falls back to the pre-existing
    context-stuffed, brand-new-conversation behavior. Returns None if
    there's no usable text in `messages` at all.

    `conversation_sessions` is None when continuity is disabled
    (--disable-conversation-continuity) -- every request then takes the
    fresh-conversation path, identical to this proxy's behavior before this
    feature existed.

    Continuation is attempted for requests WITH `tools` too (see the section
    comment above `_conversation_fingerprint`): the tools path reuses Sydney's
    conversation for a single delta turn and falls back to a fresh,
    schema-reinjected turn if that doesn't land -- so lifting the old
    tools-exclusion can only ever save round-trips, never change what a
    request can produce."""
    should_track = conversation_sessions is not None
    oid = token_cache.get().oid if should_track else None

    # Input images belong to the latest user turn regardless of whether this
    # request is recognized as a continuation, so extract them once up front.
    images = _extract_message_images(messages)
    if images and tools:
        # The emulated tool-calling path runs its own multi-attempt turns and
        # does not thread image attachments; a request mixing images with
        # `tools` gets the tool-calling behavior, images ignored. Vision input
        # is supported on ordinary (non-tools) chat turns.
        logging.warning(
            "request carries %d input image(s) AND tools; input images are "
            "only attached on non-tools chat turns and will be ignored here",
            len(images),
        )
        images = []

    if should_track and len(messages) >= 2:
        match = _match_continuation(conversation_sessions, messages, oid)
        if match is not None:
            session, lookup_fingerprint, delta_messages = match
            # Tools continuations render/parse in "code" mode (the first entry
            # of _TOOL_CALL_MODES); plain chat needs no convention wording.
            mode = _TOOL_CALL_MODES[0] if tools else "action_request"
            delta = _render_continuation_delta(
                delta_messages, tools=tools, tool_choice=tool_choice, mode=mode
            )
            if delta is not None:
                logging.info(
                    "recognized as a Sydney-native continuation "
                    "(conversation_id=%s, turn=%d) -- sending only the %d new "
                    "message(s) (delta_length=%d chars, tools=%d) instead of "
                    "context-stuffing",
                    session.conversation_id,
                    session.turn_count + 1,
                    len(delta_messages),
                    len(delta),
                    len(tools or []),
                )
                return _ChatTurnPlan(
                    prompt=delta,
                    conversation_id=session.conversation_id,
                    is_continuation=True,
                    lookup_fingerprint=lookup_fingerprint,
                    should_track=True,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    oid=oid,
                    turn_count=session.turn_count + 1,
                    delta_messages=delta_messages,
                    images=images,
                )

    prompt, conversation_id = _fresh_conversation_turn(messages, tools, tool_choice)
    if prompt is None:
        return None
    return _ChatTurnPlan(
        prompt=prompt,
        conversation_id=conversation_id,
        is_continuation=False,
        lookup_fingerprint=None,
        should_track=should_track,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        oid=oid,
        turn_count=1,
        images=images,
    )


# ==============================================================================
# MCP (Model Context Protocol) layer -- a second, native way to reach the same
# Copilot backend, served at POST /mcp over the "Streamable HTTP" transport.
#
# Same design contract as the OpenAI layer above: NO per-request auth (all the
# Microsoft auth is handled internally from the configured credential), bind to
# localhost. On by default; turn off with --disable-mcp.
#
# This implements the minimal spec-compliant subset an MCP client needs to
# discover and call tools: the `initialize` handshake, `tools/list`,
# `tools/call`, and `ping`, plus the `notifications/*` a client sends (which
# take no response). It is stateless -- each POST is a self-contained JSON-RPC
# message, no session id is issued -- and returns a single `application/json`
# JSON-RPC response (the transport also permits an SSE stream, which this
# server does not need since it never initiates messages). Hand-rolled on the
# stdlib http.server like everything else here; no MCP SDK, per the
# single-file/stdlib-only rule (see AGENTS.md).
# ==============================================================================

#: Protocol revision advertised in the `initialize` result when the client
#: doesn't name one. The handshake echoes the client's requested version when
#: it sends a string, which is what every current MCP client does.
MCP_PROTOCOL_VERSION = "2025-06-18"

MCP_SERVER_INFO = {"name": "m365-copilot-proxy", "version": PROXY_VERSION}

#: One-line human hints surfaced to an MCP client in the initialize result.
MCP_INSTRUCTIONS = (
    "Tools that relay to Microsoft 365 Copilot (Sydney). `ask_copilot` asks a "
    "question and returns Copilot's answer (it can use web search and a Python "
    "code interpreter on its side). `describe_image` asks about an image you "
    "pass as a data: URI. `generate_image` turns a text description into a "
    "generated image, returned as an image content block."
)


def _mcp_tool_definitions():
    """The `tools/list` payload: each tool's name, description, and JSON-Schema
    input contract. Kept as a function (not a constant) so `PROXY_VERSION` and
    the caps stay in one place and it's trivially testable.

    TWO THINGS DELIBERATELY ABSENT, both of which look like obvious additions
    and are not:

    1. **No `tools` parameter.** Exposing this proxy's `/v1` tool-calling
       emulation through an MCP tool would nest two tool loops: the MCP
       client's own model calls `ask_copilot`, which prompt-steers Sydney into
       emitting a fake tool call, which comes back as plain TEXT to a model
       that already has a real, reliable, schema-enforced tool mechanism of
       its own. The result is incoherent even before considering that the
       emulation is probabilistic (see the module docstring's tool-calling
       section). Copilot is a LEAF in the MCP role -- something the caller's
       agent consults, never a second agent runtime. Keep it that way.

    2. **No `conversation_id` -- yet.** Every call is an independent Sydney
       conversation, which each tool's own description states plainly rather
       than papering over. `/v1` infers continuity by fingerprinting the
       resent `messages[]` array (see `_match_continuation`); that has no
       equivalent here, because an MCP caller sends one prompt, not a
       transcript. The natural fix is the opposite of `/v1`'s: an EXPLICIT
       id in the schema, returned in the result and passed back next call --
       honest here precisely because MCP arguments are supplied by a model
       reading a schema, which will happily thread an opaque id through.
       Purely additive whenever it's wanted; nothing below has to change."""
    return [
        {
            "name": "ask_copilot",
            "description": (
                "Ask Microsoft 365 Copilot (Sydney) a question and get its "
                "text answer. Copilot may use its own web search and Python "
                "code interpreter to answer. Each call is an independent, "
                "stateless turn."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question or instruction for Copilot.",
                    }
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "describe_image",
            "description": (
                "Ask Microsoft 365 Copilot about an image. Provide the image "
                "as an inline data: URI (data:image/png;base64,...). Returns "
                "Copilot's text answer."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "image": {
                        "type": "string",
                        "description": (
                            "The image as a data:image/<type>;base64,<...> URI. "
                            "Remote URLs are not accepted."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "What to ask about the image. Defaults to "
                            "'What is in this image?'."
                        ),
                    },
                },
                "required": ["image"],
                "additionalProperties": False,
            },
        },
        {
            "name": "generate_image",
            "description": (
                "Ask Microsoft 365 Copilot to generate an image from a text "
                "description. Returns the image itself. Copilot may decline or "
                "answer in prose instead, which is reported as an error."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the image should depict.",
                    }
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    ]


class _MCPToolError(Exception):
    """A tool-call failed in a way the caller (the LLM) should see as tool
    output, not a transport error -- surfaced as an MCP result with
    `isError: true` rather than a JSON-RPC error."""


def _mcp_image_from_data_uri(url):
    """Validates one data: URI and returns the image dict
    `run_chat_turn`/`upload_image_to_sydney` expect, or raises `_MCPToolError`
    with a client-facing reason."""
    data, file_type = _decode_data_uri_image(url)
    if data is None:
        raise _MCPToolError(
            "the `image` argument must be an inline data:image/<type>;base64,"
            "<...> URI of a supported type (png, jpeg, gif, webp, bmp)"
        )
    if len(data) > MAX_INPUT_IMAGE_BYTES:
        raise _MCPToolError(
            f"image is {len(data)} bytes, over the {MAX_INPUT_IMAGE_BYTES}-byte limit"
        )
    return {
        "data_uri": url,
        "data": data,
        "file_type": file_type,
        "filename": f"image_1.{file_type}",
    }


def _mcp_run_tool(token_cache, name, arguments):
    """Executes one MCP tool call and returns its result as a list of MCP
    content blocks. Raises `_MCPToolError` for a bad argument (shown to the
    model as tool output) and `KeyError` for an unknown tool name (surfaced as
    a JSON-RPC error by the caller).

    Content BLOCKS rather than a plain string because `generate_image` returns
    an actual image: MCP has a first-class `{"type": "image", "data": <base64>,
    "mimeType": ...}` block, which is the whole reason image OUTPUT is offered
    here as well as on `/v1/images/generations` and not at all on
    `/v1/chat/completions`."""
    arguments = arguments or {}
    if name == "ask_copilot":
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _MCPToolError("`prompt` is required and must be a non-empty string")
        return [_mcp_text_block("".join(run_chat_turn(token_cache, prompt)))]
    if name == "describe_image":
        url = arguments.get("image")
        if not isinstance(url, str) or not url:
            raise _MCPToolError("`image` is required and must be a data: URI string")
        image = _mcp_image_from_data_uri(url)
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = "What is in this image?"
        return [
            _mcp_text_block("".join(run_chat_turn(token_cache, prompt, images=[image])))
        ]
    if name == "generate_image":
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _MCPToolError("`prompt` is required and must be a non-empty string")
        return [
            {
                "type": "image",
                "data": base64.b64encode(raw).decode("ascii"),
                "mimeType": mime,
            }
            for raw, mime in generate_image(token_cache, prompt)
        ]
    raise KeyError(name)


def _mcp_text_block(text):
    return {"type": "text", "text": text}


def make_handler(token_cache, conversation_sessions, mcp_enabled=True):
    """Builds a request handler class bound to one TokenCache (in turn bound
    to one CredentialStore) -- the single configured Microsoft identity this
    proxy instance speaks as -- and one ConversationSessionStore (or None if
    --disable-conversation-continuity was given).

    `mcp_enabled` (default True) serves the MCP endpoint at /mcp; set False
    (via --disable-mcp) to route /mcp to 404 exactly like any other path."""

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
                except Exception:  # nosec B110  # noqa: BLE001,S110 - see comment above
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
            elif path == "/mcp" and mcp_enabled:
                # Streamable HTTP allows a GET to open a server->client SSE
                # stream; this server never initiates messages, so it declines
                # with 405 + Allow: POST, exactly as the spec prescribes.
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
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
                except Exception:  # nosec B110  # noqa: BLE001,S110 - see comment above
                    pass

        def _read_json_body(self):
            """Reads the request body and parses it as JSON. Returns
            `(obj, None)` on success or `(None, error_message)` on a bad
            Content-Length or unparseable body."""
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return None, "invalid Content-Length header"
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw_body), None
            except json.JSONDecodeError:
                return None, "invalid JSON body"

        def _do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            logging.debug("POST %s from %s", path, self.address_string())
            if path == "/mcp" and mcp_enabled:
                self._handle_mcp()
                return
            if path == "/v1/images/generations":
                self._handle_images_generations()
                return
            if path != "/v1/chat/completions":
                self._error(404, "not found")
                return

            body, err = self._read_json_body()
            if err is not None:
                self._error(400, err)
                return

            messages = body.get("messages") or []
            tools = body.get("tools") or None
            tool_choice = body.get("tool_choice")
            model = body.get("model") or "m365-copilot"
            stream = bool(body.get("stream", False))

            plan = _plan_chat_turn(
                token_cache, messages, tools, tool_choice, conversation_sessions
            )
            if plan is None:
                self._error(400, "no usable message content found in 'messages'")
                return
            logging.info(
                "chat completion request from %s: %d message(s), rendered_prompt_length=%d chars, "
                "stream=%s, model=%r, tools=%d, continuation=%s",
                self.address_string(),
                len(messages),
                len(plan.prompt),
                stream,
                model,
                len(tools or []),
                plan.is_continuation,
            )
            if stream:
                self._handle_streaming(plan, model)
            else:
                self._handle_full(plan, model)

        # --- OpenAI images endpoint ----------------------------------------

        def _handle_images_generations(self):
            """`POST /v1/images/generations` -- OpenAI's own image endpoint.

            Image generation lives here rather than on `/v1/chat/completions`
            because a chat-completions response can only carry text: the
            choices there would be a URL the caller cannot authenticate to, or
            megabytes of base64 in `content`. This endpoint's response format
            IS base64 image data, so it fits exactly.

            `response_format` is accepted for compatibility but only
            `b64_json` can be honored -- returning a `url` would mean handing
            the caller a `designerapp.officeapps.live.com` link that 401s
            without a token this proxy holds and will not hand out. That
            mirrors OpenAI's own GPT-image models, which likewise only return
            `b64_json`."""
            body, err = self._read_json_body()
            if err is not None:
                self._error(400, err)
                return

            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                self._error(400, "'prompt' is required and must be a non-empty string")
                return
            response_format = body.get("response_format") or "b64_json"
            if response_format != "b64_json":
                self._error(
                    400,
                    f"response_format={response_format!r} is not supported; only "
                    "'b64_json' is (the generated file is served from a Microsoft "
                    "endpoint that requires a token this proxy does not hand out, "
                    "so a usable 'url' cannot be returned)",
                    err_type="invalid_request_error",
                )
                return
            for ignored in ("size", "quality", "style", "background"):
                if body.get(ignored) is not None:
                    logging.warning(
                        "ignoring unsupported image parameter %r -- Copilot's "
                        "image generation exposes no such control",
                        ignored,
                    )

            request_id = f"img-{uuid.uuid4().hex}"
            logging.info(
                "image generation request from %s: %s prompt_length=%d n=%s model=%r",
                self.address_string(),
                request_id,
                len(prompt),
                body.get("n"),
                body.get("model"),
            )
            try:
                images = generate_image(token_cache, prompt, n=body.get("n") or 1)
            except ThrottledError as e:
                logging.exception("image generation %s throttled", request_id)
                self._error(429, str(e), err_type="upstream_throttled")
                return
            except UnsupportedContentError as e:
                # Copilot answered without producing an image (prose, or a
                # content-policy refusal). Not a rate limit, and not a proxy
                # bug -- a different prompt may well work, so it gets its own
                # err_type rather than being lumped in with either.
                logging.info("image generation %s produced no image: %s", request_id, e)
                self._error(502, str(e), err_type="upstream_no_image")
                return
            except Exception as e:
                logging.exception("image generation %s failed", request_id)
                self._error(502, str(e))
                return

            data = []
            for raw, mime in images:
                data.append({"b64_json": base64.b64encode(raw).decode("ascii")})
            logging.info(
                "image generation %s finished successfully (images=%d)",
                request_id,
                len(data),
            )
            self._write_json(
                200,
                {
                    "created": int(time.time()),
                    "data": data,
                    # Echoing the real format back is part of OpenAI's own
                    # ImagesResponse, and its allowed values ("png"/"jpeg"/
                    # "webp") are exactly the subtype of the Content-Type the
                    # download reported. `usage` is deliberately OMITTED rather
                    # than reported as zeros: it is optional in OpenAI's schema,
                    # and this proxy does no token counting (see the module
                    # docstring's note on `usage` for chat completions, where
                    # the field is required and so has to be faked).
                    "output_format": images[0][1].split("/")[-1] or "png",
                },
            )

        # --- MCP (Streamable HTTP) endpoint ---------------------------------

        def _write_accepted(self):
            """Empty HTTP 202 for a JSON-RPC message that warrants no response
            body (a notification), as the Streamable HTTP transport prescribes."""
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        @staticmethod
        def _jsonrpc_error(msg_id, code, message):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }

        @staticmethod
        def _jsonrpc_result(msg_id, result):
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        def _handle_mcp(self):
            body, err = self._read_json_body()
            if err is not None:
                # -32700 Parse error (JSON-RPC), no id available.
                self._write_json(400, self._jsonrpc_error(None, -32700, err))
                return
            if not isinstance(body, dict):
                # Batched requests (a JSON array) were removed in MCP
                # 2025-06-18 and this server never supported them.
                self._write_json(
                    400,
                    self._jsonrpc_error(
                        None, -32600, "expected a single JSON-RPC object"
                    ),
                )
                return

            method = body.get("method")
            msg_id = body.get("id")
            is_notification = "id" not in body
            logging.info(
                "MCP request from %s: method=%s notification=%s",
                self.address_string(),
                method,
                is_notification,
            )

            # Notifications (no id) -- client->server one-way messages such as
            # notifications/initialized -- get a bare 202 and no JSON-RPC body.
            if is_notification:
                self._write_accepted()
                return

            if method == "initialize":
                requested = body.get("params", {}).get("protocolVersion")
                self._write_json(
                    200,
                    self._jsonrpc_result(
                        msg_id,
                        {
                            "protocolVersion": requested
                            if isinstance(requested, str)
                            else MCP_PROTOCOL_VERSION,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": MCP_SERVER_INFO,
                            "instructions": MCP_INSTRUCTIONS,
                        },
                    ),
                )
                return
            if method == "ping":
                self._write_json(200, self._jsonrpc_result(msg_id, {}))
                return
            if method == "tools/list":
                self._write_json(
                    200,
                    self._jsonrpc_result(msg_id, {"tools": _mcp_tool_definitions()}),
                )
                return
            if method == "tools/call":
                self._handle_mcp_tools_call(msg_id, body.get("params") or {})
                return

            self._write_json(
                200,
                self._jsonrpc_error(msg_id, -32601, f"method not found: {method}"),
            )

        def _handle_mcp_tools_call(self, msg_id, params):
            name = params.get("name")
            arguments = params.get("arguments") or {}
            completion_id = uuid.uuid4().hex
            logging.info("MCP tools/call %s name=%r", completion_id, name)
            try:
                content = _mcp_run_tool(token_cache, name, arguments)
            except KeyError:
                self._write_json(
                    200,
                    self._jsonrpc_error(msg_id, -32602, f"unknown tool: {name}"),
                )
                return
            except _MCPToolError as e:
                # A bad argument the model can correct -- an isError result, not
                # a transport error, so the tool text reaches the model.
                self._write_json(
                    200,
                    self._jsonrpc_result(
                        msg_id, self._mcp_tool_text(str(e), is_error=True)
                    ),
                )
                return
            except (ThrottledError, UnsupportedContentError) as e:
                logging.info("MCP tools/call %s upstream refusal: %s", completion_id, e)
                self._write_json(
                    200,
                    self._jsonrpc_result(
                        msg_id, self._mcp_tool_text(str(e), is_error=True)
                    ),
                )
                return
            except Exception as e:
                logging.exception("MCP tools/call %s failed", completion_id)
                self._write_json(
                    200,
                    self._jsonrpc_result(
                        msg_id, self._mcp_tool_text(f"tool failed: {e}", is_error=True)
                    ),
                )
                return

            text = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
            non_text = [b for b in content if b.get("type") != "text"]
            # The empty-reply throttle signature only applies to a turn that was
            # supposed to answer in TEXT. A `generate_image` result is all image
            # blocks and no text at all, which is a complete success -- checking
            # it here would report every generated image as throttling, exactly
            # the bug this release fixes on the chat path.
            if not non_text and _looks_like_throttled_empty_reply(text):
                logging.error(
                    "MCP tools/call %s got a completely empty reply -- likely "
                    "Microsoft-side throttling",
                    completion_id,
                )
                self._write_json(
                    200,
                    self._jsonrpc_result(
                        msg_id, self._mcp_tool_text(_THROTTLED_EMPTY_MSG, is_error=True)
                    ),
                )
                return

            logging.info(
                "MCP tools/call %s finished (text_length=%d chars, non_text_blocks=%d)",
                completion_id,
                len(text),
                len(non_text),
            )
            self._write_json(
                200,
                self._jsonrpc_result(msg_id, {"content": content, "isError": False}),
            )

        @staticmethod
        def _mcp_tool_text(text, is_error=False):
            """An MCP `tools/call` result carrying a single text content block."""
            return {"content": [_mcp_text_block(text)], "isError": is_error}

        def _remember_turn(self, plan):
            """Shared tail of the plain and tool-call turn runners: if `plan`
            is tracked, remembers the completed turn's Sydney conversation
            under a fingerprint of the exact `messages[]` array the client
            sent THIS turn, so the client's next call -- which resends this
            array verbatim as a prefix and appends its own rendering of the
            reply plus new input -- is recognized as a continuation by
            `_match_continuation`.

            Keying on the client's INPUT (not `messages + [our reply]`, as an
            earlier design did) is what lets prefix matching tolerate the
            client re-serializing the assistant turn differently than this
            proxy emitted it: the fingerprint never depends on the reply's
            wording at all, only on what the client itself resends."""
            if not plan.should_track:
                return
            new_fingerprint = _conversation_fingerprint(plan.messages)
            conversation_sessions.remember(
                new_fingerprint, plan.conversation_id, plan.oid, plan.turn_count
            )

        def _run_plain_turn(self, plan):
            """Runs one plain-text (no `tools`) Chathub turn per `plan`,
            with ConversationSessionStore bookkeeping: on success, remembers
            the resulting conversation state under a fingerprint of
            `messages + [this reply]` so the client's next call can be
            recognized as a continuation. If `plan` WAS a continuation and
            the Chathub turn failed, forgets the now-suspect cached session
            and retries once as a brand-new, fully context-stuffed
            conversation before giving up -- safe here (unlike the
            streaming path below) because nothing has been written to the
            HTTP response yet at this point."""
            try:
                text = "".join(
                    run_chat_turn(
                        token_cache,
                        plan.prompt,
                        conversation_id=plan.conversation_id,
                        images=plan.images,
                    )
                )
            except (ThrottledError, UnsupportedContentError):
                # Neither of these is a problem with this conversation_id, so
                # the retry-as-a-fresh-conversation path below would just burn
                # a second doomed Chathub call:
                #   - ThrottledError: Sydney is over capacity right now, and a
                #     fresh conversation would not be any less throttled.
                #   - UnsupportedContentError: the turn produced an image and
                #     no text; asking again in a new conversation produces the
                #     same image and the same absence of text.
                # Forget the session regardless (harmless either way) and
                # propagate so the client sees the real reason, same as a
                # non-continuation turn always has.
                if plan.is_continuation:
                    conversation_sessions.forget(plan.lookup_fingerprint)
                raise
            except Exception:
                if not plan.is_continuation:
                    raise
                logging.warning(
                    "Sydney-native continuation turn failed (conversation_id=%s) "
                    "-- forgetting the cached session and retrying once as a "
                    "brand-new conversation",
                    plan.conversation_id,
                    exc_info=True,
                )
                conversation_sessions.forget(plan.lookup_fingerprint)
                fresh_prompt, fresh_conversation_id = _fresh_conversation_turn(
                    plan.messages, plan.tools, plan.tool_choice
                )
                if fresh_prompt is None:
                    raise
                plan.prompt = fresh_prompt
                plan.conversation_id = fresh_conversation_id
                plan.is_continuation = False
                plan.turn_count = 1
                text = "".join(
                    run_chat_turn(
                        token_cache,
                        plan.prompt,
                        conversation_id=plan.conversation_id,
                        images=plan.images,
                    )
                )

            self._remember_turn(plan)
            return text

        def _stream_plain_turn(self, plan):
            """Generator equivalent of `_run_plain_turn` for the streaming
            path: yields text deltas, doing the same success-path
            bookkeeping, but -- unlike `_run_plain_turn` -- does NOT retry a
            failed continuation turn in place. Once a delta has been
            flushed to the client there is no safe way to restart a fresh
            conversation without duplicating output, so a broken
            continuation is only forgotten here (so the client's own next
            request, resending the same transcript, gets a clean cache miss
            and a fresh conversation) rather than retried."""
            parts = []
            try:
                for delta in run_chat_turn(
                    token_cache,
                    plan.prompt,
                    conversation_id=plan.conversation_id,
                    images=plan.images,
                ):
                    parts.append(delta)
                    yield delta
            except Exception:
                if plan.is_continuation:
                    logging.warning(
                        "Sydney-native continuation turn failed mid-stream "
                        "(conversation_id=%s) -- forgetting the cached session "
                        "so the next request starts fresh",
                        plan.conversation_id,
                    )
                    conversation_sessions.forget(plan.lookup_fingerprint)
                raise

            self._remember_turn(plan)

        def _handle_streaming(self, plan, model):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())
            tools_requested = bool(plan.tools)

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
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()

            try:
                if tools_requested:
                    # Buffered mode: whether this reply is plain content or a
                    # tool call can only be known once the FULL text has been
                    # seen (see _extract_tool_calls/_extract_code_mode_calls),
                    # so there is no true incremental streaming when `tools`
                    # is present -- the whole reply is collected first, then
                    # emitted as one chunk. Documented as a known limitation.
                    # See _run_tool_call_turn for why this retries internally
                    # (and across more than one tool-calling convention).
                    text, tool_calls, conv_id, turn_count = _run_tool_call_turn(
                        token_cache, plan
                    )
                    # Reflect whichever conversation actually produced this
                    # reply (reused on a continuation, fresh otherwise) back
                    # onto the plan so _remember_turn tracks the right id.
                    plan.conversation_id = conv_id
                    plan.turn_count = turn_count
                    self._remember_turn(plan)
                    if tool_calls:
                        # Leftover text is deliberately dropped here, not
                        # surfaced as delta content -- see _handle_full's
                        # comment on the same point for why.
                        emit(
                            {
                                "role": "assistant",
                                "tool_calls": _openai_tool_call_entries(
                                    tool_calls, with_index=True
                                ),
                            }
                        )
                        finish_reason = "tool_calls"
                    elif _looks_like_throttled_empty_reply(text):
                        # See _looks_like_throttled_empty_reply's docstring:
                        # raising here (rather than emitting a fake
                        # successful empty completion) routes this into the
                        # `except Exception` handler below, which surfaces
                        # it as a proper SSE error event (type
                        # `upstream_throttled`) instead of a silent
                        # "the assistant said nothing" success.
                        raise ThrottledError(_THROTTLED_EMPTY_MSG)
                    else:
                        emit({"role": "assistant", "content": text})
                        finish_reason = "stop"
                else:
                    first = True
                    for delta in self._stream_plain_turn(plan):
                        emit(
                            {"role": "assistant", "content": delta}
                            if first
                            else {"content": delta}
                        )
                        first = False
                    if first:
                        # No delta was ever yielded at all -- see
                        # _looks_like_throttled_empty_reply's docstring for
                        # why this is treated as an error, not a valid
                        # (if terse) empty answer.
                        raise ThrottledError(_THROTTLED_EMPTY_MSG)
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
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
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
                # UnsupportedContentError is checked FIRST: it is a
                # ProtocolError like ThrottledError, and mixing the two up in
                # this direction is exactly the bug being fixed (an image-only
                # turn reported as "back off and retry").
                if isinstance(e, UnsupportedContentError):
                    err_type = "unsupported_upstream_content"
                elif isinstance(e, ThrottledError):
                    err_type = "upstream_throttled"
                else:
                    err_type = "proxy_error"
                err = {"error": {"message": str(e), "type": err_type}}
                try:
                    self.wfile.write(f"data: {json.dumps(err)}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    pass

        def _handle_full(self, plan, model):
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            tools_requested = bool(plan.tools)
            try:
                if tools_requested:
                    text, tool_calls, conv_id, turn_count = _run_tool_call_turn(
                        token_cache, plan
                    )
                    # Reflect whichever conversation actually produced this
                    # reply (reused on a continuation, fresh otherwise) back
                    # onto the plan so _remember_turn tracks the right id.
                    plan.conversation_id = conv_id
                    plan.turn_count = turn_count
                    self._remember_turn(plan)
                else:
                    text, tool_calls = self._run_plain_turn(plan), []
            except ThrottledError as e:
                # Sydney explicitly refused the turn with its own rate-limit
                # message -- surface it as 429 (Too Many Requests), the code
                # OpenAI clients/SDKs already know to back off and retry on,
                # matching the empty-reply throttle detection below.
                logging.exception("chat completion %s throttled", completion_id)
                self._error(429, str(e), err_type="upstream_throttled")
                return
            except UnsupportedContentError as e:
                # NOT 429: this is the case that used to be misreported as
                # throttling, telling callers to back off and retry when
                # retrying can never work. 502 with a distinct err_type so a
                # client can tell "the upstream produced something I can't
                # represent" apart from "slow down".
                logging.exception(
                    "chat completion %s produced only non-text content",
                    completion_id,
                )
                self._error(502, str(e), err_type="unsupported_upstream_content")
                return
            except Exception as e:
                # See the comment in _handle_streaming's except clause: broad
                # on purpose, so any bug still produces a diagnosable log entry.
                logging.exception("chat completion %s failed", completion_id)
                self._error(502, str(e))
                return

            if not tool_calls and _looks_like_throttled_empty_reply(text):
                # See _looks_like_throttled_empty_reply's docstring: a
                # completely empty reply is surfaced as an error, not a
                # silent 200-with-empty-content "success" that would leave
                # the caller unable to tell "the model said nothing" apart
                # from "you're being throttled, back off and retry later".
                logging.error(
                    "chat completion %s got a completely empty reply from "
                    "Sydney -- likely Microsoft-side throttling after a "
                    "burst of requests, surfacing as an error",
                    completion_id,
                )
                self._error(429, _THROTTLED_EMPTY_MSG, err_type="upstream_throttled")
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
                    "tool_calls": _openai_tool_call_entries(tool_calls),
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
        "args: host=%s port=%d credentials_prefix=%s log_file=%s log_level=%s "
        "init_credentials=%s disable_conversation_continuity=%s disable_mcp=%s",
        args.host,
        args.port,
        args.credentials_prefix,
        args.log_file,
        args.log_level,
        args.init_credentials,
        args.disable_conversation_continuity,
        args.disable_mcp,
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
        "--port", type=int, default=8001, help="bind port (default: 8001)"
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
    parser.add_argument(
        "--disable-conversation-continuity",
        action="store_true",
        help="always context-stuff the whole conversation into a brand-new Sydney "
        "conversation on every call, never reusing Sydney's own server-side "
        "conversation memory -- this proxy's original behavior. Continuity is "
        "enabled by default (see module docstring's 'Sydney-native conversation "
        "continuity' section); this is an escape hatch if it ever causes trouble",
    )
    parser.add_argument(
        "--disable-mcp",
        action="store_true",
        help="do not serve the MCP endpoint at /mcp. The Model Context Protocol "
        "endpoint (Streamable HTTP) is served on the same host/port by default, "
        "alongside the OpenAI-compatible /v1 API; pass this to turn it off",
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

    conversation_sessions = (
        None if args.disable_conversation_continuity else ConversationSessionStore()
    )
    mcp_enabled = not args.disable_mcp
    handler_cls = make_handler(
        token_cache, conversation_sessions, mcp_enabled=mcp_enabled
    )
    server = _LoggingHTTPServer((args.host, args.port), handler_cls)
    logging.info(
        "listening on http://%s:%d (Ctrl+C to stop) -- no auth required on the API "
        "itself; Sydney-native conversation continuity is %s; MCP endpoint at "
        "/mcp is %s",
        args.host,
        args.port,
        "disabled (--disable-conversation-continuity)"
        if conversation_sessions is None
        else "enabled",
        "enabled" if mcp_enabled else "disabled (--disable-mcp)",
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
