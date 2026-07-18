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
- **Still open / unexplored**: multi-turn conversation payload shape (does
  Sydney want resent history, or look it up server-side by ConversationId?
  — every capture so far is a brand-new single-turn conversation); whether
  `msal.cache.encryption` is `HttpOnly`.
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
