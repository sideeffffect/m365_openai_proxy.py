"""Offline pytest suite for image generation (image OUT).

Sydney generates images fine but streams no answer TEXT for such a turn, so a
chat-completions response can carry nothing (it stays a 502 -- see
`test_generated_content.py`). The image itself is reachable, just not over that
API: it needs a second access token for the `designerappservice` resource,
minted from the same FOCI refresh token, plus the URL's `fileToken` moved into
a request header. That is what `/v1/images/generations` and the MCP
`generate_image` tool are built on -- see REVERSE_ENGINEERING.md's "Fetching a
generated image".

Fixtures here are verbatim shapes from live captures. The whole pipeline was
verified live (see the PR): real frames -> real token exchange -> real download
of a 1,216,520-byte PNG.

No network: `urlopen` and `run_chat_turn` are stubbed.
"""

import base64
import json
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pytest

import m365_openai_proxy as proxy

#: Verbatim shape of a real `ImageReferenceUrls` entry (opaque values shortened
#: -- only the host, the `fileToken` parameter and the scheme are read).
REAL_URL = (
    "https://designerapp.officeapps.live.com/designerapp/document.ashx"
    "?path=%2Fguid%2FDallEGeneratedImages%2Fdalle-abc.png"
    "&dcHint=WestEurope&speCId=cid&speType=Image&speIdx=0"
    "&fileToken=ZXlKVWIydGxibEJ5WldacGVDSTZJa0ZCUkMweA"
)


def _png(rgb=(220, 20, 20), w=8, h=8):
    row = b"\x00" + bytes(rgb) * w

    def chunk(typ, data):
        c = typ + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * h, 9))
        + chunk(b"IEND", b"")
    )


PNG = _png()


class _FakeResp:
    def __init__(self, body, content_type="image/png"):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeTokenCache:
    """Records which scopes were asked for; hands back distinguishable tokens."""

    def __init__(self):
        self.scopes = []

    def get(self, scope=None):
        scope = scope or proxy.SYDNEY_SCOPE
        self.scopes.append(scope)
        return type(
            "A",
            (),
            {
                "access_token": "token-for-"
                + ("designer" if "designer" in scope else "sydney"),
                "oid": "OID",
                "tid": "TID",
                "expires_at": 1 << 40,
            },
        )()


@pytest.fixture
def captured(monkeypatch):
    """Stubs the download; returns a dict recording the request that was made."""
    seen = {}

    def fake_open(self_or_req, req=None, timeout=None):
        # build_opener().open(req) passes the opener as the first arg.
        request = req if req is not None else self_or_req
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _FakeResp(PNG)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    return seen


# ---------------------------------------------------------------------------
# fetch_generated_image
# ---------------------------------------------------------------------------


def test_fetch_moves_filetoken_into_a_header(captured):
    tc = _FakeTokenCache()
    data, mime = proxy.fetch_generated_image(tc, REAL_URL)

    assert data == PNG
    assert mime == "image/png"
    # The token is a header, and is GONE from the query string -- leaving it
    # there is a live-confirmed HTTP 400.
    assert captured["headers"]["filetoken"] == "ZXlKVWIydGxibEJ5WldacGVDSTZJa0ZCUkMweA"
    assert "fileToken" not in captured["url"]
    # ...while every other query parameter survives untouched.
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert q["speType"] == ["Image"] and q["dcHint"] == ["WestEurope"]


def test_fetch_uses_the_designer_scope_not_the_chathub_one(captured):
    tc = _FakeTokenCache()
    proxy.fetch_generated_image(tc, REAL_URL)
    assert tc.scopes == [proxy.DESIGNER_SCOPE]
    assert captured["headers"]["authorization"] == "Bearer token-for-designer"


