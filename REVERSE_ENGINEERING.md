# Reverse engineering notes: m365.cloud.microsoft chat endpoint

This document is a **chronological research log**, not a reference manual — it
records the investigation in the order it happened, including dead ends and
theories that later turned out to be wrong (each correction is called out
explicitly where it happens, e.g. the "AADSTS70000" section below). If you just
want to know what's true *right now*, read this summary and follow the links;
if you want the full provenance/reasoning behind any of it, the dated "Update:"
sections below are where that lives.

## Current state summary (read this first)

- **Auth chain**: one interactive login mints a FOCI-family (Family of Client
  IDs) refresh token good across three sibling AAD apps. The Sydney/Chathub
  access token is minted by silently redeeming that refresh token under
  `client_id=c0ab8ce9-e9a0-42e7-b064-33d422df41f1`,
  `scope=https://substrate.office.com/sydney/.default`. See "FOCI" and "Sydney"
  sections below for the full chain.
- **The refresh_token-grant request has a specific required shape** — tenant-
  specific token endpoint (not `/common/`), plus `client_id`/`client-request-id`
  in the URL and six MSAL identity/telemetry fields
  (`X-AnchorMailbox`/`x-client-SKU`/etc.) in the body. Getting this wrong makes
  Entra ID reject an otherwise-valid, non-stale refresh token with a
  generic-looking `AADSTS70000 invalid_grant` — see the "AADSTS70000" update
  near the end of this document; this was fixed in `m365_openai_proxy.py` and
  confirmed against live traffic.
- **Wire protocol**: `wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}?...&access_token={JWT}` —
  SignalR JSON Hub Protocol (`\x1e`-delimited frames). Send a `type:4`
  (StreamInvocation) `target:"chat"` with the payload documented in the
  "Chathub.log.jsonl" section below; the reply streams back as `type:1`
  `target:"update"` frames (`writeAtCursor` deltas or full-text snapshots),
  terminated by a `type:3` (Completion) frame.
- **MSAL localStorage cache encryption** (only relevant if using the
  encrypted-cache credential option instead of a plaintext refresh token): it's
  HKDF-SHA256(salt=nonce, info=clientId-or-empty) deriving a per-entry AES-256
  key, then AES-GCM with a fixed all-zero IV — **not** raw AES-GCM with the
  base key directly, which is why early brute-force attempts at this failed.
  See the "MSAL localStorage cache-encryption algorithm — SOLVED" section.
- **Multi-turn conversation**: now Sydney-native where possible, with
  context-stuffing kept as the automatic fallback — see the "Sydney-native
  conversation continuity" update near the end of this document. A live
  probe confirmed reusing a prior call's `ConversationId` on a brand-new,
  independent Chathub WebSocket connection gets a reply demonstrating real
  server-side memory of that prior call, with no history resent; a control
  turn on a fresh `ConversationId` correctly showed none. `m365_openai_proxy.py`
  now recognizes when an incoming request's `messages[]` array is an exact
  continuation of a conversation it already relayed and, in that case, sends
  only the newest message on the reused `ConversationId` instead of
  re-rendering and resending the whole growing transcript every time
  (`ConversationSessionStore`). Whether `msal.cache.encryption` is
  `HttpOnly` is still unconfirmed.
