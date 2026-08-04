"""Offline pytest suite for vision input (image understanding / GPT-V).

Exercises the real proxy code paths that turn an OpenAI `image_url` content
part into a Sydney `UploadFile` POST + an `ImageFile` message annotation,
with every network call stubbed. The wire format these assert against was
reverse-engineered from a live browser capture and verified live on
2026-08-02 (red/green/blue solid PNGs each described correctly); see
REVERSE_ENGINEERING.md's "Vision input" section.

No network, no credentials.
"""

import base64
import json
import struct
import zlib

import pytest

import m365_openai_proxy as proxy


def _make_png(width=8, height=8, rgb=(220, 20, 20)):
    """A minimal valid solid-color PNG, pure stdlib."""
    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def chunk(typ, data):
        c = typ + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _data_uri(png_bytes, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(png_bytes).decode()


# ---------------------------------------------------------------------------
# _decode_data_uri_image
# ---------------------------------------------------------------------------


def test_decode_data_uri_png_roundtrip():
    png = _make_png()
    data, file_type = proxy._decode_data_uri_image(_data_uri(png))
    assert data == png
    assert file_type == "png"


def test_decode_data_uri_jpeg_maps_to_jpg():
    data, file_type = proxy._decode_data_uri_image(
        "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff").decode()
    )
    assert data == b"\xff\xd8\xff"
    assert file_type == "jpg"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/cat.png",  # not a data: URI
        "data:text/plain;base64,aGk=",  # not an image mime
        "data:image/png,notbase64",  # missing base64 marker
        "data:image/tiff;base64,AAAA",  # unsupported image type
        "",
        None,
        1234,
    ],
)
def test_decode_data_uri_rejects_unsupported(url):
    assert proxy._decode_data_uri_image(url) == (None, None)


# ---------------------------------------------------------------------------
# _extract_message_images
# ---------------------------------------------------------------------------


def test_extract_images_from_last_user_message():
    png = _make_png()
    uri = _data_uri(png)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(_make_png(rgb=(1, 2, 3)))},
                }
            ],
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": uri}},
            ],
        },
    ]
    images = proxy._extract_message_images(messages)
    assert len(images) == 1
    # The exact data URI is preserved verbatim (that is what UploadFile wants).
    assert images[0]["data_uri"] == uri
    assert images[0]["data"] == png
    assert images[0]["file_type"] == "png"


def test_extract_accepts_bare_string_image_url():
    uri = _data_uri(_make_png())
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": uri}]}]
    images = proxy._extract_message_images(messages)
    assert len(images) == 1
    assert images[0]["data_uri"] == uri


def test_extract_plain_string_content_has_no_images():
    assert proxy._extract_message_images([{"role": "user", "content": "hello"}]) == []


def test_extract_skips_http_image_urls():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}
            ],
        }
    ]
    assert proxy._extract_message_images(messages) == []


def test_extract_respects_image_cap():
    part = {"type": "image_url", "image_url": {"url": _data_uri(_make_png())}}
    messages = [{"role": "user", "content": [part] * (proxy.MAX_INPUT_IMAGES + 3)}]
    images = proxy._extract_message_images(messages)
    assert len(images) == proxy.MAX_INPUT_IMAGES


def test_extract_skips_oversized_image(monkeypatch):
    monkeypatch.setattr(proxy, "MAX_INPUT_IMAGE_BYTES", 10)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _data_uri(_make_png())}}
            ],
        }
    ]
    assert proxy._extract_message_images(messages) == []


# ---------------------------------------------------------------------------
# _multipart_body
# ---------------------------------------------------------------------------


def test_multipart_body_encodes_fields_and_bytes():
    content_type, body = proxy._multipart_body(
        [("scenario", "UploadImage"), ("blob", b"\x00\x01\x02"), ("optionsSets", "a")]
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    assert body.count(b"--" + boundary.encode()) == 4  # 3 parts + closing
    assert b'name="scenario"' in body
    assert b"UploadImage" in body
    assert b"\x00\x01\x02" in body  # raw bytes passed through untouched
    assert body.rstrip().endswith(b"--" + boundary.encode() + b"--")


# ---------------------------------------------------------------------------
# upload_image_to_sydney  (urlopen stubbed)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_upload_image_builds_annotation_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        return _FakeResp(
            {"docId": "0-weu-d9-deadbeef", "fileName": "x.png", "fileType": ".png"}
        )

    monkeypatch.setattr(proxy.urllib.request, "urlopen", fake_urlopen)

    auth = type("A", (), {"access_token": "tok", "oid": "OID", "tid": "TID"})()
    uri = _data_uri(_make_png())
    image = {
        "data_uri": uri,
        "data": _make_png(),
        "file_type": "png",
        "filename": "image_1.png",
    }
    annotation = proxy.upload_image_to_sydney(auth, "conv-123", image)

    assert annotation == {
        "id": "0-weu-d9-deadbeef",
        "messageAnnotationType": "ImageFile",
        "messageAnnotationMetadata": {
            "@type": "File",
            "annotationType": "File",
            "fileType": "png",
            "fileName": "image_1.png",
        },
    }
    assert captured["url"] == proxy.UPLOAD_FILE_URL
    h = captured["headers"]
    assert h["authorization"] == "Bearer tok"
    # The three load-bearing headers (missing X-Variants => live 403).
    assert h["x-variants"] == "feature.EnableImageSupportInUploadFile"
    assert h["x-scenario"] == "OfficeWebIncludedCopilot"
    assert h["x-anchormailbox"] == "Oid:OID@TID"
    # FileBase64 carries the entire data URI string verbatim, plus the upload
    # optionsSets and the conversation id.
    assert uri.encode() in captured["body"]
    assert b"conv-123" in captured["body"]
    for opt in proxy.UPLOAD_IMAGE_OPTIONS_SETS:
        assert opt.encode() in captured["body"]


def test_upload_image_raises_without_docid(monkeypatch):
    monkeypatch.setattr(
        proxy.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp({"result": "nope"}),
    )
    auth = type("A", (), {"access_token": "t", "oid": "o", "tid": "d"})()
    image = {
        "data_uri": _data_uri(_make_png()),
        "data": b"x",
        "file_type": "png",
        "filename": "i.png",
    }
    with pytest.raises(proxy.ProtocolError):
        proxy.upload_image_to_sydney(auth, "c", image)


# ---------------------------------------------------------------------------
# send_chat_message attaches (or omits) messageAnnotations
# ---------------------------------------------------------------------------


class _CaptureWS:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)

    def last_message(self):
        frame = self.sent[-1].rstrip(proxy.SIGNALR_RS)
        return json.loads(frame)["arguments"][0]["message"]


def test_send_chat_message_attaches_annotations():
    ws = _CaptureWS()
    ann = [
        {
            "id": "0-weu-d9-x",
            "messageAnnotationType": "ImageFile",
            "messageAnnotationMetadata": {},
        }
    ]
    proxy.send_chat_message(ws, "sess", "what is this?", image_annotations=ann)
    assert ws.last_message()["messageAnnotations"] == ann


def test_send_chat_message_without_images_has_no_annotations():
    ws = _CaptureWS()
    proxy.send_chat_message(ws, "sess", "hello")
    assert "messageAnnotations" not in ws.last_message()