@pytest.mark.parametrize(
    "bad_url",
    [
        # A server-supplied URL is the one place this proxy would attach a real
        # access token to a URL it didn't build, so the host is asserted.
        REAL_URL.replace("designerapp.officeapps.live.com", "attacker.example.com"),
        REAL_URL.replace(
            "designerapp.officeapps.live.com",
            "designerapp.officeapps.live.com.evil.net",
        ),
        REAL_URL.replace("https://", "http://"),
    ],
)
def test_fetch_refuses_a_url_it_should_not_send_credentials_to(bad_url, captured):
    with pytest.raises(proxy.ProtocolError):
        proxy.fetch_generated_image(_FakeTokenCache(), bad_url)
    assert not captured, "it must refuse BEFORE issuing any request"


def test_fetch_refusal_never_echoes_the_capability_token():
    """The `fileToken` grants access to the file, so it must not reach an error
    message (and from there a log or an HTTP response body)."""
    bad = REAL_URL.replace("designerapp.officeapps.live.com", "attacker.example.com")
    with pytest.raises(proxy.ProtocolError) as excinfo:
        proxy.fetch_generated_image(_FakeTokenCache(), bad)
    assert "ZXlKVWIydGxibEJ5WldacGVDSTZJa0ZCUkMweA" not in str(excinfo.value)


def test_fetch_requires_a_filetoken(captured):
    no_token = REAL_URL.split("&fileToken=")[0]
    with pytest.raises(proxy.ProtocolError) as excinfo:
        proxy.fetch_generated_image(_FakeTokenCache(), no_token)
    assert "fileToken" in str(excinfo.value)


def test_fetch_rejects_an_empty_download(monkeypatch):
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, req, timeout=None: _FakeResp(b""),
    )
    with pytest.raises(proxy.ProtocolError):
        proxy.fetch_generated_image(_FakeTokenCache(), REAL_URL)


def test_fetch_never_follows_a_redirect():
    """A followed redirect would silently invalidate the host assertion above."""
    assert (
        proxy._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.example.com/x"
        )
        is None
    )


# ---------------------------------------------------------------------------
# generated-URL collection off the Progress frames
# ---------------------------------------------------------------------------


def _progress(urls, status):
    return {
        "text": "Loading image",
        "author": "bot",
        "messageType": "Progress",
        "messageId": "img-1",
        "contentType": "GraphicArt",
        "contentOrigin": "ImageGeneration",
        "contentGenerationProgressList": [
            {"contentType": "image", "ImageReferenceUrls": urls, "status": status}
        ],
    }


def test_only_finished_files_are_collected():
    """Live-observed: the first progress entries carry an EMPTY url list and a
    non-ready status; only the final one is fetchable."""
    urls = []
    proxy._note_generated_content(_progress([], 1), {}, set(), urls)
    assert urls == []
    proxy._note_generated_content(_progress([REAL_URL], 2), {}, set(), urls)
    assert urls == [REAL_URL]


def test_the_same_url_is_not_collected_twice():
    """Sydney repeats the ready entry across frames (2 of 3, live)."""
    urls = []
    for _ in range(3):
        proxy._note_generated_content(_progress([REAL_URL], 2), {}, set(), urls)
    assert urls == [REAL_URL]


def test_urls_are_not_collected_at_all_without_a_sink():
    """The text-only chat path must not even accumulate these URLs."""
    counts = {}
    proxy._note_generated_content(_progress([REAL_URL], 2), counts, set(), None)
    assert counts == {"image": 1}  # still counted, just not collected


def test_a_content_sink_suppresses_the_unsupported_content_error():
    """Passing a sink IS the caller declaring it can represent non-text
    content, so an image-only turn is a success there and an error otherwise."""
    sink = {}
    proxy._finish_turn(0, {"image": 1}, [REAL_URL], sink)
    assert sink == {"kinds": {"image": 1}, "images": [REAL_URL]}

    with pytest.raises(proxy.UnsupportedContentError):
        proxy._finish_turn(0, {"image": 1}, None, None)


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_turn(monkeypatch):
    """Stubs run_chat_turn; `state` controls what the turn 'produces'."""
    state = {"images": [REAL_URL], "text": "", "prompts": []}

    def fake_run_chat_turn(token_cache, text, content_sink=None, **kw):
        state["prompts"].append(text)
        if content_sink is not None:
            content_sink["kinds"] = {"image": 1} if state["images"] else {}
            content_sink["images"] = list(state["images"])
        if state["text"]:
            yield state["text"]

    monkeypatch.setattr(proxy, "run_chat_turn", fake_run_chat_turn)
    monkeypatch.setattr(
        proxy, "fetch_generated_image", lambda tc, url: (PNG, "image/png")
    )
    return state