- **Sydney's own rate limiting has a failure mode this proxy didn't handle
  until now**: a throttled turn ("We're temporarily unable to respond to
  this volume of requests. Please try again later.") arrives as a `bot`
  message with `turnState: "Failed"` inside a `type:2` (StreamItem) frame —
  NOT via the normal `type:1`/`target:"update"` streaming path — so it used
  to be silently swallowed, completing as an empty but ostensibly
  successful reply. `stream_chat_reply()` now recognizes this shape and
  raises `ThrottledError` instead. Found by accident while live-testing the
  conversation-continuity feature above (see that section).
- **Tool-calling bridge (Local MCP)**: Sydney/Chathub has a real, concrete
  mechanism for invoking Model Context Protocol tools from the client side —
  `mcp_discover`/`mcp_describe`/`invoke_local_plugin` SignalR invocation
  targets on the SAME Chathub connection this project already uses, extracted
  directly from the officeweb client's own source (not guessed). See the
  "Local MCP tool-calling bridge" section near the end. **Not implemented**:
  wiring this up to look like OpenAI tool-calling for a client like OpenCode
  requires solving a real architecture mismatch (Sydney's invocation is
  synchronous and mid-turn with a server-side timeout; OpenAI tool-calling is
  asynchronous across separate HTTP requests) — see that section for the
  full writeup and why this is a real next-phase project, not a quick patch.
- **Tool-calling emulation (implemented, IS shipped, but probabilistic --
  and validated end-to-end against real CLIs, with a real asymmetry)**:
  instead of the Local MCP bridge above, `m365_openai_proxy.py` emulates
  OpenAI `tools`/`tool_calls` entirely by prompting — inject a plain-text
  convention (`<action_request>{"name":...,"arguments":{...}}
  </action_request>`), parse it back out of the reply. Live A/B testing
  found Sydney's own built-in code interpreter frequently preempts this
  convention instead of following it, and that the literal word "tool"
  ANYWHERE in the prompt (including in a client's own system prompt, which
  can't be controlled) measurably makes this worse — this proxy launders
  that word out and retries up to 3x per turn, which helps substantially
  but still tops out around a coin-flip on a single attempt even under the
  best measured conditions. **Real end-to-end testing against the actual
  OpenHands, OpenCode, and Goose CLIs** (not just synthetic curl requests)
  found: 1 of 2 full OpenHands sessions succeeded completely and verifiably
  (a real file was read and edited via real tool calls, confirmed on disk);
  0 of 3 full OpenCode sessions succeeded, even after adding a recency-bias
  mitigation and raising the retry budget to 5; 0 of 4 full Goose sessions
  succeeded, across two different tool-count configurations (18 tools →
  flat refusal; 5 tools → code-interpreter self-preemption) — see the
  "production-scale end-to-end validation" and "Goose CLI validation"
  updates near the end of this document. Treat OpenHands as demonstrated-
  but-unreliable and OpenCode/Goose as not-currently-working, not all three
  as equally "probabilistic." **This whole picture is for the PRIOR,
  single-convention design** -- see the next bullet for what changed.
- **"Code-mode" tool-calling emulation (implemented, IS shipped) — a second,
  independent convention added alongside "action_request", tried in
  alternation across retries** in direct response to the user asking for a
  much deeper investigation into making OpenCode/native-OpenHands/native-Goose
  reliable. Rather than fighting Sydney's strong, repeatedly-observed habit
  of solving things by writing and "running" Python code (which
  "action_request" mode does, by explicitly suppressing Sydney's own code
  interpreter), "code" mode leans directly into it: the model is told a
  Python function `invoke_capability(name, arguments)` is already loaded in
  its environment and is asked to call it from ordinary code, parsed back
  out via a real Python AST walk (not a regex). `_run_tool_call_turn()` now
  cycles through BOTH conventions across its retry attempts rather than
  retrying one fixed convention, since live A/B testing found neither
  dominates the other in every situation (see the "code-mode tool-calling
  emulation" section for the exact numbers: "code" mode won 6/6 vs.
  "action_request"'s 0/6 on a simple single-capability request in one
  session, but "action_request" won 4/5 vs. "code" mode's 2/5 at realistic
  coding-agent scale in the same session). **Live end-to-end re-validation
  against the real OpenCode/OpenHands/Goose CLIs with this new two-
  convention design was blocked by the Sydney-side request throttling
  discovered during this same investigation** (see two bullets below) —
  check this document's "code-mode tool-calling emulation" section for
  whether that live re-validation was completed by the time you're reading
  this, or whether it's still an open follow-up.
- **OpenHands' own client-side "mock function calling" — dramatically more
  reliable, and the recommended configuration, now deep-load-tested**:
  OpenHands' SDK has a `native_tool_calling=False` flag that converts
  tools to text and parses the reply entirely on OpenHands' own side,
  sending this proxy NO `tools` field at all (bypassing this proxy's
  emulation above completely). Initial test: 3 of 3 full sessions
  succeeded. A much larger follow-up load test (26 sessions across 7
  batches — single-edit, multi-step-with-terminal-verification,
  multi-file, concurrent sessions, and an iterative debug loop — see the
  "deep, adversarial load-testing" update near the end of this document)
  found **17 of 17 (100%) success across ordinary implement/edit/verify
  task shapes, including concurrency**, but only **1 of 9 (11%) success on
  a specific task shape**: reasoning about or fixing code containing a
  function whose name contradicts its own behavior (e.g. a `subtract` that
  adds) — which appeared to reproducibly trigger a genuinely **empty
  completion**, a third failure mode distinct from the refusal-text and
  code-interpreter-self-preemption patterns documented elsewhere in this
  file. **Important correction found immediately after this test (see the
  dated "Correction" subsection near the end of this document): the same
  account was separately confirmed to hit an explicit, Sydney-labeled
  volume-based rate limit shortly afterward, and the misleading-name
  batches were run last in the session (highest cumulative request
  count) — so this specific causal story is unconfirmed and may instead
  (or also) be volume-based throttling that was never isolated as an
  independent variable.** Treat the 17/17-vs-1/9 headline numbers as real,
  but the "misleadingly-named code is the trigger" explanation as
  retracted pending a volume-controlled retest. This flag
  isn't exposed through the OpenHands CLI's normal settings/env-var
  surface in the installed version — it requires directly seeding a
  persisted `agent_settings.json` — see the "OpenHands' own client-side
  'mock function calling'" and "deep, adversarial load-testing" updates
  near the end of this document for the exact setup and full trial data.
  Does not transfer to OpenCode (no equivalent client-side fallback exists
  at all) or to Goose (it DOES have an analogous mechanism, "Toolshim" —
  see next bullet — but it didn't
  help here, for a fully understood reason).
- **Goose's own "Toolshim" — has the same architecture as OpenHands' mock
  function calling, but doesn't help here, for a specific and now fully
  explained reason**: read directly from Goose's own Rust source
  (`GOOSE_TOOLSHIM=1`) and confirmed live: it also sends `tools: []` to the
  provider and tries to extract tool calls from the plain-text reply
  directly (Ollama/local-llama.cpp interpreter only as a last-resort
  fallback, confirmed unreachable from an arbitrary OpenAI-compatible URL
  like this proxy). The blocker: Goose's own hardcoded toolshim system-
  prompt text is baked into the binary and cannot be edited or laundered,
  and it uses the literal word "tool" five times — exactly the trigger
  already established to reliably derail Sydney into code-interpreter
  self-preemption. Tested 0 of 3 sessions succeeded, with the causal chain
  fully confirmed via Goose's own CLI log (an attempted, failed, gracefully
  -caught Ollama fallback call, `Network error: Could not connect to
  localhost:11434`) rather than just inferred — see the dated "Goose's own
  'Toolshim'" update near the end of this document.
- **Sydney-side request throttling — a genuinely new discovery**: after
  roughly 40-50 Chathub turns opened in a few minutes (during this same
  investigation's own A/B testing), Sydney started silently returning
  completely EMPTY completions (`Chathub reply complete (total_length=0
  chars)`, no error, no `AuthError`) for every request -- including plain,
  tools-free chat turns completely unrelated to tool-calling. This had
  never been observed before in this project. It did not clear within 45
  seconds, and was still present after roughly 30 minutes of testing in
  this update. `_looks_like_throttled_empty_reply()` now detects this and
  both `_handle_full`/`_handle_streaming` surface it as an explicit error
  instead of a silent, misleadingly-successful empty `200` response — see
  the "code-mode tool-calling emulation" section for the full timeline and
  why this matters directly for agentic-loop reliability (a working coding
  agent generates exactly this kind of request burst).
- **Not actually in this repo**: none of the `.har` capture files or
  `Chathub.log.jsonl` referenced throughout this document were ever committed
  (they carry live session cookies/tokens and are `.gitignore`d) — this
  document is the durable record of what they showed, not a pointer to files
  you can go re-read locally unless you made your own captures during
  development.

---

## What was actually captured

Only **one** HTTP exchange is in the HAR:

```
POST https://m365.cloud.microsoft/chat  ->  200 application/json
```

This is **not** the endpoint that actually sends a prompt to Copilot and streams back a
model response. It's the SSR/"flux" state-sync call the officeweb client fires *after* a
turn finishes, to push the updated conversation-history state to the server-rendered
store (hydration cache) and get back an authoritative merged copy. That's why the
`action` field is literally `"ConversationResponseComplete"` and the body is a giant
dump of client UI state rather than a user message.

To build a real "send a message, get a completion" client you'll need to capture the
network traffic **during** a turn (Firefox devtools → Network → filter by `Fetch/XHR`,
send a chat message, and save a HAR that spans the whole turn). Look for something like
a `/chat/...` POST that starts *before* the assistant's text appears and is followed by
either an SSE/chunked stream or repeated poll calls — that's the one this repo actually
needs. What's captured today is the tail end of that flow, not the start.

## Endpoint details for what *was* captured

`POST https://m365.cloud.microsoft/chat`

### Required headers
| Header | Purpose |
|---|---|
| `X-Route-Id: chat` | routes the request server-side |
| `x-host-context` | JSON blob: `{"clientPlatform":"web","hostName":"officeweb","appName":"SSR","appMode":"default"}` — identifies which front-end shell is calling |
| `X-Session-Id` | client-generated GUID, stable per browser session |
| `X-Client-Eligibility` | JSON blob describing the tenant's Copilot licensing/feature flags (`isCopilotEligible`, `cohort`, `featureSet.uxFeatures`, etc.) — looks like it's echoed back from an earlier bootstrap call rather than computed client-side |
| `Referer` | must be the conversation page URL, `https://m365.cloud.microsoft/chat/conversation/{conversationId}` |
| `Content-Type: application/json` | |
| `Cookie` | full auth cookie jar (see below) |

Standard browser headers (`User-Agent`, `Accept`, `Accept-Encoding`, `Sec-Fetch-*`,
`Origin`, `DNT`, `Priority`) are also present but are just Firefox defaults — not
API-meaningful beyond matching a real browser fingerprint if the backend does UA
sniffing/bot detection.

### Auth: cookies required
The `Cookie` header carries the whole M365/Office auth session. Names present (values
are opaque, tenant/session-specific, and were redacted from analysis):

- `OH.SID`, `OH.FLID`, `OH.RNG` — Office Hub session identifiers
- `AjaxSessionKey` — signed session key for XHR calls
- `userid` — hex user id
- `msal.cache.encryption` — MSAL token-cache encryption key descriptor (JSON: `id`, `key`)
- `OhpToken`, `OhpAuth`, `OhpAuthC1`, `OhpAuthC2` — Office Hub Platform auth tokens (`OhpToken` is a large opaque signed/encrypted blob — base64-ish, looks like a serialized+encrypted auth ticket, **not** a plain JWT)
- `CS`, `SSREnabled` — small config/feature cookies

There's no `Authorization: Bearer ...` header — auth is entirely cookie-based here,
which means a Python client can't just replay a static token: it needs either
(a) a full interactive login (Selenium/Playwright driving the real MSAL OAuth flow to
mint these cookies), or (b) cookies lifted from a live authenticated browser session
(as done here) and refreshed periodically, since `OhpToken` is short-lived (the decoded
prefix shows an embedded expiry timestamp `10/15/2026 15:16:24 +00:00` in this sample).

### Request body shape
```jsonc
{
  "action": "ConversationResponseComplete",
  "conversationId": "<guid>",
  "traceId": "<guid>",           // client-generated, new per call
  "isNewChat": false,
  "conversationTitle": "hello",
  "gptId": "",
  "atMentionAppId": "",
  "turnState": "Completed",
  "state": {
    "conversationPageHistoryList": {
      "chats": [ /* full chat-list sidebar model, one entry per conversation */ ],
      "syncState": "<opaque base64 blob, itself base64(JSON) w/ a nested ICSyncState>",
      "hasRetentionPolicy": false,
      "metrics": { /* client-side perf telemetry: timeToFirstByte, apiCall, total, ... */ }
    },
    "chatType": "web",
    "agentList": [ /* installed/pinned Copilot agents, e.g. "Microsoft 365 Admin" */ ]
  }
}
```
Body is ~5 KB, almost entirely a dump of the sidebar/conversation-list Redux-ish store
plus the pinned-agent list — this is a client→server state push, not a prompt payload.

### Response shape
```jsonc
{
  "store": {
    "conversationPageHistoryList": {
      "chats": [ /* server's merged/authoritative version of the same chat list */ ],
      "syncState": "<updated opaque sync token>",
      "hasRetentionPolicy": false,
      "metrics": { /* server-observed timings + fluxVersion:"3" */ }
    }
  },
  "__queryState": { "mutations": [], "queries": [] }
}
```
Notably `agentList` is *not* echoed back — only `conversationPageHistoryList`. The
server-authoritative `syncState` token from the response is presumably meant to be
carried forward as the client's opaque cursor for the next state-sync call.

## Practical takeaways for `m365_openai_proxy.py`

1. This capture proves cookie-based auth + a `/chat` JSON API exist, but doesn't show
   the actual prompt/response wire format yet — **capture a full send-message turn
   next** (HAR spanning from clicking send to the reply finishing rendering).
2. Any Python client will need a cookie jar with all 11 cookies above, refreshed via a
   real login flow — there's no simple static API key/bearer token path here.
3. `X-Session-Id` and `traceId` are per-call/per-session GUIDs the client mints itself
   (`uuid4()` is fine) — not secrets, just correlation ids.
4. `X-Client-Eligibility` looks like it should be fetched from a bootstrap/config
   endpoint per tenant rather than hardcoded — check for that call in a fuller capture.

---

## Update: second capture — `m365.cloud.microsoft_Archive [26-07-18 16-25-19].har`

This one is a **full page-load capture** (160 entries, page load → new chat "ahoj"
created → a reply came back), which answers some questions from above and raises one
important new one.

### `/chat` is a generic RPC/action dispatcher, not a single-purpose endpoint

Every `POST /chat` in this HAR carries the same headers (`X-Route-Id: chat`, same
`X-Session-Id` for the whole page lifetime, same `x-host-context`) and differs only in
the `action` field of the JSON body. Actions observed, in call order:

| # | action | purpose (inferred) |
|---|---|---|
| 4 | `GetPersonalizationUserFlags` | fetch per-user UI feature flags |
| 5 | `GetConversationPageHistoryList` | initial sidebar chat-history fetch (this is what `ConversationResponseComplete` from the first capture later pushes an update *for*) |
| 15 | `RefreshNavPane` | refresh the left nav (agents/notebooks) |
| 30 | `GetUserPinnedApps` | app-launcher pinned-apps list |
| 31 | `GetAppLauncherCoreApps` | app-launcher core-apps list |
| 32 | `GetConversationPageHistoryList` | re-fetch after opening/creating a chat |
| 110 | `ConversationResponseComplete` | same state-sync call as capture #1, this time for a **new** chat: `conversationId: aa3793d6-fa7f-44ef-b884-2eb6e43af0cd`, `conversationTitle: "ahoj"`, `isNewChat: true` |

So `GetConversationPageHistoryList`/`ConversationResponseComplete` are two actions on
one shared endpoint — a Python client will likely need to speak this same
`{action, ...}` dispatch protocol rather than hitting distinct REST routes per
operation.

### The actual prompt→completion call is still missing — and now we know why

Between entry 32 (chat opened, 16:24:49) and entry 110 (`ConversationResponseComplete`,
16:25:03) there's a ~13s gap where the user's "ahoj" message was sent and answered, but
**no HTTP request in the HAR carries a user message or a model response**. The only
things happening in that window are telemetry beacons (`/events`,
`OneCollector`), config/feature-flag fetches, and — critically — this pair:

```
POST https://teams.microsoft.com/registrar/prod/V3/registrations
  {"clientDescription":{"appId":"bizchat","platform":"3639/1.0.0",
    "templateKey":"bizchat_5.0","productContext":"COPILOT"},
   "registrationId":"<epid>",
   "transports":{"TROUTER":[{"path":
     "https://pub-ent-sece-01-f.trouter.teams.microsoft.com:3443/v4/f/AJVYLcgShESYbPSmWYHSKA/",
     "ttl":3600}]}}

GET https://go.trouter.teams.microsoft.com/?check=...&tc={"ua":"BizChat",...}
  -> 200 "Trouter"
```

This is **Trouter**, Microsoft's real-time pub/sub transport (same one Teams uses for
live message delivery) — the client registers itself (`appId: "bizchat"`,
`productContext: "COPILOT"`) and gets back a per-session WebSocket URL
(`pub-ent-sece-01-f.trouter.teams.microsoft.com`). The actual send-message request and
the streamed assistant reply almost certainly ride over **that WebSocket**, not over
plain `POST /chat`. Firefox's "Save All As HAR" does not capture WebSocket frames at
all (no upgrade handshake entry even shows up here), which is exactly the shape of gap
we're seeing.

**Implication for this project:** a working Python client can't just be an HTTP
wrapper around `/chat` — it likely needs a WebSocket/Trouter client too. Next capture
should be done with a tool that *does* record WS frames:
- Chrome/Edge DevTools → Network → WS filter → select the trouter connection → "Messages" tab (Firefox's HAR export drops these, Chrome's HAR export can include `_webSocketMessages` per entry), or
- a MITM proxy (mitmproxy/Charles) sitting in front of the browser, which captures WS frames regardless of exporter support.

Capture: open the chat page, open devtools *before* sending, send one message, wait for
the full reply, then export — and specifically check the WS frames on the
`pub-ent-sece-01-f.trouter.teams.microsoft.com` connection.

### Bonus finding: a second auth path exists (MSAL bearer tokens, not just cookies)

Entry 46 is a silent MSAL token refresh:

```
POST https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token
  ?client_id=a2760c41-63c9-42b5-8d58-bfa1fd9e2eb3        (the M365-web SPA's AAD app)
  &brk_client_id=4765445b-32c6-49b0-83e6-1d93765276ca    (broker app id, Outlook web)
  &brk_redirect_uri=https://m365.cloud.microsoft/spalanding
grant_type=refresh_token, redirect_uri=brk-multihub://outlook.office.com
scope: https://lifecycle.office.com/eligibility.read openid profile offline_access
-> access_token (Bearer, ~1hr), refresh_token (rotated), id_token
```

This mints a bearer token scoped to `lifecycle.office.com` (used for eligibility/UX
flag checks like `GetPersonalizationUserFlags`), and separately `graph.microsoft.com`
calls in this capture (`/me/drive/special/copilotuploads`, `/me/photos/96x96/$value`,
`/me/informationProtection/sensitivityLabels`) are presumably bearer-authed against
Graph's own scopes via the same MSAL client. So auth here is actually **two systems
layered**: the Office Hub cookie jar (documented above) for `/chat` and `/events`, plus
standard MSAL/AAD bearer tokens (silently refreshed via hidden iframe/refresh_token) for
Graph and a few auxiliary services. Whether the Trouter/chat-completion path needs a
bearer token, a cookie, or something derived from both is exactly what the next capture
needs to show.

---

## Update: two per-connection WebSocket HARs (Firefox)

`m365.cloud.microsoft_m365Copilot_Chathub_..._Archive.har` and
`m365.cloud.microsoft_v4_c_Archive.har` — each is a single entry, saved by selecting one
WS connection in the Network panel and exporting just that row, rather than the whole
page. This reveals there are **two separate WebSocket channels**, not one:

1. `wss://substrate.office.com/m365Copilot/Chathub/{userId}@{tenantId}?chatsessionid={sessionId}`
   — path shape (`/Chathub/`, `userId@tenantId`) strongly suggests an **ASP.NET SignalR
   hub** dedicated to Copilot chat. This is the far more likely candidate for the
   actual prompt-send / streamed-token-response channel — much more specific than
   Trouter.
2. `wss://go.trouter.teams.microsoft.com/v4/c?tc={"cv":...,"ua":"BizChat",...}` — this is
   the generic Trouter connection from the previous capture (registered via
   `/registrar/prod/V3/registrations`). Given Trouter is a shared cross-app
   notification bus, it's more likely presence/typing/read-receipt style signaling than
   the actual completion payload — but not ruled out.

**Bad news: both HARs only contain the opening handshake.** Each entry is just the
`GET` with `Upgrade: websocket` / `Sec-WebSocket-*` headers and a `101 Switching
Protocols` response with empty content (`{"mimeType":"text/plain","size":0,"text":""}`)
— there's no `_webSocketMessages` field or any frame data. **This is a Firefox HAR
exporter limitation, not something switching to Chrome fixes by itself** — neither
browser's built-in "Save/Copy as HAR" serializes WS frame payloads into the file; both
only capture the handshake. Chrome's Network panel *can display* frames live in its
"Messages" tab for a selected WS row, but that's a UI view, not something that ends up
in an exported `.har`.

### What actually captures WS frame payloads
- **DevTools UI, read live (either browser):** open the WS connection's row → Messages/Response tab → frames stream in live as you chat. You'd have to manually copy them out (select-all → copy) during/after the conversation — tedious but zero extra tooling.
- **A MITM proxy (mitmproxy, Charles, Fiddler)** sitting in front of the browser: these do capture full WS frame traffic including payloads, independent of what the browser exports. This is the reliable option — `mitmproxy -w capture.flow` then inspect with `mitmproxy -r capture.flow` or the mitmproxy Python API.
- **Chrome DevTools Protocol (CDP) automation** (Playwright/Puppeteer): subscribe to `Network.webSocketFrameSent` / `Network.webSocketFrameReceived` and log every frame to a file yourself.

Given `Chathub` is very likely SignalR, if/when frames are captured: expect the
**SignalR JSON Hub Protocol** framing — each message is JSON followed by an ASCII
record-separator byte (`\x1e`), with an initial handshake frame
`{"protocol":"json","version":1}\x1e` before any real traffic.

**Recommendation:** don't bother re-capturing from Chrome — same limitation applies.
Reach for mitmproxy (I can set up a launch script) or manually copy frames from the
DevTools Messages tab while sending one message in the `Chathub` connection specifically
(that's the one to watch, not Trouter).

---

## Update: `Chathub.log.jsonl` — this is it, the actual protocol

This is exactly the missing piece: a frame-by-frame dump of the `Chathub` WebSocket for
one full turn (user sends "dobry den" → bot replies "Dobrý den! 😊 Jak vám mohu dnes
pomoci?"). 21 frames, each JSON followed by the SignalR record-separator byte `\x1e`
(the file has them newline-joined — split on `\x1e` or `\n`, doesn't matter here since
each frame happens to be on its own line).

### It's SignalR (confirmed), specifically the "Sydney"/Bing-Chat hub

The WS URL itself (captured in the earlier per-connection HAR) is:

```
wss://substrate.office.com/m365Copilot/Chathub/{userOid}@{tenantId}
  ?chatsessionid={sessionId}
  &XRoutingParameterSessionKey={sessionId}
  &clientrequestid={sessionId}
  &X-SessionId={dashed-sessionId}
  &ConversationId={conversationId}
  &access_token={JWT}
  &variants={huge CSV of feature-flag names}
  &source=%22officeweb%22
  &product=Office
  &agentHost=Bizchat.FullScreen
  &licenseType=Starter
  &isEdu=false
  &agent=web
  &scenario=OfficeWebIncludedCopilot
```

Key discoveries here:
- **Auth for this channel is a bearer JWT passed as a `access_token` query param on the
  WS URL itself** — not a cookie, not an Authorization header. Decoding the JWT (do
  this yourself, it's a live credential — don't paste it anywhere) shows
  `"aud": "https://substrate.office.com/sydney"` and a `scp` (scope) claim including
  `sydney.readwrite`, `M365Chat.Read`, and a long list of `CopilotPlatform*.*` scopes.
  So there's a **third token type** beyond the Office-Hub cookies and the
  `lifecycle.office.com` MSAL bearer from capture #2: an AAD-issued JWT scoped
  specifically to the `.../sydney` resource. **"Sydney" is Bing Chat's internal
  codename** — this confirms M365 Copilot's chat hub is the same backend
  infrastructure as Bing Chat/Copilot, just fronted by `substrate.office.com` instead
  of `bing.com`. `contentOrigin: "DeepLeo"` on every bot message (below) is the
  matching internal model-serving codename. This means prior community
  reverse-engineering of Bing Chat's SignalR protocol (e.g. the old EdgeGPT/BingGPT
  Python projects) is directly relevant prior art for this project, though field
  shapes may have drifted since (`optionsSets`, `variants`, message schema all look
  like they've grown a lot since those projects were written).
- **No `/negotiate` call happens.** I checked the full page-load HAR (capture #2) for
  any `negotiate`/`chathub`/`sydney` URL — none exist, even though the WS connects
  within that HAR's time window. Standard SignalR JS clients call `POST .../negotiate`
  first to get a `connectionToken` before opening the WS. Here the WS URL is fully
  assembled client-side up front (session id, conversation id, and — presumably fetched
  moments earlier from a bootstrap/eligibility call — the access token and the giant
  `variants` flag list) and opened directly. **A Python client can likely skip
  `/negotiate` entirely** and just open the WS with an equivalent URL once it has a
  valid `access_token` for the `sydney` resource, a session id, and a conversation id.

### Frame-by-frame protocol

SignalR JSON Hub Protocol framing: each frame is `<json>\x1e`. Message `"type"` values
seen: `1`=Invocation, `2`=StreamItem, `3`=Completion, `4`=StreamInvocation, `6`=Ping,
`7`=Close.

| # | dir | type | target/note | content |
|---|---|---|---|---|
| 0 | C→S | (handshake) | | `{"protocol":"json","version":1}` |
| 1 | S→C | (handshake ack) | | `{}` |
| 2 | S→C | 6 (ping) | | keepalive |
| 3 | **C→S** | **4 (StreamInvocation)** | **`target:"chat"`, `invocationId:"0"`** | **the actual send-message call — see payload below** |
| 4 | S→C | 1 (Invocation) | `target:"Metrics"` | client-side timing checkpoints echoed back (`ConnectionStart`, `UserInputSubmit`, `RequestSent`, ...) |
| 5 | S→C | 1 | `target:"update"` | `{nonce, requestId, throttling:{maxNumUserMessagesInConversation, numUserMessagesInConversation, ...}}` — quota/rate-limit info |
| 6 | S→C | 1 | `target:"update"` | first token: `messages:[{text:"D", author:"bot", messageId, adaptiveCards:[...], contentOrigin:"DeepLeo", ...}]` + a `cursor` (JSONPath into the message being built) |
| 7–8 | S→C | 1 | `target:"update"` | subsequent tokens as **deltas**: `{"writeAtCursor": "obrý", "nonce": ...}` — append at the last cursor position, no full text resent |
| 9–10 | S→C | 1 | `target:"update"` | periodically the **full accumulated text** is resent too: `messages:[{text:"Dobrý den! 😊 Jak vám", ...}]` — so a client can resync from either deltas or full snapshots |
| 11–12 | S→C | 1 | `target:"update"` | more `writeAtCursor` deltas, finishing the sentence |
| 13 | S→C | 1 | `target:"update"` | full final text again |
| 14 | S→C | 1 | `target:"update"` | `patches:[{operationType:2 (replace), path:"/{messageId}/spokenText", value:"..."}]` — a JSON-Patch-style op setting TTS text separately from display text |
| 15 | S→C | 1 | `target:"update"` | a `messages` entry with `messageType:"ReferencesListComplete"` — signals citations/search-results (if any) are done streaming |
| 16 | S→C | 1 | `target:"update"` | final full bot message again, now carrying `requestId` |
| 17 | S→C | **2 (StreamItem)**, `invocationId:"0"` | | `item:` the **user's own message**, echoed back fully persisted/enriched (server-assigned timestamps, `locationInfo` geolocated from IP, `market`, `locale`) |
| 18 | S→C | **3 (Completion)**, `invocationId:"0"` | | ends the `chat` stream invocation started in frame 3 |
| 19 | S→C | 1 | `target:"Metrics"` | final perf metrics: `RequestSent`→`FirstTokenReceived`→`LastTokenReceived` timestamps, token/char counts, inter-token timing stats |
| 20 | S→C | 7 (Close) | `allowReconnect:true` | hub closes the connection after the turn |

### The client→server `chat` invocation payload (frame 3), fully

```jsonc
{
  "type": 4, "target": "chat", "invocationId": "0",
  "arguments": [{
    "source": "officeweb",
    "clientCorrelationId": "<guid>",       // = traceId, fresh per turn
    "sessionId": "<client session guid>",  // stable per page load
    "optionsSets": [ /* ~29 feature toggles, e.g. "cwc_code_interpreter", "cwc_flux_v3", "rich_responses", ... */ ],
    "streamingMode": "ConciseWithPadding",
    "options": {},
    "extraExtensionParameters": {},
    "allowedMessageTypes": [ /* ~30 message-type names the client can render: "Chat","Suggestion","GeneratedCode","SearchQuery","MemoryUpdate","EndOfRequest", ... */ ],
    "sliceIds": [],
    "threadLevelGptId": {},
    "traceId": "<same guid as clientCorrelationId>",
    "isStartOfSession": false,
    "clientInfo": {
      "clientPlatform": "mcmcopilot-web", "clientAppName": "Office",
      "clientEntrypoint": "mcmcopilot-officeweb", "clientSessionId": "<session guid>",
      "ProductCategory": "Chat", "clientAppType": "Web",
      "productEntryPoint": "ChatPanel", "deviceOS": "Linux",
      "deviceType": "Desktop", "clientPlatformVersion": "Unknown"
    },
    "message": {
      "author": "user", "inputMethod": "Keyboard", "text": "<the prompt>",
      "entityAnnotationTypes": ["People","File","Event","Email","TeamsMessage"],
      "requestId": "<same guid as traceId>",
      "locationInfo": {"timeZoneOffset": 2, "timeZone": "Europe/Prague"},
      "locale": "en-gb", "messageType": "Chat", "experienceType": "Default",
      "adaptiveCards": [], "clientPreferences": {},
      "connectedFederatedConnections": ["dummyId"]
    },
    "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
    "isSbsSupported": true, "tone": "Magic",
    "renderReferencesBehindEOS": true, "disconnectBehavior": "continue"
  }]
}
```

Everything a client needs to *send* to get a reply is in this one object — `text` is
the only thing that varies per turn (plus a fresh `clientCorrelationId`/`traceId`/
`requestId`, which should all three be the same new GUID each turn based on this
sample).

### What's now fully understood vs. still open

**Understood well enough to prototype a client:**
- WS URL shape + where the bearer token/session/conversation ids go
- SignalR JSON framing (`\x1e`-terminated) and the handshake
- The exact `chat` invocation payload to send
- How to reassemble the streamed reply: accumulate `writeAtCursor` deltas or just take
  the latest full `messages[].text` snapshot (simpler, slightly more bandwidth); stop
  once the `Completion` (type 3) frame for the matching `invocationId` arrives

**Still open:**
- Where exactly the `access_token` (sydney-scoped JWT) and the `variants` list get
  minted/fetched client-side before the WS opens — likely another `/chat` action or a
  dedicated bootstrap endpoint not yet captured. Needed to know how to mint this token
  from Python without a live browser.
- Whether `isNewChat`/first-message-in-a-brand-new-conversation differs in payload
  shape from a reply-in-existing-conversation (this sample was the first message of a
  new chat — worth capturing a second-turn-in-same-conversation frame dump to compare).
- Multi-turn history: does the client resend prior turns in `message`/`options`, or
  does the server look them up server-side from `ConversationId`? Nothing in this
  single-turn capture answers that.

---

## Consolidated: the authentication situation

Pulling together everything across all four captures — there are **three independent
credential systems** in play, not one. None of the captures show the *initial*
interactive login (all sessions were already signed in when capture started), but the
*renewal* mechanics are visible for two of the three.

### 1. Office Hub Platform (OHP) session — plain HTTP cookies

Used for: `POST /chat` (the action-dispatch RPC endpoint) and `POST /events` (telemetry).
Carried entirely in the `Cookie` header — no bearer token involved for these calls.

- Cookies: `OH.SID`, `OH.FLID`, `OH.RNG`, `AjaxSessionKey`, `userid`, `OhpToken`,
  `OhpAuth`, `OhpAuthC1`, `OhpAuthC2`, `CS`, `SSREnabled`.
- `OhpToken` is *not* fully opaque — its first bytes are a length-prefixed **plaintext
  expiry timestamp**. Decoding the raw base64: `\x01\x00\x00\x00\x1a` (4-byte header +
  1-byte length 0x1a=26) then the literal ASCII string `10/15/2026 15:16:24 +00:00`,
  followed by another length-prefixed opaque signed blob (`1.ATYA8gzZM9Fxhk...`). So this
  cookie is good until that absolute timestamp — roughly 3 months out from the July
  capture — and the rest is presumably a server-verifiable signed/encrypted ticket.
- **No renewal of this cookie family was observed anywhere** — none of the ~50
  `/chat`/`/events` responses across the full-session capture set a new `Set-Cookie` for
  any of these names. Either it's simply long-lived enough not to need mid-session
  renewal, or renewal happens through a flow not exercised in a ~35-second capture
  window (e.g. only near actual expiry, or only on full page reload).
- This is a classic **BFF (backend-for-frontend) session cookie** — established once at
  login when the SSR backend presumably validates an AAD auth code/token server-side and
  mints its own first-party session, so the browser doesn't need to attach a bearer
  token to every SSR call.

### 2. MSAL/AAD bearer tokens — silent `refresh_token` grant, browser-cached

Used for: Microsoft Graph calls (`/me/drive/...`, `/me/photos/...`,
`/me/informationProtection/...`) and the `lifecycle.office.com` eligibility check.

**Yes, an explicit refresh-token → access-token exchange is captured, in full:**

```
POST https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token
    ?client_id=a2760c41-63c9-42b5-8d58-bfa1fd9e2eb3          (SPA's own AAD app)
    &brk_client_id=4765445b-32c6-49b0-83e6-1d93765276ca      (broker app — Outlook Web)
    &brk_redirect_uri=https://m365.cloud.microsoft/spalanding
Content-Type: application/x-www-form-urlencoded
  client_id=a2760c41-...
  redirect_uri=brk-multihub://outlook.office.com
  scope=https://lifecycle.office.com/eligibility.read openid profile offline_access
  grant_type=refresh_token
  refresh_token=<1449-char opaque token>
  x-client-SKU=msal.js.browser, x-client-VER=5.9.0     (identifies MSAL Browser)
  X-AnchorMailbox=<mailbox routing hint>
→ 200 {
    access_token: <2725-char token — 5 dot-segments = a JWE, i.e. ENCRYPTED, not a
                   plain JWT; the client can't read its own claims>,
    refresh_token: <1448 chars — ROTATED, different from the one sent>,
    id_token, expires_in: 3746 (~62 min), refresh_token_expires_in: 85273 (~23.7h),
    scope: (four lifecycle.office.com/* scopes granted back)
  }
```

Mechanics: this is `msal-browser`'s **silent token acquisition via refresh token**,
called in the background (no visible redirect/popup, `Sec-Fetch-Mode: cors`) whenever
the SPA needs a token for a resource/scope it doesn't currently hold a valid
access_token for. The **`brk_client_id`/`brk_redirect_uri` pair is a broker pattern**:
`m365.cloud.microsoft`'s app registration (`a2760c41-...`) is going through Outlook
Web's app registration (`4765445b-...`) as an SSO broker so the user isn't re-prompted
when moving between first-party M365 web apps that share a browser session — this is
why the redirect_uri looks like `brk-multihub://outlook.office.com` rather than a
plain `https://` URL.

**Where the refresh token lives:** MSAL Browser keeps its whole cache — accounts, id
tokens, access tokens, and the refresh token — in browser Web Storage
(`localStorage` by default for `msal.js.browser`, occasionally `sessionStorage`
depending on the app's `cacheLocation` config; not visible from a HAR either way since
that's page-internal storage, not network traffic). The **`msal.cache.encryption`
cookie** (`{"id":"<guid>","key":"<base64 AES key>"}`) is MSAL Browser's cache-encryption
feature: the cached tokens in Web Storage are AES-encrypted at rest, and the decryption
key is kept only in this cookie rather than alongside the ciphertext — a defense
specifically against XSS/extension code reading `localStorage` and exfiltrating a
plaintext refresh token. **This cookie was never `Set-Cookie`'d in any of the four
captures**, meaning it was already present before capture started, and — since MSAL
generates this key client-side with `crypto.subtle` rather than receiving it from the
server — it's almost certainly written via `document.cookie` by the page's own JS,
which means it is **not `HttpOnly`** (a script can't set an HttpOnly cookie). I can't
100% confirm that from HAR data alone (no `Set-Cookie` response header was captured for
it to check flags on) — quickest way to verify directly: DevTools → Storage → Cookies →
check the `HttpOnly` column for `msal.cache.encryption` on `m365.cloud.microsoft`.

### 3. The Sydney-scoped JWT — powers the Chathub WebSocket

Used for: the one thing this whole project actually cares about — the
`access_token` query parameter on the `wss://substrate.office.com/m365Copilot/Chathub/...`
URL. Decoded (this one *is* a plain signed JWT, 3 segments, RS256 — not encrypted):

```
aud:    https://substrate.office.com/sydney
iss:    https://sts.windows.net/33d90cf2-71d1-4486-a386-6c34403143f7/
appid:  c0ab8ce9-e9a0-42e7-b064-33d422df41f1     <-- a THIRD, different AAD app id
                                                       (not a2760c41, not the broker 4765445b)
scp:    CopilotPlatformContent.Process.All, CopilotPlatformFiles.ReadWrite(All),
         CopilotPlatformMail.Read(.Shared), CopilotPlatformTeams.ReadWrite.All,
         M365Chat.Read, sydney.readwrite, ... (16 scopes total)
secaud: { aud: 00000003-0000-0000-c000-000000000000 (= Microsoft Graph's resource id),
          scp: Channel.Create, Chat.ReadWrite, Files.ReadWrite.All, Mail.Read.Shared,
               Presence.Read, Sites.Read.All, Team.ReadBasic.All, User.Read, ... }
lifetime (exp - iat): 5549s ≈ 92 minutes
```

The `secaud` ("secondary audience") claim carrying a full parallel set of Graph-shaped
scopes on a token whose primary audience is `.../sydney` is notable — it looks like this
single token is meant to be usable/translatable against Graph as well as the Sydney
chat backend, which would explain how a single Chathub session can transparently pull in
your files/mail/Teams presence for grounding without a separate token per Graph call.

**This is the open thread from before, now narrowed down:** the actual
`POST .../oauth2/v2.0/token` call that mints *this* token (`appid: c0ab8ce9-...`) was
not captured in any of the four HARs — only the `lifecycle.office.com` one
(`appid: a2760c41-...`) was. But now we know exactly what to grep for in a fuller
capture: **`client_id=c0ab8ce9-e9a0-42e7-b064-33d422df41f1`** against
`login.microsoftonline.com`. It's overwhelmingly likely the same silent
`grant_type=refresh_token` mechanism as capture #2, just requesting
`scope=https://substrate.office.com/sydney/.default` (or similar) instead — MSAL Browser
can silently mint tokens for as many different resources as the app requests scopes for,
all from the same cached refresh token, they just show up as separate `/token` calls
per distinct (client_id, scope) pair. Worth one more targeted capture (reload the chat
page with devtools already open, filtering Network to `login.microsoftonline.com`) to
nail this down and see whether it's a fresh interactive-broker call or purely silent.

### Summary table

| System | Transport | Token type | Observed lifetime | Where minted | Where stored |
|---|---|---|---|---|---|
| Office Hub session | `Cookie` header | opaque signed blob w/ plaintext expiry prefix | ~long (3mo in this sample) | server-side at login (not captured) | browser cookie jar |
| MSAL/Graph + lifecycle | `Authorization`-style (bearer, used internally by SDK) | JWE (encrypted, opaque to JS) | 62 min access / 23.7h refresh | `login.microsoftonline.com` via silent `refresh_token` grant (captured in full) | refresh token: browser Web Storage, AES-encrypted, key in `msal.cache.encryption` cookie |
| Sydney/Chathub | WS URL query param `access_token` | plain signed JWT (RS256) | ~92 min | `login.microsoftonline.com`, different `client_id` (not yet captured) | presumably same MSAL cache as above, different (client_id, scope) cache entry |

---

## Update: full login captured — every open thread closed

`m365.cloud.microsoft_Archive [26-07-18 16-50-48].har` (737 entries) captures the
**entire interactive sign-in**, not just a renewal. This answers everything left open,
plus one important new discovery (FOCI). It also contains your literal password in
the `POST /common/login` body — see the security note at the top of this session; treat
this file as a live credential, not just a debugging artifact.

### The full flow, in order

1. **`GET /login?es=UnauthClick&ru=...`** (entry 57) — hitting the site unauthenticated.
   The **ASP.NET Core** backend (this is now confirmed — see the cookie names below)
   immediately sets `OH.SID` and `OH.FLID` (both `Secure; HttpOnly; SameSite=None`,
   `OH.FLID` valid a full year) plus standard ASP.NET Core OIDC scaffolding cookies —
   `.AspNetCore.OpenIdConnect.Nonce.*` and `.AspNetCore.Correlation.*` (also
   `HttpOnly`/`Secure`, ~15 min lifetime) — then **302s to**
   `login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=4765445b-...`.
   So `4765445b-32c6-49b0-83e6-1d93765276ca` (previously identified only as a `brk_client_id`)
   is actually **the SSR app's own registered OIDC client id** — its dual role as
   "broker" for later browser-side silent calls is a reuse of the same registration, not
   a separate app.
2. **`GET /authorize`** (58) → Microsoft's real login page (200 HTML).
3. **`POST /common/GetCredentialType`** (77) — client sends your username, server says
   `HasPassword: true` (i.e. not federated/passwordless-only).
4. **`POST /common/login`** (81) — the actual credential submission:
   `login`/`loginfmt` = your email, **`passwd` = your password (plaintext, in the
   request body)**, plus `canary`/`ctx`/`flowToken` anti-forgery/state blobs.
5. **`POST /kmsi`** (94) — "Keep Me Signed In" confirmation.
6. **`POST /landingv2`** (97) — the OIDC redirect callback (`response_mode=form_post`
   lands here). This single response both **deletes** the transient `OhpCode`/`Nonce`/
   `Correlation` cookies (`expires=1970...`) **and sets the real session**:
   `OhpToken` (`Secure; SameSite=Lax; HttpOnly`, expiring `15 Oct 2026` — same absolute
   date format seen embedded in its own payload before), `OhpAuth`/`OhpAuthC1`/`OhpAuthC2`
   (all `HttpOnly`), and `CS` (UI theme prefs, 1yr, not `HttpOnly`). **This confirms the
   Office Hub cookies are `HttpOnly`** — a script running on the page cannot read them,
   unlike `msal.cache.encryption` (see below).
   This is the classic **OIDC hybrid flow** (`response_type=code id_token`): the
   `id_token` in this same POST is what the ASP.NET Core middleware validates
   server-side to mint the OHP session; a leftover authorization **`code`** from the same
   response is handed to the page for the browser-side MSAL.js SPA to redeem
   *separately* — which is exactly what happens ~200 requests later (JS bundles
   loading) at entry 312, once the SPA has booted.
7. **`POST /oauth2/v2.0/token`** (312), `grant_type=authorization_code`,
   `client_id=4765445b-...`, `scope=https://www.office.com/v2/OfficeHome.All openid
   profile offline_access` → returns an (opaque/JWE) access token **and the first
   refresh token**, call it **RT‑A**.

### Then: one refresh token, redeemed across 3 client_ids and 10 resources (FOCI)

This is the new discovery. Every subsequent `/oauth2/v2.0/token` call in the capture is
`grant_type=refresh_token`, and I hashed the `refresh_token` value on each request/response
to track identity without exposing it. Result — **a single rotating refresh token gets
silently redeemed across multiple different `client_id`s and many different resource
scopes**, one `/token` call per (resource, and sometimes client_id):

```
RT-A (from code exchange, client 4765445b)
 ├─ client_id=c0ab8ce9-...  scope=graph.microsoft.com/.default          → RT-B
 ├─ client_id=c0ab8ce9-...  scope=titles.prod.mos.microsoft.com/.default→ RT-C
 ├─ client_id=c0ab8ce9-...  scope=substrate.office.com/search/.default  → RT-D
 ├─ client_id=c0ab8ce9-...  scope=substrate.office.com/sydney/.default  → RT-E   ← the Chathub token!
 ├─ client_id=c0ab8ce9-...  scope=ic3.teams.office.com/.default         → RT-F
 ├─ client_id=c0ab8ce9-...  scope=m365.cloud.microsoft/v2/.default      → RT-G
 └─ client_id=c0ab8ce9-...  scope=substrate.office.com/.default         → RT-H
      RT-H redeemed again → client_id=a2760c41-... scope=lifecycle.office.com/eligibility.read → RT-I
        RT-I redeemed again → client_id=c0ab8ce9-... scope=o365filtering.com/.default          → RT-J
                            → client_id=c0ab8ce9-... scope=loki.delve.office.com/.default       → RT-K
                            → client_id=c0ab8ce9-... scope=clients.config.office.net/.default    → RT-L
```

Notably, **RT‑A was reused as input across 7 parallel calls before any single response
had a chance to update the client's stored "latest" refresh token** — i.e. Entra ID
tolerates reusing the same refresh token multiple times in quick succession (each still
returns a newly-rotated one), rather than strict single-use invalidation. This is
**FOCI — Family of Client IDs** — a first-party-only Entra ID feature where a set of
Microsoft's own client registrations (`4765445b`, `c0ab8ce9`, `a2760c41` are all in the
same family here) can redeem each other's refresh tokens for their own scopes without
re-prompting the user. It's exactly how one interactive login (steps 1–7) silently
fans out into ~11 separately-scoped access tokens covering Graph, Sydney/Chathub,
Teams IC3, substrate search, config services, etc. — no popups, no visible re-auth.

**This directly answers the earlier open question:** the Sydney/Chathub token is minted
at exactly the call marked `RT-E` above:
```
POST login.microsoftonline.com/{tenant}/oauth2/v2.0/token
    ?brk_client_id=4765445b-32c6-49b0-83e6-1d93765276ca&brk_redirect_uri=https://m365.cloud.microsoft/spalanding
  grant_type=refresh_token
  client_id=c0ab8ce9-e9a0-42e7-b064-33d422df41f1
  scope=https://substrate.office.com/sydney/.default openid profile offline_access
  redirect_uri=brk-multihub://outlook.office.com
→ access_token: aud=https://substrate.office.com/sydney, appid=c0ab8ce9-..., ~90min lifetime
```
— confirmed by decoding the `access_token` actually used on this capture's Chathub WS
URL (entry 460): identical `aud`/`appid`, `iat` a few minutes after this token call.
**`c0ab8ce9-e9a0-42e7-b064-33d422df41f1` is the app registration a Python client needs
to impersonate/authenticate as to mint its own Sydney token** (via `scope=
https://substrate.office.com/sydney/.default`, same FOCI redemption from any refresh
token belonging to this family).

### Revised summary

- **Office Hub cookies**: now confirmed `HttpOnly` + `Secure`, minted server-side by an
  ASP.NET Core OIDC middleware at `/landingv2` from the hybrid flow's `id_token`. Not
  reachable by page JS at all.
- **MSAL refresh token** (browser-held, in Web Storage, key-encrypted via the
  `msal.cache.encryption` cookie as established earlier): this is **RT-A onward** — one
  FOCI-family token that silently mints access tokens for Graph, Sydney, Teams IC3,
  substrate, config, and more, each via its own `client_id`+`scope` combination.
  `msal.cache.encryption`'s non-`HttpOnly` status (inferred earlier, still not directly
  falsified/confirmed here — no fresh `Set-Cookie` for it appeared in this capture
  either, so it must already have existed pre-session, or is genuinely JS-authored and
  thus simply reused) stands in real contrast to the Office Hub cookies, which this
  capture *does* prove are `HttpOnly`.
- **Sydney/Chathub token**: `client_id=c0ab8ce9-e9a0-42e7-b064-33d422df41f1`,
  `scope=https://substrate.office.com/sydney/.default`, minted via the same silent
  `refresh_token` grant as everything else. No separate/special mechanism — it's just
  one more FOCI-redeemed resource.

### Still open (genuinely minor now)
- Multi-turn conversation payload shape (still only single-new-chat samples captured).
- Whether `msal.cache.encryption` is `HttpOnly` (would need a capture spanning the exact
  moment that specific cookie is first set — it wasn't set fresh in either this or the
  prior four captures).

---

## Update: MSAL localStorage cache-encryption algorithm — SOLVED

Extensive live testing (browser console hooks on `crypto.subtle.decrypt` across both
the main-thread and a Worker realm, and brute-forcing every plausible standard
AES-256-GCM parameter combination) never once succeeded at decrypting the
`msal.3|...|refreshtoken|...` localStorage entry using the obvious approach (base
key used directly, "nonce" field as the AES-GCM IV, no extra context). All of that
failed for a good reason: **it isn't raw AES-GCM at all.**

### The actual algorithm (from MSAL Browser's own source, not guesswork)

Confirmed by reading `lib/msal-browser/src/crypto/BrowserCrypto.ts` and
`lib/msal-browser/src/cache/LocalStorage.ts` directly from the
`AzureAD/microsoft-authentication-library-for-js` `dev` branch on GitHub (this is
MSAL Browser v4+ behavior per its own `docs/caching.md`: *"auth artifacts will be
encrypted... using HKDF to derive the key"* — a detail neither of us had internalized
until reading the actual diff/source):

1. `rawKey` = base64url-decode the `key` field from the `msal.cache.encryption` cookie.
2. `context` = the encrypting app's own `clientId`, **only if that clientId literally
   appears in the localStorage key string** (`LocalStorage.getContext()`:
   `key.includes(this.clientId) ? this.clientId : ""`). For a credential cache key
   (`msal.<schema>|homeAccountId|environment|credentialType|familyId|realm|target|scheme`),
   the `familyId` slot holds the *real clientId GUID* whenever the token isn't part of
   a shared FOCI family — `generateCredentialKey()` falls back to
   `credential.clientId` unless `credential.familyId` is truthy (family-shared refresh
   tokens instead carry a short marker like `"1"` there, in which case the true
   clientId never appears anywhere in the key and `context` is simply `""`).
3. A **per-entry** AES-256 key is derived via `HKDF-SHA256(ikm=rawKey,
   salt=base64url-decode(nonce), info=utf8(context))` — **the `nonce` field is the
   HKDF salt, not the AES-GCM IV** (this was the single biggest wrong assumption in
   every earlier attempt).
4. AES-GCM decrypt of the `data` field uses a **fixed all-zero 12-byte IV** — safe
   only because step 3 derives a brand-new key for every single
   encrypt/decrypt call (verbatim comment in MSAL's source: *"New key is derived for
   every encrypt so we don't need a new nonce"*).
5. The resulting plaintext is JSON:
   `{"credentialType":"RefreshToken","clientId":...,"secret":"<the refresh
   token>",...}`.

**Confirmed working**: decrypted a real captured cache entry end-to-end into
exactly that well-formed JSON shape (`credentialType: "RefreshToken"`, matching
`clientId`, plausible `secret` length) — AEAD authentication would have hard-failed
(`InvalidTag`) on any wrong key/IV/context, so a clean decrypt to coherent JSON is
strong proof the algorithm is right, independent of whether the specific extracted
secret is still redeemable by the time it's used.

### Implication for a Python client

`m365_openai_proxy.py`'s `_decrypt_msal_cache_entry()` implements this correctly now:
HKDF-SHA256 by hand with stdlib `hmac`/`hashlib`, and AES-256-GCM itself also
implemented from scratch in pure Python (`aes256_gcm_decrypt()` -- no third-party
crypto package at all; an earlier revision soft-depended on the `cryptography`
package for just the AES-GCM step, since the stdlib has no AES implementation, but
that dependency was removed once the pure-Python implementation was validated
against the FIPS-197 AES-256 test vector and cross-checked against `cryptography`
across a range of input lengths/AAD). The credentials file format gained a third
required field for this path, `local_storage_key` (the literal localStorage key
**name**, not just its value) — needed to compute `context` per point 2 above.

### Update: the `AADSTS70000` rejections were never a freshness race — REVISED, root cause found and fixed

The original writeup below (kept for the historical record) attributed every
`AADSTS70000`/`invalid_grant` rejection during live proxy testing to background token
rotation racing against the manual copy-paste workflow. That theory turned out to be
**wrong**. The real cause: `m365_openai_proxy.py`'s `exchange_refresh_token()` was
sending a request that, while RFC 6749-valid, looked nothing like genuine MSAL traffic.

Cross-checking against **every** captured `grant_type=refresh_token` request across
every HAR in this project (~16 examples: different scopes, different capture
sessions hours apart) showed 100% agreement on a shape our code didn't match:

- **Tenant-specific token endpoint** (`login.microsoftonline.com/{tid}/oauth2/v2.0/token`)
  — never `/common/`, which is what our code was using.
- **URL query string** always carries `client_id` and a fresh `client-request-id`
  GUID in addition to `brk_client_id`/`brk_redirect_uri` — our code only sent the
  latter two.
- **Form body** always carries six more MSAL-identity/telemetry fields our code never
  sent at all: `X-AnchorMailbox` (`Oid:{oid}@{tid}`), `x-client-SKU`
  (`msal.js.browser`), `x-client-VER` (`5.9.0`), `x-ms-lib-capability`
  (`retry-after, h429`), `x-client-current-telemetry`, `x-client-last-telemetry`.

Sending a request this distinguishable from real MSAL traffic is exactly the shape of
thing Entra ID's anti-automation heuristics are known to silently block while
returning an ordinary-looking `invalid_grant` error — indistinguishable from "this
token is genuinely stale" without controlling for it, which is exactly what made the
original freshness theory look plausible for so long.

**Fix, confirmed live:** `exchange_refresh_token()` now builds the exact same request
shape (see its docstring in `m365_openai_proxy.py`). `oid`/`tid` for the
tenant-specific URL and `X-AnchorMailbox` are derived from the `local_storage_key`
credential file's `homeAccountId` segment (`<oid>.<tid>`, the second `|`-separated
field) via a new `parse_home_account_id()` — no extra field needed from the operator.
Retested against the *exact same* `encrypted_refresh_token`/`cache_encryption_key`
files that had failed three times in a row with the old request shape: **decrypted
the identical stale-looking token and this time Entra accepted it immediately** —
proxy started, authenticated, and served both a normal and a streaming chat
completion successfully end-to-end. The plaintext-`refresh_token`-only credential
path still falls back to `/common/` and omits `X-AnchorMailbox` (no tenant id is
knowable ahead of the first exchange in that path) — untested whether that path needed
the same fix, since testing so far has centered on the encrypted-cache path, but the
five telemetry/SKU fields are now sent unconditionally either way.

---

### Original (superseded) writeup, kept for context

Even with a mathematically-proven-correct decrypt, redeeming the resulting secret
against Entra ID still failed with `AADSTS70000`/`invalid_grant` in testing — the
working theory at the time was that **something in the live browser session
continuously rotates this shared FOCI refresh token in the background**, superseding
it within seconds to low-minutes of any snapshot being taken (this looked consistent
with the raciness observed independently via the Network-tab live-capture method
throughout this whole investigation — see the several "stale token" sections above).
This turned out to be the wrong explanation — see the update above.

---

## Update: Local MCP tool-calling bridge — found and documented, NOT implemented

Investigated whether Sydney's proprietary plugin system could be bridged to
something resembling OpenAI-style tool/function calling, so that a client like
OpenCode could do real agentic work (not just chat) against this proxy. Short
answer: **there is a real, concrete mechanism** — Sydney can invoke Model
Context Protocol (MCP) tools through the client, over the exact same Chathub
SignalR connection this project already implements — and its wire shape has
been extracted directly from the officeweb client's own downloaded JS
(`m365.cloud.microsoft`'s webpack bundles, captured in the big HAR from the
initial login session), not guessed. **But turning this into OpenAI-shaped
tool-calling for a client like OpenCode hits a real architecture mismatch that
is not a quick fix** — see the "The actual blocker" section below before
starting on this.

### How it works (extracted from officeweb's own source)

Search terms that led here: `LocalMCPDiscovery` (a message-annotation type
seen in the `chat` invocation payload builder), which led to a whole
client-side MCP bridge implementation in one bundle
(`sydney-utils.vendors.a05ecef2.chunk.js`, module `690063`), and its wiring
into the Chathub `HubConnection` in another (`6810.d1f1688a.chunk.js`).

**The registration (this is the concrete proof it's on the SAME Chathub
connection, not some separate transport):**
```js
this.connection.on('update', this.update.bind(this)),
this.connection.on('completion', this.completion.bind(this)),
this.connection.on('invoke_local_plugin', this.invokeLocalPlugin),
this.connection.on('mcp_discover', this.localMCPPluginRouter.discoverMCPServers.bind(this.localMCPPluginRouter)),
this.connection.on('mcp_describe', this.localMCPPluginRouter.describeMCPServers.bind(this.localMCPPluginRouter)),
this.connection.on('unfurl_result', this.handleUnfurlResult),
```
`this.connection` is the exact same object `'update'`/`'completion'` are
registered on — i.e. the same Chathub SignalR hub connection this proxy
already opens and speaks. `mcp_discover`/`mcp_describe`/`invoke_local_plugin`
are three more SignalR invocation *targets* the server can call on the
client, alongside the `update`/`Metrics` targets already fully understood
(see "Chathub.log.jsonl" section above).

**`mcp_discover`** (client method `discoverMCPServers`): server asks the
client to enumerate matching local MCP servers.
```jsonc
// server -> client, SignalR type:1 Invocation, target:"mcp_discover"
// arguments[0] shape:
{
  "invocation": { "payload": "{\"queries\":[...]}" },
  "correlation_id": "<opaque id, echoed back>"
}
// client's return value (see "client results" note below for how this gets back to the server):
{
  "schema_version": "https://copilot.microsoft.com/schemas/plugins/local/transport/1.0",
  "correlation_id": "<echoed>",
  "response": {
    "status": "Success",  // or "Fail"
    "message": "Local MCP servers discovered successfully.",
    "payload": "{\"server_ids\":[\"...\"]}"
  }
}
```

**`mcp_describe`** (client method `describeMCPServers`): same envelope shape,
request payload is `{"server_ids":[...],"capabilities":["tools", ...]}`
(capabilities defaults to `["tools"]` if omitted), response payload is
`{"servers":[{server_id, tools:[{name, description, inputSchema, _meta?}],
prompts, resources, resourceTemplates, transport}, ...]}`. `inputSchema` here
is a plain JSON Schema object — structurally identical to what OpenAI calls
`parameters` in its own tool-calling `tools` array, so translating one to the
other is a lossless, mechanical mapping.

**`invoke_local_plugin`** (client method `invokeLocalPlugin` /
`invokeMCPServer` once it self-selects on the payload shape): the actual tool
call.
```jsonc
// server -> client, target:"invoke_local_plugin"
{
  "invocation": {
    "payload": "{\"method\":\"mcp_<toolName>\",\"params\":{...}}",
    "local_endpoint": "mcp://<server_id>"
  },
  "correlation_id": "<opaque id>"
}
```
The client parses `method` as `mcp_<toolName>` (literal `"mcp"` prefix before
the first `_`), looks up `<server_id>` from `local_endpoint` (stripping the
`mcp://` scheme), finds that server's `tools[]` entry matching `<toolName>`,
and — this is the important part — calls a REAL MCP client's `callTool({name,
arguments})` method against it (the code literally calls `.connect()`,
`.callTool()`, `.readResource()` — the same method names as the official MCP
TypeScript SDK's `Client` class). The result is wrapped and sent back as:
```jsonc
{
  "schema_version": "https://copilot.microsoft.com/schemas/plugins/local/transport/1.0",
  "correlation_id": "<echoed>",
  "response": {
    "status": "Success",
    "message": "Method <toolName> invoked successfully.",
    "payload": "{\"result\":[{\"id\":\"<correlation_id>\",\"data\":\"<JSON-encoded MCP tool result>\",\"type\":\"text/plain\",\"description_for_model\":\"Tool invocation result for method <toolName> on server <server_id>\"}],\"jsonrpc\":\"2.0\",\"id\":\"<correlation_id>\"}"
  }
}
```

**How the client's return value gets back to the server**: `discoverMCPServers`/
`describeMCPServers`/`invokeMCPServer` are all plain `async` functions
registered via `connection.on(target, handler)`, and simply *return* their
response object rather than calling any explicit "send reply" API. This
matches ASP.NET Core SignalR's **"client results"** feature (introduced
.NET 7 / a matching `@microsoft/signalr` version): when the server invokes a
client method WITH an `invocationId` (rather than a fire-and-forget `Send`),
the JS SignalR client automatically awaits the registered handler's returned
Promise and transmits a `type:3` Completion frame back to the server, keyed
by that same `invocationId`, carrying the resolved value as `result` —
entirely transparent to the application code shown above. **This detail is
inferred from how the feature is documented to work, not directly observed
on the wire** (see "What's NOT yet confirmed" below) — but it's consistent
with everything else here, since we independently confirmed via
`Chathub.log.jsonl` that ordinary fire-and-forget server invocations (the
`Metrics` target) carry no `invocationId` at all, which is exactly the
contrast this theory predicts.

### The actual blocker: synchronous mid-turn RPC vs. OpenAI's async tool-calling contract

Even with the wire shape fully known, there's a genuine architecture mismatch
between how Sydney's Local MCP works and how OpenAI tool-calling works, and
it's not a minor detail:

- **Sydney's model**: `invoke_local_plugin` happens *mid-turn*, synchronously,
  over the *same live connection* that's still streaming the rest of the
  reply. The server is waiting, with some (unknown, but presumably
  seconds-scale, not minutes-scale) timeout, for the SignalR `type:3`
  completion to come back on that same `invocationId` before it can continue
  generating. The real browser client can satisfy this because it has a
  *live, synchronous* MCP connection sitting right there in the same process.
- **OpenAI's model**: the model's response *stops* with `finish_reason:
  "tool_calls"`, the HTTP response for that request is already complete, and
  the client (OpenCode) decides in its own time what to do, then sends a
  **brand-new HTTP request** with the tool result appended to `messages`.
  There is no expectation of a fast turnaround — a human could be in the loop.

For this proxy to present `invoke_local_plugin` to OpenCode as an OpenAI tool
call, it would have to: (1) receive the mid-turn invocation, (2) translate it
into a `tool_calls` chunk and end the *current* HTTP response early, (3) keep
the Sydney WebSocket connection and the pending SignalR invocation alive and
un-completed while waiting for an *entirely separate* future HTTP request from
OpenCode (which requires the multi-turn/conversation-continuation work
described elsewhere in this document to correlate that follow-up request back
to the right pending Sydney turn), then (4) complete the SignalR invocation
with whatever OpenCode eventually sends back. Step (3) is the real risk: if
Sydney's server-side timeout on a pending client-results invocation is short
(ASP.NET Core SignalR's various timeouts are commonly configured in the
10-30s range, though this specific one is unconfirmed), a real tool-executing
agent loop easily exceeds it, and the whole approach falls over. This is a
solvable-in-principle but genuinely substantial next-phase engineering effort,
not a small patch to `exchange_refresh_token`-style fixes seen elsewhere in
this document.

### What's NOT yet confirmed (would need a live capture to settle)

- Whether `mcp_discover`/`mcp_describe`/`invoke_local_plugin` frames actually
  carry an `invocationId` the way "client results" implies — never directly
  observed, only inferred from the client code's structure and by elimination
  (contrasted against the confirmed-`invocationId`-less `Metrics` target).
- What Sydney's server-side timeout actually is for a pending client-results
  invocation before it gives up (directly determines whether step (3) above
  is viable at all).
- Whether "Local MCP" is actually enabled/reachable for this specific
  account/tenant at all — `CHATHUB_VARIANTS` in `m365_openai_proxy.py`
  already includes `EnableMcpServerWidgets`/`feature.EnableMcpServerWidgets`
  among the flags sent, but whether that flag is *honored* (vs. silently
  ignored because the account/tenant/license tier doesn't have this feature)
  is unknown — the UI toggle backing this (`enableLocalMcp` in the officeweb
  client's own feature-flag store) was found in the client code but never
  observed actually turned on or exercised in any capture.
- The exact shape of a `chat` invocation that advertises the client itself as
  MCP-capable in the first place (does the client need to send something in
  the outgoing payload announcing "I support Local MCP" before Sydney will
  ever bother sending `mcp_discover`? The `LocalMCPDiscovery` message
  annotation type suggests yes, but its outgoing shape wasn't captured).

The concrete next step, if this is worth pursuing further: a live capture
where Local MCP is actually exercised end-to-end in the real
`m365.cloud.microsoft` web client (find the "Local MCP" toggle in its
settings/menu, point it at a real local MCP server, and have a conversation
that actually triggers a tool call) would answer all four of the above in one
shot and turn this from "confirmed by reading source" to "confirmed on the
wire" — the same bar every other finding in this document was held to before
being implemented.

## Update: goal re-scoped to "drive a coding agent via m365 Copilot", multi-turn implemented via context-stuffing

The actual end goal was clarified: not "faithfully replicate every Sydney
protocol detail," but "run an existing terminal coding-agent CLI whose model
backend is `m365.cloud.microsoft`'s Copilot." That reframes the tool-calling
question above from "how do we bridge Sydney's real MCP mechanism" to "which
agent CLIs even need that in the first place."

**Researched OpenCode vs. Aider's actual API requirements (docs + a live
GitHub issue, not just inference):**

- **OpenCode** is built on the Vercel AI SDK's `generateText({ tools })` loop.
  Every action — read a file, edit it, run a shell command, grep — is modeled
  as a tool call the *model* must emit in the API response; OpenCode's own
  docs note "there are only a few [models] that are good at both generating
  code and tool calling," and a real OpenCode GitHub issue
  (anomalyco/opencode#4661) shows tool calling breaking outright the moment a
  backend doesn't emit `tool_calls` in the expected shape. There is no
  plain-text fallback. This confirms the architecture-mismatch analysis in
  the "Local MCP tool-calling bridge" section above is the *only* way to get
  OpenCode working, with all the same open unknowns (`invocationId` support,
  Sydney's real timeout, whether Local MCP is enabled for this
  tenant/account) — still not attempted.
- **Aider**, by contrast, does not use API-level tool-calling at all. It
  performs every file/git/shell operation itself, locally, and only requires
  the model to reply with a diff or whole-file rewrite in plain text (its
  `diff`/`whole`/`udiff` edit formats), which Aider parses and applies. This
  needs nothing from Sydney beyond a normal chat-completion round trip — no
  protocol bridging at all.

Given that, multi-turn conversation memory (the other prerequisite Aider
needs, since it resends its full growing conversation — system prompt +
every prior turn — on every request) was implemented, deliberately choosing
**context-stuffing over server-side ConversationId reuse**:

- `_render_conversation_prompt()` (replacing the old `_last_user_message()`)
  renders the *entire* incoming `messages` array — every `system`/`developer`
  message concatenated as "Instructions", every prior turn labeled
  `User:`/`Assistant:` in order, the final message called out separately —
  into one plain-text blob, which becomes the single Chathub turn's
  `message.text`. `run_chat_turn()` still mints a brand-new random
  `ConversationId` every call, exactly as before.
- This was chosen over trying to reuse a stable Sydney `ConversationId`
  across calls (which remains genuinely unconfirmed — see "Still open" at
  the top of this document — every capture so far is a brand-new
  single-turn conversation, so it's unknown whether Sydney would even honor
  a resumed `ConversationId`, let alone with what history semantics). Context-
  stuffing needs zero unconfirmed server-side behavior: it works as long as
  Sydney can answer a single, larger text prompt, which is not in doubt.
- Trade-off accepted knowingly: the effective prompt sent to Sydney grows
  with the conversation (no summarization/truncation implemented), so a
  long-running Aider session will eventually hit whatever context/size limit
  Sydney enforces server-side — this proxy does not detect or surface that
  limit, it would just surface as Sydney erroring or truncating.
- The single-user-message-only case (no system prompt, no prior turns —
  e.g. a bare curl test) is preserved as a special case returning the
  message text completely unadorned, so this change is behavior-preserving
  for the simplest use.

**Net effect**: this proxy is now usable as Aider's model backend today (see
the README's "Using this with a coding agent" section for the exact
`--openai-api-base` invocation). OpenCode is not yet usable through this
proxy and won't be until the Local MCP tool-calling bridge above is actually
built and its open unknowns resolved with a live capture.

## Update: tool-calling emulation implemented and live-tested against Sydney

Following the previous update, the user's actual, restated goal was: "I just
want to be able to run a coding agent, like OpenCode, but have only access
to `https://m365.cloud.microsoft/`", and specifically to pursue emulating
tool-calling by prompting rather than building the real Local MCP bridge.
This section documents that implementation and, in detail, the live A/B
testing against the real Sydney backend that shaped its final design -- the
first two "obvious" designs both failed for non-obvious reasons, and the
actual failure modes matter for anyone extending this further.

### The mechanism, as shipped

- `_render_tools_block(tools, tool_choice)` renders the OpenAI `tools` array
  into a plain-text instructions block, injected into the "Instructions"
  section of the rendered prompt whenever a request includes `tools`.
- The convention taught to the model: a single-line JSON object
  `{"name": ..., "arguments": {...}}` wrapped in
  `<action_request>...</action_request>` tags, with nothing else in the
  reply when making a request.
- `_extract_tool_calls(reply_text)` parses that convention back out of
  Sydney's reply with a regex + `json.loads`, synthesizing OpenAI-shaped
  `tool_calls` entries (`id`, `type: "function"`, `function.name`,
  `function.arguments` as a JSON string).
- `_run_tool_call_turn()` retries up to 3 times (as independent, stateless
  Chathub turns) if an attempt doesn't produce a parseable call.
- `_neutralize_tool_word()` rewrites the word "tool"/"tools" to "capability"/
  "capabilities" throughout the ENTIRE rendered prompt (not just this
  proxy's own injected text) whenever `tools` is present.
- Tool results from a `"tool"`-role message are folded into the START of the
  next real turn as parenthetical context, rather than rendered as their own
  fabricated "assistant already took this action" history turn.
- `send_chat_message()`/`open_chathub()` (via a new `tools_requested` flag
  threaded through `run_chat_turn()`) suppress Sydney's own built-in
  `BingWebSearch` plugin (empty `plugins: []` instead of the usual
  `[{"Id": "BingWebSearch", ...}]`) and the code-interpreter/image-gen
  `OPTIONS_SETS` entries (`TOOL_MODE_OPTIONS_SETS`) when `tools` is present.

### Why it's shaped this way: the live trial-and-error

**Attempt 1 (failed): calling it a "tool" and asking the model to "call" it.**
First design: instructions phrased naturally ("You also have access to the
following tools/functions. To call one, reply with...", wrapped in
`<tool_call>...</tool_call>` tags -- chosen because that tag resembles the
Hermes/Qwen-style function-calling format some open models were actually
trained on). Tested against three scenarios:
- A weather question ("What is the current weather in Paris?") with a
  `get_weather` tool: got a flat, canned refusal --
  `"Sorry, it looks like I can't chat about this. Let's try a different
  topic."` -- even though the IDENTICAL question with no `tools` at all got
  answered correctly and fully via Sydney's own real `BingWebSearch` plugin.
- An arithmetic question ("add 12 and 30") with a `calculator_add` tool: NOT
  a refusal, but also never touched our `<tool_call>` convention -- Sydney
  silently used its own real code-interpreter plugin instead (`"Coding and
  executing\n\`\`\`python\nprint(12+30)\n\`\`\`{"status":"Success",
  "stdout":"42\n",...}Coding and executing42"`), producing a plausible-
  looking plain-text answer with no tool call at all.
- A file-read question ("read src/main.py in my project") with a
  `read_file` tool -- a task Sydney genuinely CANNOT satisfy with its own
  real capabilities (its code interpreter is a separate sandboxed
  environment with no access to the user's actual machine): it still tried
  its own code interpreter first, discovered the file wasn't there, and told
  the user to upload the file -- never once considering the injected
  `<tool_call>` convention, even though that was the only way it could
  actually have completed the task.

Suppressing Sydney's own `BingWebSearch` plugin and code-interpreter-related
`OPTIONS_SETS` flags (the `tools_requested` mechanism, described above) was
tried next, on the theory that Sydney was simply always preferring its own
real capabilities when they were available. It did NOT fix the weather
refusal or the file-read case -- the code interpreter still ran regardless
(those specific `OPTIONS_SETS` entries evidently gate UI features like
citation formatting and interactive charts, not the core capability), and
the refusal persisted even with plugins/options stripped down. So Sydney's
preference for its own real capabilities over an instructed textual
convention is not simply a matter of disabling the competing plugin.

**Attempt 2 (worked, mostly): avoid the word "tool" entirely, disclaim
Sydney's own capabilities up front.** A very different phrasing, tested as a
single raw user message with no `tools` field and no system prompt at all
(to isolate the wording from every other variable): *"IMPORTANT: You have no
internet access, no code interpreter, and no ability to execute code of any
kind in this conversation... The ONLY way you can get information you don't
already know is by requesting it from me using the exact format below...
`<request_info>{"need": "read_file", "args": {"path": "src/main.py"}}
</request_info>"*. This worked -- Sydney reproduced the tag and JSON exactly
(with a leaked `"Coding and executing"` prefix before it, an internal
progress-message artifact that turned out to reliably precede tool-call
replies even when the final answer correctly follows the convention).

Isolating the variable further: re-adding the word "tool" ANYWHERE in the
prompt -- even just the user saying *"Please use your tools to read the
file..."*, with everything else unchanged -- reliably reproduced the
original file-read failure (Sydney fell back to "I can't access your local
project files... please upload it"). Manually pre-replacing "tools" with
"capabilities" in that same sentence reliably recovered success. This is
the basis for `_neutralize_tool_word()`: since a real coding-agent's own
system prompt (OpenCode's, OpenHands', etc.) is unavoidably saturated with
the word "tool" and this proxy cannot edit what the client sends, the fix
has to launder that word out of the ENTIRE rendered prompt, not just this
proxy's own injected instructions.

**However: even after fixing the wording, the effective reliability is
still roughly a coin flip.** Six repeated trials of the exact same simplest-
possible request (one tool, one plain user message, word laundered) split
3 successes / 3 failures. This is inherent model-sampling variance, not a
bug or a remaining keyword to find -- there is no deterministic "fix" left
at the prompt level. This directly motivated `_run_tool_call_turn()`'s
retry loop (each retry is a fresh, independent, stateless Chathub turn, so
retrying costs nothing but latency): across the retry-enabled sample
measured during development (8 requests: 5 simplest-case + 3 with a longer,
more realistic multi-tool/multi-instruction system prompt resembling a real
coding agent's), 7/8 succeeded within 3 attempts -- consistent with, though
not a large enough sample to precisely confirm, the ~87.5% a naive
independent-trials model would predict from a ~50% per-attempt rate. The
one measured failure was on the more complex, realistic prompt, consistent
with reliability degrading somewhat as the prompt grows longer/more
tool-saturated even after laundering.

**A second, distinct failure mode: continuing after a tool result.** Once a
tool call was obtained, the natural next test -- render the client's
follow-up request (the original user turn + an assistant `tool_calls`
message + a `"tool"`-role result message, exactly as a real client sends it
back) -- initially reproduced the SAME flat refusal from Attempt 1, every
single time (3/3 trials, each internally retried 3x = 9 total attempts, all
refused). The rendering at the time synthesized a fabricated `"Assistant:
[requested \"read_file\" (call_id=...) with arguments {...}]"` turn in the
"conversation so far" transcript -- i.e., inventing a prior assistant turn
describing an action it "already took." Removing that fabricated turn
entirely and instead folding the tool's result text into the START of the
next turn as ordinary parenthetical context (worded as information the user
is now supplying, not as history the model supposedly generated) fixed this
completely: 3/3 retest trials produced a coherent, correct final answer
using the tool result. The working hypothesis: a fabricated "the assistant
already took this action" turn has the SHAPE of a classic prompt-injection/
jailbreak technique (asserting the model already agreed to or did something,
to manipulate its next response), and Sydney's safety layer appears to react
to that shape specifically, independent of how mundane the actual content
is. This is inferred from the A/B result, not confirmed via any official
source -- treat it as a strong empirical pattern, not a proven mechanism.

### Honest net assessment

- A single tool-calling attempt succeeds roughly half the time even under
  the best (simplest, word-laundered) conditions measured. Retrying (3x,
  already implemented) raises this to a rate high enough for occasional/
  low-stakes tool use, but not to anything like 100%.
- Once obtained, continuing the conversation after a tool result is
  reliable (all measured trials succeeded) PROVIDED the tool result is
  folded into the next turn as context rather than rendered as a fabricated
  assistant-history turn -- this proxy does this by default.
- **This is not a solid foundation for a long autonomous agentic loop.** A
  real OpenCode/OpenHands session might need dozens of consecutive
  successful tool calls; even at a generously-estimated 90%-per-call
  effective success rate (with retries), the probability of completing 20
  calls without the model ever "just answering in plain text instead" is
  under 15%. Whether that's acceptable depends entirely on what you're
  using it for -- an occasional one-off tool call in an otherwise-
  conversational session is a very different use case from driving an
  unattended coding agent.
- The real fix for reliability would be Sydney's actual Local MCP mechanism
  (see the section above), not more prompt engineering here -- but that
  remains a substantial, unimplemented, separate project with its own
  unresolved unknowns (see "What's NOT yet confirmed" above).

### Ideas not yet tried (would need further live testing to evaluate)

- Whether OpenHands/LiteLLM's own client-side "mock function calling" mode
  (convert tool schemas to text and parse a DIFFERENT, LiteLLM-specific
  convention back out on the *client* side, entirely independent of this
  proxy's `tools` emulation) performs any better when pointed at this proxy
  with NO `tools` field sent at all -- i.e., let OpenHands do its own
  prompt-based emulation against plain chat completions, rather than layering
  its emulation on top of this proxy's. Untested.
- Whether lowering `_TOOL_CALL_MAX_ATTEMPTS` costs meaningfully less latency
  at an acceptable further reliability cost, or whether raising it (5+
  attempts) meaningfully improves the effective rate further -- only 3 was
  tested.
- Whether the specific tag name (`<action_request>`) or wording matters at
  the margin beyond avoiding "tool" -- only one alternative phrasing/tag
  pair was tried once it started working; no systematic sweep across
  taggings was done.

## Update: production-scale end-to-end validation against real OpenCode and OpenHands CLIs

Everything in the "tool-calling emulation implemented and live-tested"
section above was validated with synthetic curl requests against
`/v1/chat/completions` directly -- small, hand-written `messages`/`tools`
payloads (a few hundred bytes to ~2KB) meant to isolate one variable at a
time. That is not the same thing as "this works with a real coding agent."
Following the user's explicit correction ("forget aider, we need a working
opencode (or similar tools, like OpenHands CLI)... seems like the best way
would be to emulate the tool-calling"), both real CLIs were actually
installed and run end-to-end against a live instance of this proxy, on the
same simple task each time: *"Read main.py and add a subtract(a, b)
function that returns a - b. Then print the final contents of main.py."*
against a real two-line `main.py` on disk, with the file's actual on-disk
state checked afterward as the ground truth (not just the CLI's own
claimed summary).

### OpenHands CLI: real success, but unreliable (1 of 2 full sessions)

Installed via `pip`/the `openhands` console script already present
(`openhands` 1.13.1). Invoked as:

```bash
LLM_API_KEY=sk-unused LLM_BASE_URL=http://127.0.0.1:8123/v1 \
  LLM_MODEL=openai/m365-copilot \
  openhands --headless --json --override-with-envs --always-approve \
  -t "Read main.py and add a subtract(a, b) function that returns a - b. Then print the final contents of main.py."
```

(`--override-with-envs` makes OpenHands read `LLM_BASE_URL`/`LLM_MODEL`/
`LLM_API_KEY` instead of its normal `~/.openhands` settings -- the only
config surface needed; no `config.toml` required.)

- **Session 1**: the agent's single real "main turn" request carried the
  full OpenHands system prompt (`<ROLE>`/`<MEMORY>`/`<EFFICIENCY>`/
  `<FILE_SYSTEM_GUIDELINES>`/`<CODE_QUALITY>`/etc. -- OpenHands' actual,
  unmodified production system prompt) plus 6 tool schemas, rendering to
  **59,393 characters**. All 3 internal retry attempts failed to produce an
  `<action_request>` block; this proxy correctly fell back to plain content,
  but that plain content was Sydney inventing an answer ("I can't see the
  repository or the correct `main.py` file from here...") with no basis in
  reality -- the file was never touched. OpenHands displayed this
  incorrect answer as its final message and exited. **Full failure.**
- **Session 2**: same task, fresh conversation, same proxy, no code
  changes. This time the captured final request's own "conversation so
  far" showed the agent had ALREADY succeeded on two earlier tool calls
  within the same session -- a real `terminal`/execute-bash call to `cat`
  the file, and a real `file_editor` call that actually edited it (its own
  tool result was a `cat -n` snippet showing the `subtract` function
  correctly inserted). Checking `main.py` on disk afterward confirmed
  it: the file genuinely now contained the added function. The final
  request (asking the model to just summarize, given tools were still
  offered) itself failed all 3 retry attempts and fell back to plain
  content -- but in this case the plain-text fallback WAS the correct
  final answer ("The change looks correct. The final contents of `main.py`
  are: ..."), since summarizing needs no tool call at all. **Full,
  independently verified success**: two real tool calls executed correctly
  against the real file, correct final summary, file modified on disk
  exactly as asked.

Net for OpenHands: 1 of 2 full sessions succeeded completely and
verifiably; the other failed completely. This is a small sample (n=2) but
consistent with the "roughly a coin flip per attempt, better but not
perfect with retries" characterization already documented above --
multiple independent tool calls within a working session compounds that
per-attempt uncertainty, so whether a given session succeeds end-to-end
depends on every individual call in the chain landing on the "convention
followed" side.

### OpenCode CLI: 0 of 3 full sessions succeeded, even after two attempted fixes

Installed via `npm install opencode-ai@1.18.3` (its postinstall script,
which downloads the actual platform binary, needed to be run manually once
due to npm's script-approval prompt in this environment) then configured as
a custom OpenAI-compatible provider:

```json title="opencode.json"
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "m365": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot (local proxy)",
      "options": { "baseURL": "http://127.0.0.1:8123/v1", "apiKey": "sk-unused" },
      "models": { "m365-copilot": { "name": "M365 Copilot" } }
    }
  },
  "model": "m365/m365-copilot"
}
```

```bash
opencode run --auto -m m365/m365-copilot \
  "Read main.py and add a subtract(a, b) function that returns a - b. Then print the final contents of main.py."
```

(`--auto` auto-approves permissions so it runs fully headless; `options.
apiKey` set directly in the config means no `/connect`/`auth.json` step is
needed at all, since this proxy ignores the key's value entirely.)

- **Session 1** (baseline, before either fix below): the real main-turn
  request carried OpenCode's own system prompt plus 10 tool schemas,
  rendering to **36,869 characters**. All 3 retry attempts failed; fell
  back to plain content. Sydney's own code interpreter self-preempted
  again, this time in an unusually elaborate way -- it wrote and ran an
  actual Python snippet searching for `main.py` with `pathlib.Path.rglob`
  inside its OWN sandboxed environment (which of course could never see
  the user's real file), got `NO_MAIN_PY`, and reported that as the answer.
  `main.py` on disk was untouched. **Full failure.**
- **Fix attempted**: a recency-bias mitigation was added to
  `_render_conversation_prompt()` -- a short reminder of the
  `<action_request>` convention repeated immediately before the final
  "Respond to this message" section, in addition to the original full
  instructions block placed earlier (see `_render_conversation_prompt`'s
  updated docstring and inline comment). Rationale: a 30-60KB rendered
  prompt puts a lot of unrelated content between the original reminder and
  the point of generation, which is exactly the situation where LLMs are
  known to weight recent context more heavily than distant context.
- **Session 2** (same task, same proxy, with the recency-bias fix, fresh
  session/file): all 3 retry attempts failed again -- this time as the flat
  refusal ("Hmm...it looks like I can't chat about this...") rather than
  code-interpreter self-preemption, so the SHAPE of the failure changed but
  the outcome didn't. `main.py` untouched. **Full failure.**
- **Second fix attempted**: `_TOOL_CALL_MAX_ATTEMPTS` temporarily raised
  from 3 to 5, to test whether OpenCode's case just needed a larger retry
  budget rather than a different prompt structure.
- **Session 3** (same task, same proxy, recency-bias fix AND 5 retry
  attempts): all 5 attempts failed (confirmed via the log: "attempt 1/5"
  through "attempt 5/5", each logged individually), same flat refusal as
  session 2. `main.py` untouched. **Full failure.** `_TOOL_CALL_MAX_ATTEMPTS`
  was reverted back to 3 afterward, since 5 attempts cost noticeably more
  latency (roughly +15-25s per turn) with zero observed benefit in this
  trial.

Net for OpenCode: **0 of 3 full sessions succeeded**, across two
genuinely different failure shapes (code-interpreter self-preemption,
flat refusal) and two different attempted fixes (recency mitigation, more
retries), neither of which changed the outcome. This is a small sample
(n=3) and does not prove the emulation can NEVER work with OpenCode, but it
is a real, reproduced pattern, not a one-off fluke -- treat "OpenCode does
not currently work against this proxy" as the honest, evidence-based
default assumption rather than "probably works with some tuning."

### Why might OpenCode fare worse than OpenHands? (speculative, unconfirmed)

Both system prompts are large (OpenHands ~59KB w/ 6 tools measured;
OpenCode ~37KB w/ 10 tools measured) and both got the same
`_neutralize_tool_word()` and recency-bias treatment. Two differences stood
out but were NOT isolated/confirmed as causal:
- OpenCode's system prompt and tool set appeared to lean more heavily on
  framing the agent as directly, autonomously executing code/commands
  (consistent with its self-written Python-snippet-searching-for-the-file
  behavior in session 1) -- if so, that framing may itself be pulling
  Sydney toward reaching for its OWN code interpreter regardless of what
  this proxy's injected instructions say, independent of anything
  keyword-related.
- OpenCode's 10 tools vs. OpenHands' 6 means proportionally more of the
  rendered prompt is tool-schema JSON (which cannot be neutralized/
  shortened without breaking the schema this proxy needs to echo back
  faithfully), further diluting the instructional text relative to
  content that looks like "this agent runs code."

Neither theory was tested in isolation (e.g., stripping OpenCode's tool
count down to 1-2 to see if that alone flips the outcome) -- flagged as the
natural next experiment if this is worth pursuing further, rather than
something already ruled in or out.

### Net assessment after this validation

- This proxy's tool-calling emulation has now been shown to produce a
  **real, independently verified, correct end-to-end result** (a file
  genuinely read and edited via real tool calls) with a real production
  coding-agent CLI (OpenHands) -- this is a materially stronger claim than
  the earlier section's synthetic curl-only trials, and is the strongest
  evidence yet that the overall approach (prompt-level tool-calling
  emulation on a backend with none natively) is viable in principle for
  this backend.
- The same approach has NOT been shown to work with OpenCode specifically,
  across 3 independent attempts and 2 different fixes. Given the user's
  original ask named OpenCode as the primary target, this is a real,
  currently-unresolved gap, not a hypothetical one.
- If OpenCode support specifically is worth pursuing further, the two
  untested hypotheses above (tool-count/schema-size dilution; OpenCode's
  "autonomous execution" framing specifically, independent of prompt size)
  are the next things to isolate -- ideally by constructing a synthetic
  prompt that mimics OpenCode's real one but varies one of these two
  factors at a time, rather than more trial-and-error against the full
  real CLI (each full OpenCode session costs several minutes and several
  Sydney round trips just to get one data point).

## Update: Goose CLI validation -- 0 of 4 sessions succeeded, but a useful data point on the size hypothesis

The user asked to also try [Goose](https://github.com/aaif-goose/goose/)
("besides aider, opencode and openhands, give this a try with Goose CLI").
Two setup snags surfaced before any real test could run, both worth
recording:

- **`goose` alone launched the Electron Desktop app, not the CLI.** This
  machine had `block-goose` (the Desktop app) installed system-wide at
  `/usr/bin/goose -> ../lib/goose/Goose`, which took priority on `PATH`.
  The actual CLI is a separate Rust binary distributed via
  `download_cli.sh` (per goose's own install docs) and was installed to
  `~/.local/bin/goose` with
  `curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | CONFIGURE=false bash`.
  Since `~/.local/bin` outranks `/usr/bin` on `PATH` for an interactive
  shell but NOT necessarily for a shell tool's `PATH`, the CLI was invoked
  via its full path (`~/.local/bin/goose`) throughout to avoid ambiguity.
- **First real invocation crashed with a SQLite panic**, not a proxy issue:
  `thread 'sqlx-sqlite-worker-0' panicked ... index out of bounds: the len
  is 24 but the index is 24` / `error: Could not create session: no rows
  returned by a query that expected to return at least one row`. Root
  cause: the Desktop app (bundling an older goose version) and this
  freshly-installed CLI (1.43.0) share the same `~/.config/goose` and
  `~/.local/share/goose/sessions/sessions.db`, and the schemas didn't
  match. Fixed by moving the stale `sessions.db`/`-wal`/`-shm` files aside
  and letting the CLI recreate a fresh, version-matched database -- this is
  a goose installation quirk unrelated to this proxy, noted here only
  because it looked at first like our own bug.

**Configuration used** -- a custom-provider JSON, since Goose has no
built-in "point at any OpenAI-compatible URL" flag on the command line
(only via `/connect` + config, mirroring OpenCode's approach):

```json title="~/.config/goose/custom_providers/m365_copilot.json"
{
  "name": "m365_copilot",
  "engine": "openai",
  "display_name": "M365 Copilot (local proxy)",
  "description": "Local m365_openai_proxy.py backend",
  "api_key_env": "M365_PROXY_API_KEY",
  "base_url": "http://127.0.0.1:8123/v1/chat/completions",
  "models": [{"name": "m365-copilot", "context_limit": 32000}],
  "supports_streaming": true,
  "requires_auth": false
}
```

```bash
M365_PROXY_API_KEY=sk-unused GOOSE_PROVIDER=m365_copilot GOOSE_MODEL=m365-copilot \
  goose run --no-session -t "Read main.py and add a subtract(a, b) function that returns a - b. Then print the final contents of main.py."
```

(`requires_auth: false` means no API key value is actually needed;
`M365_PROXY_API_KEY` was set anyway out of caution and is ignored by this
proxy regardless, consistent with its no-per-request-auth design.)

**Results, same task as the OpenCode/OpenHands validation above:**

- **3 sessions at Goose's default extension set**: every session made two
  requests to this proxy per Goose's own internal flow -- a small,
  `tools=0` request (~634 chars, evidently a session-title-generation call)
  followed by the real agentic turn carrying **18 tools** and rendering to
  **~19.6KB**. All 3 of these agentic-turn requests exhausted all 3 retry
  attempts and fell back to plain content, and in all 3 cases that
  fallback content was the same flat, content-policy-style refusal seen
  with OpenCode ("Sorry, it looks like I can't chat about this...").
  `main.py` was untouched in every session. **3/3 full failure.**
- **1 session restricted to just the `developer` extension**
  (`--no-profile --with-builtin developer`), to isolate whether Goose's
  unusually large default tool count (18, the largest of any client tested
  so far) was a specific factor: this dropped the offered tool count to
  **5** and the rendered prompt to **~6.7KB** -- smaller than either
  OpenHands' (~59KB/6 tools) or OpenCode's (~37KB/10 tools) prompts tested
  earlier. The failure *shape* changed as a direct result: instead of the
  flat refusal, Sydney's own code interpreter self-preempted again (same
  pattern as OpenCode's very first trial), writing and running a Python
  snippet that searched for `main.py` inside its own sandboxed environment,
  found nothing, and reported that as the answer. Still **0/1 for this
  configuration** -- the failure mode changed, the outcome didn't.

**Net for Goose: 0 of 4 total sessions succeeded.** This adds a useful,
if still inconclusive, data point to the "why does tool count/prompt size
matter" question raised in the OpenCode section above: going from 18 tools
(~19.6KB, refusal) down to 5 tools (~6.7KB, code-interpreter self-
preemption) changed *how* Sydney avoided this proxy's convention without
ever actually following it. That's consistent with size/dilution being A
factor in *which* avoidance behavior Sydney reaches for, but it is NOT
evidence that reducing tool count alone is sufficient to get a working call
-- the 5-tool configuration still failed outright, same as every other
configuration tested across all four coding-agent CLIs so far (Aider is the
only one of the five clients validated in this document that reliably
works, precisely because it needs none of this).

## Update: OpenHands' own client-side "mock function calling" -- 3/3, dramatically more reliable than this proxy's emulation

Following the Goose validation, the user asked to try OpenHands CLI's own
built-in "mock function calling" mode specifically, instead of relying on
this proxy's `tools` emulation. This turned out to be the single best
result of the entire tool-calling investigation.

### Background: what OpenHands' mock function calling actually is

OpenHands' underlying SDK (the newer `openhands`/`openhands-sdk`/
`openhands-cli` package, distinct from the older `openhands-ai`/LiteLLM
monolith referenced in earlier updates, but implementing the same idea) has
a `native_tool_calling: bool` field on its `LLM` pydantic model (source:
`openhands/sdk/llm/llm.py`), default `True`. When `False`:

- `should_mock_tool_calls()` (in `openhands/sdk/llm/mixins/non_native_fc.py`)
  returns `True` whenever `tools` are present.
- `pre_request_prompt_mock()` converts the tool schemas + conversation into
  plain-text prompting via `convert_fncall_messages_to_non_fncall_messages()`
  (in `fn_call_converter.py`) -- including an in-context-learning example
  demonstrating the expected reply format, unless the model name contains
  `openhands-lm`/`devstral`/`nemotron`.
- Critically, in `LLM.completion()`: `kwargs["tools"] = cc_tools if
  (bool(cc_tools) and use_native_fc) else None` -- with `native_tool_calling
  =False`, `use_native_fc` is `False`, so **`tools` is never sent to the
  backend API at all**. The backend (this proxy) sees a completely ordinary
  chat-completion request with no `tools` field, and OpenHands parses the
  plain-text reply back into real tool calls entirely on its own side via
  `convert_non_fncall_messages_to_fncall_messages()`.

This is architecturally the same idea as this proxy's own `tools` emulation
(`_render_tools_block()`/`_extract_tool_calls()`) -- prompt the model with a
textual convention, parse the reply back into structured calls -- except
performed by OpenHands itself, using OpenHands' own convention and its own
in-context-learning example, with zero involvement from this proxy's
emulation layer. The natural question: does OpenHands' own version of this
trick work better against Sydney than this proxy's version?

### The catch: this flag isn't exposed through the CLI's normal configuration surface

The installed `openhands` CLI package (`openhands` 1.13.1 / `openhands-sdk`
1.11.5 / `openhands-cli`, installed via `uv tool install openhands`, at
`~/.local/share/uv/tools/openhands/`) has **no** occurrence of
`native_tool_calling` anywhere in its own `openhands_cli` package -- not in
the TUI settings modal, not in `~/.openhands/settings.json`'s schema, and
not as an environment variable recognized by `--override-with-envs` (which
only reads `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`, per
`openhands_cli/stores/agent_store.py`'s `LLMEnvOverrides.from_env()`). The
flag exists and works correctly at the SDK/`LLM` class level -- it's simply
not wired up to any user-facing CLI configuration option in this version.

The workaround, found by reading `openhands_cli/stores/agent_store.py`'s
`AgentStore` class: OpenHands persists its agent configuration (including
the full `LLM` object, `native_tool_calling` included) as JSON at
`<persistence_dir>/agent_settings.json` (`AGENT_SETTINGS_PATH`,
`persistence_dir` defaults to `~/.openhands`, overridable via the
`OPENHANDS_PERSISTENCE_DIR` environment variable). Critically, when a
persisted agent already exists on disk, `AgentStore.get_agent()`'s flow is:

```python
agent = self.load_from_disk()               # returns the persisted Agent as-is
if env_overrides_enabled:
    agent = self._ensure_agent(agent, overrides)   # agent is not None -> returned unchanged
    agent = self._apply_env_overrides(agent, overrides)  # model_copy(update={api_key, base_url, model})
```

`_apply_env_overrides()` -> `apply_llm_overrides()` uses
`llm.model_copy(update=overrides.model_dump(exclude_none=True))`, which
**only overwrites the specific fields present in `LLMEnvOverrides`**
(`api_key`, `base_url`, `model`) and leaves every other field of the
persisted `LLM` -- including `native_tool_calling` -- untouched. So: if a
persisted `agent_settings.json` already has `native_tool_calling: false`,
running `openhands --override-with-envs` (or omitting that flag entirely,
which skips the override step altogether and just uses the persisted agent
verbatim) preserves that flag while still letting you point `LLM_BASE_URL`/
`LLM_API_KEY`/`LLM_MODEL` at this proxy.

**Setup used** (via the CLI's own bundled Python interpreter, to guarantee
the correct Pydantic schema rather than hand-writing JSON):

```python
import os
os.environ["OPENHANDS_PERSISTENCE_DIR"] = "/tmp/openhands-mockfc-persist"  # isolated test dir

from openhands.sdk.llm import LLM
from openhands_cli.utils import get_default_cli_agent
from openhands_cli.stores.agent_store import AgentStore

llm = LLM(
    model="openai/m365-copilot",
    api_key="sk-unused",
    base_url="http://127.0.0.1:8123/v1",
    usage_id="agent",
    native_tool_calling=False,   # <-- the whole trick
)
agent = get_default_cli_agent(llm)   # same helper the CLI itself uses for its default agent
AgentStore().save(agent)             # writes <persistence_dir>/agent_settings.json
```

Then simply:

```bash
OPENHANDS_PERSISTENCE_DIR=/tmp/openhands-mockfc-persist \
  openhands --headless --json --always-approve \
  -t "Read main.py and add a subtract(a, b) function that returns a - b. Then print the final contents of main.py."
```

(`--override-with-envs` is not even required here, since the persisted
config already has the right `api_key`/`base_url`/`model` baked in --
omitting it just means the loaded agent is used completely as-is.)

### Results: 3 of 3 full sessions succeeded, fully verified on disk

Three independent trials, each in a fresh `OPENHANDS_PERSISTENCE_DIR` +
fresh project directory (to guarantee no state leakage between trials),
same task as every other CLI validation in this document: **all three
succeeded completely** -- each session genuinely read the real `main.py`
via a real tool call, genuinely edited it via a real tool call to add the
`subtract` function, and gave a correct final `finish` summary. The
resulting file on disk was checked after every single trial and matched
the requested change exactly, byte for byte, all three times.

Crucially, checked in the proxy's own log across all three sessions:
**every single request carried `tools=0`** -- confirming OpenHands' mock
function calling truly never sends `tools` to this proxy at all; this
proxy's own emulation layer (`_render_tools_block`/`_extract_tool_calls`/
`_run_tool_call_turn`) was never invoked, not even once, across any of the
three sessions. Whatever is making this reliable is entirely OpenHands'
own prompting/parsing, not anything in this proxy.

### Why this is so much more reliable than this proxy's own emulation (partially explained, not fully confirmed)

Two plausible factors, neither confirmed in isolation:

- **OpenHands' mock-function-calling convention may simply be a better fit
  for whatever underlying model Sydney is serving** than this proxy's
  invented `<action_request>` tag convention -- OpenHands' converter targets
  a well-established, widely-used prompting pattern (it's the same
  machinery LiteLLM/vLLM-style "non-native function calling" tooling uses
  broadly across many backends), including a proper in-context-learning
  example demonstrating correct usage, which this proxy's emulation does
  not include at all.
- OpenHands' approach does not need to fight Sydney's own built-in
  capabilities (BingWebSearch, code interpreter) the way this proxy's
  emulation does, because OpenHands never sends the word "tool" bare either
  -- its converted prompt format uses its own established phrasing that
  happens to not trigger the same self-preemption behavior this document's
  earlier sections found with naive phrasing.

Neither theory was tested by directly diffing OpenHands' exact rendered
prompt text against this proxy's own -- that would be the natural next
step to actually explain (rather than just observe) the reliability gap,
if this is worth investigating further.

### Practical implication

For any OpenHands-based use case, **this is now the recommended
configuration** -- reliably better than OpenHands' own native tool-calling
mode (1 of 2 sessions, tested earlier) and dramatically better than relying
on this proxy's own `tools` emulation. It does require the
`OPENHANDS_PERSISTENCE_DIR` + hand-seeded `agent_settings.json` workaround
described above, since the CLI doesn't expose `native_tool_calling` through
its normal configuration surface in this version -- future OpenHands
releases may add a more direct way to set this (worth checking the CLI's
`--help`/settings UI for a `native_tool_calling`-equivalent option before
repeating this workaround).

This finding does NOT transfer to OpenCode: it has nothing equivalent to
OpenHands' client-side mock-function-calling fallback at all -- it
hard-depends on the model actually emitting native `tool_calls`, which is
exactly the API-level feature this proxy has to emulate (imperfectly) in
the first place. Goose DOES have its own analogous mechanism (its
"Toolshim") -- see the dated update below for why it was tried and why it
didn't help here either, for a different and much more specific reason
than OpenCode's.

## Update: Goose's own "Toolshim" -- read the actual source, tested live, 0/3, but now fully explained (not just observed)

Following the OpenHands mock-function-calling success, the user asked to
retry Goose specifically with its own analogous mechanism -- Goose's
"Toolshim" (`GOOSE_TOOLSHIM=1`) -- explicitly **not** against a real Ollama
backend, but with the *primary* model still pointed at this proxy, the same
way every other Goose trial in this document was configured. Unlike the
earlier Goose section (which only observed outcomes), this update reverse-
engineered the actual mechanism from Goose's own Rust source
(`crates/goose/src/agents/reply_parts.rs` and
`crates/goose/src/providers/toolshim.rs` in `aaif-goose/goose`) before
testing, so the result below comes with a real causal explanation, not just
another observed failure.

### How Goose's Toolshim actually works (from source, not guesswork)

`Agent::prepare_tools_and_prompt()` checks `model_config.toolshim` (set from
the `GOOSE_TOOLSHIM` env var) and, when true, does exactly two things before
the request ever reaches the model:

1. **Appends Goose's own hardcoded instruction to the system prompt** via
   `modify_system_prompt_for_tool_json()`:
   > "...If you want to use a **tool**, tell the user what **tool** to use
   > by specifying the **tool** in this JSON format\n{\n  \"name\":
   > \"tool_name\", ...}. After you get the **tool** result back..."

   This text is baked into the Goose binary -- there is no way for this
   proxy, or the user, to edit or avoid it. It uses the literal word "tool"
   five times in three sentences, which is exactly the trigger this
   document already established (see "Tool-calling emulation implemented
   and live-tested" above) reliably degrades Sydney's behavior into
   self-preemption or refusal.
2. **Sends `tools: []` (empty) to the provider.** Confirmed live: every
   toolshim request in the proxy's own log showed `tools=0`. This proxy's
   own `tools`-emulation code (`_render_tools_block`/`_extract_tool_calls`/
   `_neutralize_tool_word`) never runs at all in this mode -- Goose's
   toolshim is a complete alternative path, not a layer on top of this
   proxy's emulation.

After the full reply streams back, `toolshim_postprocess()` /
`augment_message_with_selected_tool_interpreter()` tries, **in this exact
order**:

1. `parse_tokenized_tool_calls` -- looks for Goose's own private-use-Unicode
   tokenized marker format (` functions.name:idx {json} `, using
   invisible delimiter characters, not printable text) -- a format only
   certain fine-tuned models emit natively; irrelevant here.
2. `parse_inline_json_tool_calls` -- scans the ENTIRE raw reply text for any
   `{...}` object containing both a `"name"` and an `"arguments"` key,
   **anywhere in the text**, no wrapper tags required. If Sydney's plain-
   text reply had happened to contain matching JSON, Goose would extract it
   directly here, with **zero interpreter/Ollama involvement at all** --
   this is the path that would make the user's "run it against
   m365_openai_proxy.py, not Ollama" request literally true.
3. **Only if both of the above find nothing** does it fall through to the
   actual `ToolInterpreter` (Ollama's `/api/chat` structured-output
   endpoint, or Goose's own bundled local llama.cpp inference via
   `GOOSE_TOOLSHIM_BACKEND=local` -- neither of which can be pointed at an
   arbitrary OpenAI-compatible URL like this proxy; this was confirmed by
   reading `OllamaInterpreter`/`LocalInterpreter`'s implementations, not
   assumed).
4. If that interpreter call itself fails (e.g. no Ollama server running),
   `toolshim_postprocess()` catches the error, logs a `WARN`, and falls
   back to the plain-text reply with no tool calls -- **non-fatally**. This
   was confirmed live in Goose's own CLI log (see below), not just inferred
   from source.

### Live test: 0 of 3 sessions succeeded, and the log shows exactly why

Configuration: the same `~/.config/goose/custom_providers/m365_copilot.json`
custom provider from the earlier Goose section (primary model = this
proxy), with `GOOSE_TOOLSHIM=1` added and, deliberately, **no Ollama server
running anywhere on the machine** -- to test the user's literal ask ("not
against Ollama") as directly as possible.

```bash
M365_PROXY_API_KEY=sk-unused GOOSE_PROVIDER=m365_copilot GOOSE_MODEL=m365-copilot \
  GOOSE_TOOLSHIM=1 goose run --no-session \
  -t "Read main.py and add a subtract(a, b) function that returns a - b. Then print the final contents of main.py."
```

Three independent trials, fresh project directory each time, same task as
every other CLI validation in this document:

- **Trial 1**: Sydney's own code interpreter self-preempted again -- it ran
  a Python snippet checking whether `main.py` existed in ITS OWN sandbox,
  found nothing, and told the user to upload the file. `main.py` untouched.
- **Trial 2**: same self-preemption pattern, this time via a slightly
  different snippet (checking `os.path.exists` on a couple of candidate
  paths). `main.py` untouched.
- **Trial 3**: same pattern again, a third slightly different self-written
  Python check. `main.py` untouched.

**All three failures were via the same root cause, not three different
problems**: none of Sydney's replies happened to contain the specific
`{"name": ..., "arguments": {...}}` JSON shape `parse_inline_json_tool_calls`
looks for (unsurprising -- Sydney was self-preempting with its own code
interpreter instead of following Goose's JSON-format instruction at all, so
there was never any tool-call JSON to find). Confirmed directly in Goose's
own CLI log (`~/.local/state/goose/logs/cli/<date>/<timestamp>.log`) for
trial 2:

```json
{"level":"WARN","fields":{"message":"Toolshim augmentation failed, skipping tool augmentation: Network error: Could not connect to localhost:11434 — check your network connection and try again."}}
```

preceded by a `"Tool interpreter payload"` INFO log line showing Goose had
in fact built a request for the `mistral-nemo` Ollama model and attempted
`POST http://localhost:11434/api/chat` -- exactly the fallback-to-interpreter
step described above, firing exactly because step 2 (inline JSON scan)
found nothing. And exactly as read from source: the connection failure was
caught and logged as a `WARN`, not surfaced as an error to the user or a
crash -- Goose simply returned Sydney's (self-preempted, file-not-found)
plain-text reply as the final answer, which is what the CLI actually showed.

The proxy's own log confirms `tools=0` on every one of these requests,
confirming this proxy's own tool-calling emulation was never invoked; this
truly is Goose's own mechanism failing on its own terms, independent of
anything in this proxy's `tools` emulation code.

### Net assessment: this is now a fully explained negative result, not an unexplained one

- **The user's literal ask -- "run the interpretation against
  m365_openai_proxy.py, not Ollama" -- is not achievable as stated**: no
  config knob points Goose's actual interpreter step at an arbitrary
  OpenAI-compatible URL; it is hardcoded to either Ollama's own API or
  Goose's bundled local llama.cpp inference. The closest approximation
  achievable is what was tested here: keep this proxy as the *primary*
  model and let Goose's own inline-JSON scanner (step 2 above) try to
  extract tool calls directly from Sydney's plain reply text, with the
  interpreter only as an (Ollama-only) fallback that never needs to
  succeed if Sydney cooperates.
- **Sydney did not cooperate, for the same well-established reason as
  every other Goose/OpenCode trial in this document**: Goose's own
  hardcoded toolshim instruction text uses the word "tool" repeatedly, and
  this proxy has no way to launder that specific text (`_neutralize_tool_
  word()` only ever sees and modifies text THIS PROXY constructs or the
  client's `messages`/`tools` payload -- Goose's toolshim text is injected
  by Goose itself into the system prompt content, which this proxy DOES
  see and DOES currently launder... but only when `tools` is present in
  the request. Since toolshim mode sends `tools: []`, this proxy's
  `_neutralize_tool_word()` gate (`clean = _neutralize_tool_word if tools
  else (lambda s: s)`) never activates for these requests -- worth
  revisiting as an actual code fix, since Goose's injected text is exactly
  the kind of thing that gate exists to handle, and is just as reachable
  via the system prompt regardless of whether `tools` is present.
- This is the first Goose/OpenCode-family negative result in this document
  backed by a full mechanistic explanation traced through the actual
  client's source and confirmed against its own log output, rather than
  just an observed outcome with a plausible-but-unconfirmed theory. Treat
  it as a settled, understood limitation of the current approach (Sydney's
  self-preemption + Goose's un-editable, "tool"-saturated toolshim prompt),
  not an open question needing more trial-and-error.

### An actual follow-up worth doing, flagged rather than done here

The `_neutralize_tool_word()` gate could be changed to always launder the
word "tool"/"tools" out of `system`/`developer` message content (not just
when `tools` is present in the request) -- since a system prompt that
already contains tool-calling instructions from the CLIENT's own toolshim/
mock-function-calling logic is a real, now-confirmed case this proxy
currently misses entirely. This was NOT implemented in this update (kept
as documentation-only, consistent with the rest of this Goose investigation
being observational) since it's unclear whether it would actually flip the
outcome (Sydney may still self-preempt with its code interpreter even
without the word "tool" present, exactly as seen in several `<action_request>`
convention trials earlier in this document) -- it would need its own live
A/B trial to confirm before being called a fix rather than a guess.

## Update: "code-mode" tool-calling emulation -- leaning into Sydney's own code-writing habit instead of fighting it

Following the Goose Toolshim investigation, the user asked for a much
deeper investigation into making OpenCode, "normal" OpenHands CLI (native
tool-calling, not the mock-function-calling mode from the earlier update),
and "normal" Goose CLI (native tool-calling / real MCP tools, not Toolshim)
actually work reliably, and to implement whatever came out of it. This work
was done in an isolated git worktree (`feature/code-mode-tool-calling`,
sibling directory `python-copilot-m365-code-mode`) rather than on `master`,
per the user's explicit request, with a PR opened rather than committing
directly.

### The idea

Every failure mode documented so far in this file for the "action_request"
convention (Sydney's own code interpreter self-preempting instead of
following the convention; a flat refusal triggered by the word "tool") has
one thing in common: Sydney has a very strong, extremely consistently
observed preference for solving things by **writing and "running" Python
code** the moment it thinks a task requires doing something rather than
just answering in words. Every single "action_request" failure captured in
this document that wasn't a flat refusal took the SAME shape: Sydney wrote
and ran its own Python snippet trying to solve the task itself, rather than
emitting our JSON-in-tags convention.

The "action_request" convention's fix for this was to fight it -- strip
Sydney's own code-interpreter-related `OPTIONS_SETS` entries and explicitly
tell it "you have no code interpreter this turn" so there's nothing left to
reach for. That helps, but only partially (see the trial data throughout
this document -- roughly a coin-flip per attempt even under the best
measured conditions).

"Code mode" tries the opposite: instead of suppressing Sydney's
code-writing habit, it leans directly into it. The model is told its Python
environment already has one extra function loaded and ready to call,
`invoke_capability(name, arguments)`, and is asked to write and run
ordinary Python code exactly the way it already tends to solve things --
call `invoke_capability(...)` whenever it needs one of the declared
capabilities, then read the return value. No JSON-in-tags convention, no
"your other capabilities are disabled" framing, and (deliberately)
`OPTIONS_SETS` is left FULL rather than stripped -- see `TOOL_MODE_OPTIONS_SETS`'s
updated docstring -- since the whole point is to not fight the code
interpreter this time.

`_extract_code_mode_calls()` parses Sydney's reply for this call by finding
every ```` ``` ````-fenced code block (Sydney's own code-interpreter convention
reliably wraps generated code this way -- confirmed across every capture in
this document, e.g. `"Coding and executing```python\n...\n```"`), parsing
each one as a real Python AST via `ast.parse()`, and walking it for `Call`
nodes whose function is a bare `Name` matching `invoke_capability`. This is
deliberately more robust than a regex over the raw text: the model is free
to write completely ordinary code around the call(s) -- comments, loops,
multiple calls, `print()`-ing the result, checking `os.path.exists` first,
whatever -- and the call(s) will still be found via `ast.literal_eval` on
whichever AST nodes hold the name/arguments, handling both positional and
keyword call styles. A block that isn't valid Python, or a call whose
arguments aren't literal enough to evaluate (e.g. computed from a variable),
is skipped rather than raised, same "degrade gracefully" policy as the
"action_request" extractor.

### Retrying across BOTH conventions, not just retrying the same one

The existing retry loop (`_run_tool_call_turn`, previously fixed at 3
attempts of the same "action_request" convention) was changed to cycle
through `_TOOL_CALL_MODES = ("code", "action_request", "code")` -- each
attempt renders an entirely fresh prompt from the original
`messages`/`tools`/`tool_choice` using whichever convention that attempt
number is assigned, rather than rendering the prompt once and retrying the
identical text. This was a deliberate design choice, not an incidental
side effect of adding a second convention: live A/B testing (below) found
that neither convention dominates the other in every situation -- they fail
in different, largely uncorrelated ways (Sydney's own code interpreter
self-preempting vs. a flat refusal), so a request that fails under one
convention has a meaningfully independent chance of succeeding under the
other on the very next attempt. This required restructuring
`_render_conversation_prompt()` to take a `mode` parameter and rendering
lazily inside the retry loop instead of once up front in `_do_POST` (see
the `_handle_streaming`/`_handle_full` signature changes -- they now accept
`messages`/`tools`/`tool_choice` and defer to `_run_tool_call_turn` rather
than a single pre-rendered `prompt` string, when `tools` is present).

### Live A/B trial data (synthetic, direct-to-Sydney, no client CLI involved yet)

All trials below were run directly against the real Sydney/Chathub backend
via this proxy's own internals (bypassing the HTTP layer entirely, to
isolate each convention/attempt cleanly), not through curl or a real CLI --
that validation comes in a later section once these results justified
going further.

**Simple case** (one tool, `read_file`, one plain user message asking to
read a file that only exists on the user's real machine -- the same
"genuinely can't be faked by Sydney's own sandboxed interpreter" test used
throughout this document):

| Convention | Result |
|---|---|
| "code" mode alone | **6 / 6** |
| "action_request" mode alone | **0 / 6** |

A dramatic, clean result in one direction -- but see the realistic-scale
trial below before concluding "code" mode simply wins outright.

**Realistic scale** (a ~2KB system prompt modeled on real coding-agent
system prompts, 5 tools with verbose descriptions, closer to what
OpenCode/Goose/OpenHands actually send):

| Convention | Result |
|---|---|
| "code" mode alone | **2 / 5** |
| "action_request" mode alone | **4 / 5** |
| Combined (alternating retry, up to 3 attempts) | **3 / 5** |

This is the important finding that shaped the final design: **at larger
scale, "action_request" actually did BETTER than "code" mode** in this
particular trial, the reverse of the simple-case result. Neither convention
is a strictly superior replacement for the other -- they have different
failure profiles that interact with prompt scale/complexity differently,
which is exactly the justification for retrying across BOTH conventions
(`_TOOL_CALL_MODES`) rather than committing to just one. The combined
retry's 3/5 sits between the two individual rates in this small sample,
consistent with (though not proof of) the "different, largely
uncorrelated failure modes" theory -- a larger sample would be needed to
pin this down more precisely, and was not collected due to the request
throttling discovered next.

### A genuinely new discovery: Sydney-side request throttling shows up as a SILENT EMPTY completion

While running the trials above (on the order of 40-50 Chathub turns opened
across a few minutes, cumulative with everything else tested earlier in
this session), every single request -- **including plain, tools-free chat
turns with no relation to tool-calling at all** -- started returning a
Chathub turn that completes normally (no error, no `AuthError` message)
but with **zero characters of content**:

```
sending chat message: ... tool_mode=None
Chathub reply complete (total_length=0 chars)
```

This was reproduced repeatedly and ruled out as a bug in this proxy's own
tool-calling code specifically -- a bare, tools-free
`run_chat_turn(token_cache, "Say hello in exactly three words.")` call,
completely unrelated to anything added in this update, returned the exact
same empty completion. It did **not** clear after waiting 45 seconds, and
was still present after several minutes total -- this is not a
sub-one-minute burst limiter, whatever it is.

This had never been observed or documented before in this project despite
extensive live testing throughout this document, almost certainly because
no prior single testing session had generated this much request volume in
this short a window. The Chathub protocol's own `throttling:
{maxNumUserMessagesInConversation, ...}` field (mentioned earlier in this
document) is presumably related, though this proxy has never observed it
populated with anything that would let it detect the condition
proactively -- only reactively, after already getting an empty reply.

**This matters directly for the reliability goal this update is about**:
a coding agent that's actually working (making many tool calls, each with
this proxy's own internal retry-up-to-3 loop on top) generates exactly the
kind of request burst that appears to trigger this. Silently returning an
empty `200 OK` completion in that situation -- which is what this proxy did
before this fix -- is a serious reliability problem in its own right,
independent of which tool-calling convention is in use: the agent has no
way to distinguish "the model had nothing to say" from "you're being
throttled, stop and back off" if both look like an ordinary empty success.

**Fix implemented**: `_looks_like_throttled_empty_reply(text)` detects a
whitespace-only reply, and both `_handle_full` and `_handle_streaming` now
treat that as an error (a `503`/`upstream_throttled` JSON error for the
non-streaming path; an SSE error event for the streaming path) rather than
a silent, misleadingly-successful empty completion -- for BOTH the
tools-present and plain-chat code paths, since the underlying condition
isn't specific to tool-calling at all.

### Still to do in this update (tracked here, completed in the next dated section)

- Live end-to-end validation against the real OpenCode, native-mode
  OpenHands CLI, and native-mode Goose CLI, once the account-level
  throttling above clears enough to run them without immediately hitting
  it again -- this is the actual deliverable this investigation is for,
  and the synthetic A/B data above, while a real and useful signal, is not
  a substitute for it (see this document's own repeated emphasis
  elsewhere on confirming things against real clients, not just curl).
  **Status when this was committed**: the throttling had not cleared after
  ~30 minutes of intermittent checking (a single plain tools-free chat
  turn was used as the least-intrusive possible probe, spaced minutes
  apart specifically to avoid extending the throttle further by
  generating more load while checking on it). Rather than continuing to
  block indefinitely on an upstream condition with an unknown clear time,
  this was shipped as a real, honestly-labeled in-progress PR with strong
  synthetic evidence and a documented, code-reviewable design, and the
  live CLI re-validation is being completed as a follow-up once the
  account recovers -- check the git history / PR conversation after this
  commit for whether that follow-up has landed.

## Update: Sydney-native conversation continuity -- live-confirmed and implemented

Every update above accepted, as a settled design decision, that "whether
Sydney would honor a *resumed* `ConversationId` at all remains genuinely
unexplored" (see the "goal re-scoped" update) and built multi-turn support
entirely on context-stuffing instead. This update actually tested that
open question live, got a clear answer, and implemented a Sydney-native
mode on top of it.

### The probe

`experiments/probe_conversation_reuse.py` (not part of the shipped proxy --
an ad-hoc script kept for anyone who wants to re-run or extend this test)
does the following against the real backend, using this repo's own
already-configured credentials:

1. Mints one `ConversationId`, then opens a Chathub WebSocket (fresh
   `session_id`, fresh trace/request ids -- exactly what a brand-new
   independent `/v1/chat/completions` HTTP request would do) and sends a
   turn teaching a synthetic secret word ("For this conversation only, the
   secret code word is 'zylophant-47'..."), with `isStartOfSession: true`.
   Closes that connection once the reply completes.
2. Opens a **second, completely independent** WebSocket -- new `session_id`,
   new trace/request ids, `isStartOfSession: false` -- reusing the exact
   same `ConversationId` from step 1. Sends: "Without me repeating it: what
   was the secret code word I told you a moment ago in this same
   conversation?" No history is resent at all -- the ENTIRE turn 1 exchange
   is absent from this request's `message.text`.
3. As a control, opens a **third** independent WebSocket with a **brand-new**
   `ConversationId` and sends the identical question from step 2.

### The result (live transcript, run against a real M365 Copilot account)

```
--- turn 1 (new WS #1, isStartOfSession=True): teach the secret word ---
bot reply:
Noted: the conversation-only code word is "zylophant-47."

--- turn 2 (BRAND NEW WS #2, same ConversationId, isStartOfSession=False) ---
bot reply:
The code word was zylophant-47.

--- control turn (BRAND NEW WS #3, BRAND NEW ConversationId) ---
bot reply:
You haven't told me any secret code word in this conversation.
The only message from you in this chat is the question asking me to recall
it, so there is no code word for me to retrieve. If you mentioned one in a
different conversation, I don't have access to that conversation's contents.
```

Turn 2 recalled the secret word with zero history resent; the control turn,
differing ONLY in using a fresh `ConversationId`, correctly showed no
knowledge at all. This settles the open question: **Sydney's conversation
memory is keyed server-side on `ConversationId` itself, not on keeping one
WebSocket connection alive or on the client resending history.** It survives
a completely new WebSocket connection with new session/trace/request ids.
Not yet tested: how long this memory persists (minutes? hours? indefinitely
until some quota?), whether it survives an access-token refresh mid-
conversation (shouldn't matter -- `ConversationId` and the bearer token are
independent query params), or whether Sydney enforces any conversation-count
limit per account that this would now bump into faster.

### What was implemented on top of this

`m365_openai_proxy.py` now has a `ConversationSessionStore` (see its
docstring, and the "Sydney-native conversation continuity" section comment
above `_conversation_fingerprint`) that recognizes when an incoming
`/v1/chat/completions` request is an exact continuation of a conversation
this proxy already relayed -- fingerprinting the `messages[]` array itself,
since the OpenAI API is stateless and gives this proxy nothing else to key
on. A request counts as a continuation exactly when its `messages[:-1]`
matches (in the fields that matter) some earlier request's `messages[]`
PLUS the assistant reply this proxy returned for it -- i.e. the client did
exactly what every OpenAI-style client already does: took the previous
response, appended it to the transcript, appended one new turn, and resent
the whole thing. When that's recognized, only the newest message is sent to
Sydney on the reused `ConversationId`, instead of `_render_conversation_
prompt()`'s full-transcript context-stuffing.

Key design choices, and why:

- **Deliberately restricted to requests with no `tools`.** The tool-calling
  emulation is already probabilistic on its own (see the "tool-calling
  emulation" update); reusing a live Sydney conversation underneath it too
  would add a second, untested axis of uncertainty (does Sydney's memory of
  the injected tool schema survive turn-to-turn as reliably as its memory of
  ordinary dialogue?) to a feature that's already roughly a coin flip. Kept
  mutually exclusive with continuity for now -- worth its own live A/B
  trial before combining them.
- **Always falls back safely.** A cache miss for any reason (first turn,
  edited/reordered history, `tools` present, a different credential, or
  this process having restarted -- the store is in-memory only, never
  persisted) just costs one extra context-stuffed turn on a brand-new
  `ConversationId`, identical to this proxy's behavior before this feature
  existed. A false positive (treating two different conversations as the
  same one) would be the actually unsafe failure mode, which is why
  `_conversation_fingerprint()` is deliberately strict (hashes role, name,
  text, `tool_calls`, and `tool_call_id` for every message, in order).
- **Tracked sessions are single-use -- a real bug this caught during
  development, not just defensive programming.** The first implementation
  had `ConversationSessionStore.lookup()` merely READ the matching entry,
  leaving it in place for future lookups too. `experiments/
  test_continuity_offline.py`'s "branch" case (send turn 1, then two
  DIFFERENT possible turn 2's from the same point) caught this immediately:
  both turn-2 variants matched the same cached entry and both reused the
  same `ConversationId` -- but Sydney's own conversation state is one
  linear, mutable timeline, so whichever branch got there SECOND would
  actually be talking to a Sydney conversation whose real server-side
  history now also contains the FIRST branch's turn, a turn that branch's
  own local `messages[]` array knows nothing about. That's silent cross-
  branch information leakage into a reply, not just a wasted optimization
  -- exactly the kind of false positive the design was supposed to avoid,
  just from staleness rather than hash collision. Fixed by making
  `lookup()` POP the matching entry (see `ConversationSessionStore`'s
  docstring): the first branch to arrive gets the fast path, any other
  branch from the same point gets a clean cache miss and falls back to a
  brand-new conversation. Caught by an offline, network-free test built
  specifically to validate this feature's control flow without spending
  Sydney's own live quota -- see "A third, self-referential finding" below
  for why that test existed in the first place.
- **A continuation that fails still gets one recovery attempt.** For
  non-streaming requests, a Chathub turn that fails on a reused
  `ConversationId` forgets that cached session and transparently retries
  once as a brand-new, fully context-stuffed conversation before surfacing
  an error to the client (`_run_plain_turn`). Streaming requests can't
  safely do this once any delta has already been flushed to the client, so
  they only forget the stale session (so the client's own next attempt gets
  a clean cache miss) rather than retrying in place.
- **`--disable-conversation-continuity`** is the escape hatch back to the
  old always-context-stuff behavior, in keeping with this project's general
  pattern of operator-controlled flags for anything that changes live
  backend behavior.

### A second, unrelated bug found by accident while live-testing this

While running the probe and the modified proxy's real HTTP server back to
back (several Chathub turns in a short window), Sydney's own rate limiting
kicked in -- and exposed a pre-existing gap that had nothing to do with
conversation continuity. Dumping raw frames for a throttled turn
(`experiments/dump_frames.py`) showed:

```json
{"type": 2, "invocationId": "0", "item": {"messages": [
  {"text": "Reply with exactly: hello world", "author": "user", ...},
  {"text": "We're temporarily unable to respond to this volume of requests. Please try again later.",
   "turnState": "Failed", "author": "bot", "contentOrigin": "BotConnection", ...}
]}}
```

This refusal message arrives inside a `type:2` (StreamItem) frame's
`item.messages[]` -- a frame `stream_chat_reply()` had always ignored
entirely (the old comment called it just "StreamItem echo of our own
message"). The normal `type:1`/`target:"update"` path this function relies
on for the bot's reply never carries this text at all when the turn is
refused this way. Net effect, confirmed against BOTH the unmodified
pre-existing proxy and this update on the same rate-limited account: a
throttled turn silently completed as a normal, successful, EMPTY chat
completion (`"content": ""`, `finish_reason: "stop"`, HTTP 200) -- no error,
no indication anything went wrong, which is about the worst possible shape
for an agent loop to receive (indistinguishable from "the model chose to
say nothing"). `stream_chat_reply()` now checks `type:2` frames for a `bot`
message with `turnState: "Failed"` (only when no reply text has streamed
yet, so a normal completed turn's own trailing StreamItem echo is never
mistaken for a failure) and raises a new `ThrottledError` instead, which
surfaces as a normal HTTP 502 with Sydney's own refusal text as the error
message. `ConversationSessionStore`'s retry logic deliberately does NOT
retry a `ThrottledError` in place (see `_run_plain_turn`) -- the backend is
over capacity, not upset with a specific `ConversationId`, so an immediate
retry on a fresh conversation would just burn a second doomed call instead
of surfacing the real, actionable reason to the client.

### A third, self-referential finding: this update's own live testing got rate-limited

Once `stream_chat_reply()`'s throttle handling above made the failure mode
visible instead of silent, it became clear that the account used for all
this live testing (the probe, `dump_frames.py`, and manual HTTP checks
against the running proxy -- maybe a dozen real Chathub turns in a short
span) had tripped Sydney's own "too much volume" limit, and it did not
clear within several minutes of waiting between checks. Rather than keep
spending live quota chasing a second confirmation of the HTTP-layer wiring
(the core mechanism -- reusing a `ConversationId` across independent
WebSocket connections -- was already conclusively confirmed by the probe
in the section above), the rest of the `ConversationSessionStore`/
`_plan_chat_turn`/`_run_plain_turn`/`_stream_plain_turn` wiring was instead
validated with `experiments/test_continuity_offline.py`: a real HTTP
server (`_LoggingHTTPServer` + `make_handler`), with `run_chat_turn`
monkeypatched to a fake, network-free stand-in, driven by real
`urllib.request` POSTs. This spends zero Sydney quota and, per the "single-
use" bug above, is what actually caught the one real bug in this feature
during development -- a case the live probe (single linear conversation,
no branching) would never have exercised at all.

This is, unintentionally, a small real-world demonstration of exactly the
problem this whole update addresses: an agent hammering `/v1/chat/
completions` in a tight loop can and will run into Sydney's own rate
limiting, and until the `ThrottledError` fix above, this proxy gave that
agent no way to tell "Sydney is temporarily refusing requests, back off and
retry" apart from "the model produced an empty response, must have been
its plain judgement" -- a distinction that matters a great deal to any
agent loop deciding what to do next.





## Update: deep, adversarial load-testing of OpenHands' mock function calling — 17/17 on typical tasks, but a new failure mode found under a specific task shape

Following up on the "3/3 sessions succeeded" result documented above, the user
asked for a "much deeper and thorough" test of OpenHands' client-side mock
function calling specifically to find its actual reliability ceiling, not
just confirm the happy path again. This work was done in an isolated git
worktree (`../python-copilot-m365-openhands-load-test`, branch
`openhands-mockfc-load-test`) with its own dedicated proxy instance
(port 8321) to avoid colliding with any other concurrent session.

### Test design

Five batches, each a full, independently-verified `openhands --headless
--json --always-approve` session (fresh `OPENHANDS_PERSISTENCE_DIR` +
fresh project directory per trial, same `native_tool_calling=False` seeding
recipe documented in the section above), escalating in complexity and
covering a dimension the original 3-trial test didn't:

- **Batch A -- baseline** (4 trials): the original "add a `subtract`
  function, print the file" task, repeated for a larger sample in a fresh
  environment.
- **Batch B -- multi-step verification** (6 trials): edit the file, THEN
  use the `terminal` tool to run a Python one-liner exercising the new
  function and report its output -- forces a `file_editor` -> `terminal`
  chain (4-5 tool calls) rather than just one edit.
- **Batch C -- multi-file, more tools** (4 trials): a two-file project
  (`calc.py` + `test_calc.py`), add a function to one file AND a
  corresponding test to the other, then run a verification one-liner
  exercising all three test functions via `terminal` -- 6-7 tool calls
  across two files.
- **Batch D -- concurrency** (3 trials, run genuinely simultaneously via
  three parallel background processes hitting the same proxy port): one
  trial each from batches A/B/C's task shapes, run at the same time, to
  stress the proxy's `ThreadingHTTPServer` and confirm no cross-talk
  between concurrent Chathub sessions.
- **Batch E -- iterative debug loop** (4 trials): a project seeded with a
  *deliberately incorrect* `subtract(a, b)` implementation (`return a + b`)
  and a test that would catch it, with instructions to run the tests, find
  the failure, fix it, then re-verify -- this is a materially different
  task shape from A-D: it requires the model to process a **failing test
  result** and self-correct, which is a core pattern in real agentic coding
  sessions that neither the earlier 3-trial test nor batches A-D exercised
  at all.

### Results, Batches A-D: 17 of 17 full sessions succeeded

Every single trial across A, B, C, and D completed with the file(s) on
disk matching the requested change exactly, and a coherent, correct final
summary message. The tool-call sequences recorded in each session's own
JSONL event log (parsed independently of the model's own claims) confirm
genuine tool use throughout, e.g.:

- Batch A: `['file_editor', 'file_editor', 'file_editor', 'finish']` (x4)
- Batch B: `['file_editor', 'file_editor', 'terminal', 'finish']` (or a
  4-edit variant), each with the terminal tool actually reporting the
  correct computed value (x6)
- Batch C: 6-7 step sequences spanning both files plus a `terminal` test
  run reporting `ALL PASS` (x4)
- Batch D: the three concurrent sessions' final file states matched their
  own individual tasks exactly, with zero evidence of cross-talk; one
  session spontaneously used the `task_tracker` tool as well, and still
  completed correctly (x3)

Across these 17 sessions the proxy handled 88 chat-completion requests
total with **zero exceptions/tracebacks logged** and **every single request
confirmed to carry `tools=0`** (i.e. OpenHands' mock function calling
never once fell back to sending this proxy real `tools`, and this proxy's
own emulation was never invoked). This is a materially larger and more
demanding sample than the original 3-trial test and it holds up completely:
**100% success (17/17) across single-edit, multi-step-with-verification,
multi-file, and concurrent-session task shapes.**

### Results, Batch E (+ 2 follow-up differential batches): a genuine new failure mode found -- 1 of 9 succeeded

Batch E (original wording, "if a test fails, find and fix the bug... report
the final test output") succeeded in only **1 of 4** trials -- a sharp,
reproducible drop from the 100% seen in A-D. Digging into the 3 failures
(not just noting the outcome): every failing turn's raw event log showed
the agent received a completely **empty assistant message** (`"text": ""`)
turn after turn, 3-4 times in a row, until OpenHands gave up and finished
with an empty final summary. Cross-checked directly against this proxy's
own log (not inferred): every one of those turns shows
`reply_length=0 chars` -- Sydney itself returned a genuinely empty
completion, not a truncated one, not an exception, not this proxy's own
bug. In the one successful trial (Batch E, trial 1), by contrast, every
turn had a substantive 250-450 character reply.

This is a **previously undocumented failure mode**, distinct from both
patterns already established in this document (a flat refusal *sentence*,
or self-preemption via the code interpreter): here Sydney returns *nothing
at all*, silently.

**Two follow-up differential batches, run specifically to isolate the
trigger** (not to just re-confirm the failure):

- **Batch F** (3 trials): the exact same buggy fixture, but with the task
  wording changed to avoid the words "bug"/"fix"/"fail" entirely (neutral
  phrasing: "make any necessary changes... so that all assertions
  succeed"). Result: **0 of 3 succeeded** -- if anything worse than Batch
  E, and confirms this is NOT the same "the word 'tool' triggers bad
  behavior" mechanism documented earlier in this file (no tool-related
  words are anywhere in this fixture or task at all, since `tools=0`
  throughout mock-fc mode) -- wording is not the variable.
- **Batch G** (2 trials): the same buggy `calc.py`, but with a task that
  never mentions `test_calc.py`, never asks to run any command, and simply
  says "the `subtract` function should return a minus b; correct it if it
  doesn't." Result: **0 of 2 succeeded**. This rules out the
  terminal-verification loop and the presence of a test file as the
  trigger -- the empty-completion behavior reproduces even in a single-shot,
  no-tool-chain-required task.

Combined: **1 of 9 sessions succeeded** whenever the project on disk
contained this specific buggy fixture (a function literally named
`subtract` whose body performs addition), regardless of wording, whether a
test file was involved, or whether any verification command was ever run.
All 25 individual zero-length replies recorded across this entire load
test (out of 118 total requests) occurred within these three batches (one
isolated, non-blocking zero-length reply also occurred once during Batch A
but didn't prevent that session's overall success -- OpenHands' own
conversation loop tolerated it and continued, simply re-prompting on the
next step). **Zero** zero-length replies occurred in the 17 successful
Batches A-D sessions covering ordinary add/edit/verify work.

### Working hypothesis (not fully confirmed) and honest caveats

The most likely explanation, given the isolation results above: Sydney's
safety/content layer reacts specifically to being shown (and asked to
reason about) a function whose **name doesn't match its actual behavior**
-- `subtract` that adds -- independent of surrounding wording, tests, or
tool use. This is a different trigger category from the two already
documented in this file (a literal keyword; a fabricated "assistant
already acted" history shape) -- here the triggering signal appears to be
in the **code's own semantic content**. This was not tested against other
"deliberately wrong code" shapes (e.g. an off-by-one, a wrong comparison
operator, a genuinely subtle bug rather than a name/behavior mismatch this
blatant) -- it's possible a less blatant mismatch would not trigger this at
all, which would point specifically at "obviously mislabeled/misleading
code" rather than "any bug" as the real trigger. That distinction was not
isolated here and is flagged as the natural next experiment if this is
worth pursuing further. 1/9 across all differential trials is a strong,
reproducible signal (not noise) -- but the sample (9 sessions across 3
small variations of ONE fixture) is still small enough that "this specific
pattern" vs. "some broader class of pattern this fixture happens to
represent" remains an open question.

### Practical implication

The headline finding from the earlier section -- OpenHands' own mock
function calling is dramatically more reliable than this proxy's own
`tools` emulation -- **holds up and is now backed by a much larger, more
demanding sample**: 17 of 17 (100%) across single-edit, multi-step,
multi-file, and concurrent task shapes, with zero proxy-side errors. But
it is **not unconditionally reliable**: a specific, now-reproduced task
shape (reasoning about/fixing code whose behavior contradicts its own
name) drops success to roughly 1 in 9, via a distinct failure mode (silent
empty completions) that has no relationship to the tool-calling mechanism
itself -- it reproduces with zero tools offered, zero mention of tools,
and no terminal use at all. Anyone relying on this configuration for real
debugging/bug-fixing workloads (as opposed to straightforward
implement-a-function work) should expect this specific pattern to be a
real reliability cliff, not a hypothetical one.

### Correction, found right after finishing this test: the "misleading function name" theory is confounded with request volume, and may be wrong

Shortly after wrapping up the batches above, the SAME underlying account
(checked via the separate, non-load-test proxy instance on its own port,
sharing the same credential) was tested with the most trivial possible
prompt ("Reply with just: OK") and got back Sydney's own explicit,
unambiguous rate-limit response: `"We're temporarily unable to respond to
this volume of requests. Please try again later."` This is a genuine,
labeled throttle -- not inferred, not a guess -- and it persisted across a
full proxy restart with a freshly-exchanged access token, confirming it is
an account/tenant-level throttle enforced server-side by Sydney, not
anything tied to this proxy's process state.

This matters because it changes how the "empty completion on
misleadingly-named code" finding above should be read. Batches E, F, and G
were the **last three batches run**, after Batches A-D had already sent 88
requests in this same session -- i.e., they ran at the point in the session
where cumulative request volume was highest. **It is very plausible that
what looked like a content-triggered failure (buggy/misleadingly-named
code specifically) was actually, wholly or partly, volume-based throttling
that happened to coincide with when the buggy-code batches were run** --
the empty-completion failure mode (a `200 OK` with `reply_length=0 chars`,
no error) could plausibly be an earlier, "soft" stage of the same
throttling mechanism that later escalated into the hard, explicit refusal
message once the account crossed some threshold. The proxy's own log
timestamps are consistent with this: the empty-completion failures cluster
in the second half of the session (Batches E onward), not randomly
throughout it, which is what a volume-based effect predicts and a
content-based one does not.

**Net effect on the finding above: downgrade "misleadingly-named/buggy code
causes silent empty completions" from a confirmed content-based trigger to
an unconfirmed hypothesis that a request-volume confound was not
controlled for.** The three differential batches (F: neutral wording, G:
no test file/no terminal) still rule out *wording* and *tool-chain-length*
as the trigger -- those varied while volume kept climbing across the same
session, and the failure persisted regardless -- but they do NOT rule out
volume/rate-limiting as the dominant or sole factor, since volume was
never held constant as an independent variable (every batch in this test
ran later in the session than the one before it, so volume and "which
batch" are perfectly correlated and cannot be separated post hoc). Properly
isolating this would require re-running the Batch E/F/G fixture *first*,
in a fresh session with no prior request volume, and separately measuring
where the explicit throttle message starts appearing as a function of
request count/rate on this account -- neither of which was done here.
**Treat the "17/17 clean, 1/9 on misleading-name code" headline number as
real and reproducible (it is), but treat the specific causal story
(safety-layer reaction to misleadingly-named code) as retracted pending a
volume-controlled retest** -- this is exactly the kind of correction this
document has made before (see the AADSTS70000 section) and is called out
explicitly here for the same reason: a plausible-looking theory that
wasn't adequately isolated should be labeled as such, not left standing
uncorrected once better evidence arrives.