def test_generate_image_returns_the_downloaded_bytes(fake_turn):
    assert proxy.generate_image(_FakeTokenCache(), "a red bicycle") == [
        (PNG, "image/png")
    ]


def test_generate_image_wraps_a_bare_description_as_an_instruction(fake_turn):
    """OpenAI's `prompt` is normally a bare noun phrase; sent verbatim Sydney
    would just answer prose ABOUT it instead of drawing it."""
    proxy.generate_image(_FakeTokenCache(), "a red bicycle")
    assert fake_turn["prompts"] == [
        "Generate an image based on this description: a red bicycle"
    ]


def test_n_runs_that_many_independent_turns(fake_turn):
    """Sydney produces one image per turn and has no batch parameter."""
    assert len(proxy.generate_image(_FakeTokenCache(), "x", n=3)) == 3
    assert len(fake_turn["prompts"]) == 3


def test_n_is_capped(fake_turn):
    proxy.generate_image(_FakeTokenCache(), "x", n=99)
    assert len(fake_turn["prompts"]) == proxy.MAX_GENERATED_IMAGES


@pytest.mark.parametrize("n", [None, 0, -5, "not a number"])
def test_a_nonsense_n_still_yields_one_image(fake_turn, n):
    if isinstance(n, str):
        pytest.raises(ValueError, proxy.generate_image, _FakeTokenCache(), "x", n)
        return
    assert len(proxy.generate_image(_FakeTokenCache(), "x", n=n)) == 1


def test_no_image_raises_with_sydneys_own_words(fake_turn):
    """The live failure this actually hits: a daily image cap, which Sydney
    reports only as prose. Passing its wording through is what made that
    diagnosable at all, so it is pinned here."""
    fake_turn["images"] = []
    fake_turn["text"] = (
        "Sorry, I can\u2019t generate any more images today. Try again tomorrow."
    )
    with pytest.raises(proxy.UnsupportedContentError) as excinfo:
        proxy.generate_image(_FakeTokenCache(), "a red bicycle")
    assert "generate any more images today" in str(excinfo.value)


def test_no_image_and_no_text_still_raises_clearly(fake_turn):
    fake_turn["images"] = []
    fake_turn["text"] = ""
    with pytest.raises(proxy.UnsupportedContentError) as excinfo:
        proxy.generate_image(_FakeTokenCache(), "x")
    assert "no text either" in str(excinfo.value)


# ---------------------------------------------------------------------------
# POST /v1/images/generations
# ---------------------------------------------------------------------------


@pytest.fixture
def port(fake_token_cache, run_server):
    return run_server(fake_token_cache, proxy.ConversationSessionStore())


def _post_images(port, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/images/generations",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_endpoint_returns_b64_json(port, fake_turn):
    status, body = _post_images(port, {"prompt": "a red bicycle"})
    assert status == 200
    assert body["data"] == [{"b64_json": base64.b64encode(PNG).decode()}]
    assert body["output_format"] == "png"
    assert isinstance(body["created"], int)
    # No `url` key: it would point at a host the caller cannot authenticate to.
    assert "url" not in body["data"][0]


def test_endpoint_omits_usage_rather_than_faking_zeros(port, fake_turn):
    _status, body = _post_images(port, {"prompt": "x"})
    assert "usage" not in body


@pytest.mark.parametrize("bad", [{}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}])
def test_endpoint_requires_a_prompt(port, fake_turn, bad):
    status, body = _post_images(port, bad)
    assert status == 400
    assert "prompt" in body["error"]["message"]


def test_endpoint_rejects_response_format_url(port, fake_turn):
    status, body = _post_images(port, {"prompt": "x", "response_format": "url"})
    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"
    # The reason must be stated, not just "unsupported".
    assert "b64_json" in body["error"]["message"]


def test_endpoint_accepts_response_format_b64_json_explicitly(port, fake_turn):
    status, _ = _post_images(port, {"prompt": "x", "response_format": "b64_json"})
    assert status == 200


def test_endpoint_ignores_controls_copilot_does_not_have(port, fake_turn):
    """`size`/`quality`/`style` are accepted-and-ignored rather than rejected,
    so an ordinary OpenAI client that always sends them still works."""
    status, _ = _post_images(
        port, {"prompt": "x", "size": "1024x1024", "quality": "hd", "style": "vivid"}
    )
    assert status == 200


def test_endpoint_reports_no_image_as_its_own_error_type(port, fake_turn):
    """Not 429 (it is not a rate limit) and not a bare 502 -- a different
    prompt may well succeed, so it gets a distinct err_type."""
    fake_turn["images"] = []
    fake_turn["text"] = "Sorry, I can't generate any more images today."
    status, body = _post_images(port, {"prompt": "x"})
    assert status == 502
    assert body["error"]["type"] == "upstream_no_image"
    assert "generate any more images today" in body["error"]["message"]


def test_endpoint_maps_throttling_to_429(port, monkeypatch):
    def boom(*a, **k):
        raise proxy.ThrottledError("slow down")

    monkeypatch.setattr(proxy, "generate_image", boom)
    status, body = _post_images(port, {"prompt": "x"})
    assert status == 429
    assert body["error"]["type"] == "upstream_throttled"


def test_images_endpoint_is_a_post_only_route(port):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/images/generations", method="GET"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ---------------------------------------------------------------------------
# MCP generate_image tool
# ---------------------------------------------------------------------------


def _mcp(port, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _call(port, args):
    return _mcp(
        port,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": args},
        },
    )


def test_mcp_returns_a_real_image_content_block(port, fake_turn):
    """The reason image output is offered over MCP at all: it has a first-class
    image block, so the bytes don't have to be smuggled through text."""
    status, r = _call(port, {"prompt": "a red bicycle"})
    assert status == 200
    assert r["result"]["isError"] is False
    assert r["result"]["content"] == [
        {
            "type": "image",
            "data": base64.b64encode(PNG).decode(),
            "mimeType": "image/png",
        }
    ]


def test_mcp_image_result_is_not_mistaken_for_a_throttled_empty_reply(port, fake_turn):
    """An all-image result has NO text at all, which is exactly the empty-reply
    throttle signature -- checking it here would report every generated image as
    throttling, the very bug this release fixes on the chat path."""
    _status, r = _call(port, {"prompt": "x"})
    assert r["result"]["isError"] is False
    assert proxy._looks_like_throttled_empty_reply(
        ""
    )  # the signature really does match


def test_mcp_missing_prompt_is_an_iserror_result(port, fake_turn):
    _status, r = _call(port, {})
    assert r["result"]["isError"] is True
    assert "prompt" in r["result"]["content"][0]["text"]


def test_mcp_no_image_is_an_iserror_result_with_the_reason(port, fake_turn):
    fake_turn["images"] = []
    fake_turn["text"] = "Sorry, I can't generate any more images today."
    _status, r = _call(port, {"prompt": "x"})
    assert r["result"]["isError"] is True
    assert "generate any more images today" in r["result"]["content"][0]["text"]


def test_mcp_text_tools_still_return_text_blocks(port, fake_turn):
    """Widening _mcp_run_tool to content blocks must not change the text tools."""
    _status, r = _mcp(
        port,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ask_copilot", "arguments": {"prompt": "hi"}},
        },
    )
    fake_turn["text"] = "hello"
    assert r["result"]["content"][0]["type"] == "text"


def test_generate_image_tool_is_advertised():
    tools = {t["name"]: t for t in proxy._mcp_tool_definitions()}
    assert tools["generate_image"]["inputSchema"]["required"] == ["prompt"]
    assert tools["generate_image"]["inputSchema"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# the scope-keyed token cache
# ---------------------------------------------------------------------------


def test_token_cache_is_keyed_by_scope_and_shares_one_refresh_lock(monkeypatch):
    """Two audiences from ONE single-use refresh token. A per-scope lock would
    let both redeem concurrently -- the second rejected as invalid_grant, and
    two rotate() writes racing, which corrupts the credential file."""
    redemptions = []

    def fake_exchange(refresh_token, oid=None, tid=None, scope=None):
        redemptions.append(scope)
        # A JWE-shaped (5-part) token for the designer scope, a real JWT for
        # Chathub -- matching what Entra actually returns for each.
        if scope == proxy.DESIGNER_SCOPE:
            return {"access_token": "a.b.c.d.e", "expires_in": 4091}
        claims = (
            base64.urlsafe_b64encode(
                json.dumps({"oid": "o", "tid": "t", "exp": (1 << 40)}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        return {"access_token": f"h.{claims}.s", "expires_in": 3600}

    monkeypatch.setattr(proxy, "exchange_refresh_token", fake_exchange)

    class Store:
        oid_hint, tid_hint = "o", "t"

        def current(self):
            return "rt"

        def rotate(self, new_rt):
            pass

    cache = proxy.TokenCache(Store())
    sydney = cache.get()
    designer = cache.get(scope=proxy.DESIGNER_SCOPE)

    assert redemptions == [proxy.SYDNEY_SCOPE, proxy.DESIGNER_SCOPE]
    assert sydney.access_token != designer.access_token
    # Each scope is cached independently -- no further redemptions.
    assert cache.get().access_token == sydney.access_token
    assert cache.get(scope=proxy.DESIGNER_SCOPE).access_token == designer.access_token
    assert len(redemptions) == 2


def test_a_jwe_scoped_token_does_not_need_readable_claims(monkeypatch):
    """The designer token is ENCRYPTED, so jwt_claims can see no oid/tid/exp in
    it. Requiring them (as the Chathub path rightly does) would break it."""

    def fake_exchange(refresh_token, oid=None, tid=None, scope=None):
        return {"access_token": "a.b.c.d.e", "expires_in": 100}

    monkeypatch.setattr(proxy, "exchange_refresh_token", fake_exchange)

    class Store:
        oid_hint, tid_hint = "hint-oid", "hint-tid"

        def current(self):
            return "rt"

        def rotate(self, new_rt):
            pass

    auth = proxy.TokenCache(Store()).get(scope=proxy.DESIGNER_SCOPE)
    assert auth.access_token == "a.b.c.d.e"
    # Identity falls back to the store's hints; the lifetime to `expires_in`.
    assert (auth.oid, auth.tid) == ("hint-oid", "hint-tid")
    assert 0 < auth.expires_at - __import__("time").time() <= 100


def test_chathub_scope_still_requires_oid_and_tid(monkeypatch):
    """They go into the Chathub WebSocket URL path, so a token without them is
    unusable and must fail loudly rather than later at connect time."""

    def fake_exchange(refresh_token, oid=None, tid=None, scope=None):
        claims = base64.urlsafe_b64encode(json.dumps({"exp": 1 << 40}).encode())
        return {"access_token": f"h.{claims.rstrip(b'=').decode()}.s"}

    monkeypatch.setattr(proxy, "exchange_refresh_token", fake_exchange)

    class Store:
        oid_hint = tid_hint = None

        def current(self):
            return "rt"

        def rotate(self, new_rt):
            pass

    with pytest.raises(proxy.AuthError):
        proxy.TokenCache(Store()).get()
