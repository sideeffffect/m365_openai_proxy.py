"""Dependency-free smoke checks that run on EVERY supported interpreter.

WHY THIS EXISTS, and why it is not just more pytest
---------------------------------------------------
CI's compatibility job deliberately uses a *bare* interpreter -- no uv, no
`pip install` -- to prove the stdlib-only proxy runs with nothing installed.
That job is also the only one that can reach the oldest supported Python:
uv cannot install 3.7 at all, and current pytest/bandit releases require 3.10+.
So on the oldest interpreters, this file is the only thing that can execute
the proxy's real code, and it therefore may not import anything outside the
standard library.

It exists because `py_compile` + `--help` -- everything that job used to do --
is far too shallow a probe. A Python-3.9-only `usedforsecurity=` kwarg on the
`hashlib.sha1()` call in the WebSocket handshake once shipped and broke *every
chat turn* on Python 3.7/3.8, while byte-compilation, `--help`, and all 22
offline tests stayed green on those versions. Nothing in CI executed the line.
The checks below are chosen on exactly that criterion: pure, local, no network
or credentials, but real calls into the code paths that a version-specific
stdlib difference would silently break.

Run it directly with any supported interpreter:

    python3 tests/selfcheck.py

It is also executed by the normal pytest suite (see test_selfcheck.py), so the
same assertions run on modern Python without being duplicated.
"""

import binascii
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import m365_openai_proxy as proxy

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _hex(s):
    return binascii.unhexlify(s)


class _LoopbackSocket:
    """Just enough socket for `_send_frame` to hand bytes to `_read_frame`."""

    def __init__(self):
        self.buf = b""

    def sendall(self, data):
        self.buf += data

    def recv(self, n):
        chunk, self.buf = self.buf[:n], self.buf[n:]
        return chunk


def _detached_ws_client():
    """A WebSocketClient wired to a loopback socket, without connecting."""
    ws = object.__new__(proxy.WebSocketClient)
    ws.sock = _LoopbackSocket()
    ws._buf = b""
    return ws


@check
def websocket_accept_matches_rfc6455_vector():
    """The exact line that broke on 3.7/3.8 -- see this module's docstring.

    Vector is the worked example from RFC 6455 section 1.3.
    """
    got = proxy.websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ==")
    assert got == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", got


@check
def websocket_frames_round_trip():
    """Exercise struct packing/masking across all three length encodings."""
    for size in (5, 125, 126, 65535, 65536):
        ws = _detached_ws_client()
        payload = os.urandom(size)
        ws._send_frame(proxy.WebSocketClient.OPCODE_BINARY, payload)
        fin, opcode, got = ws._read_frame()
        assert fin is True, size
        assert opcode == proxy.WebSocketClient.OPCODE_BINARY, size
        assert got == payload, f"payload mismatch at size {size}"


@check
def websocket_text_message_round_trips_unicode():
    ws = _detached_ws_client()
    ws.send_text('{"protocol":"json","v":1} ünïcødé ✓')
    _fin, opcode, payload = ws._read_frame()
    assert opcode == proxy.WebSocketClient.OPCODE_TEXT
    assert payload.decode("utf-8") == '{"protocol":"json","v":1} ünïcødé ✓'


@check
def signalr_buffer_splits_on_record_separator():
    buf = proxy.SignalRBuffer()
    assert list(buf.feed('{"a":1}')) == []  # no separator yet -> nothing emitted
    got = list(buf.feed('\x1e{"b":2}\x1e'))
    assert got == [{"a": 1}, {"b": 2}], got
    # An empty record between two separators must be skipped, not parsed.
    assert list(buf.feed("\x1e")) == []


@check
def aes256_gcm_decrypts_nist_vector():
    """NIST GCM specification, AES-256 test case 16 (with AAD)."""
    plaintext = proxy.aes256_gcm_decrypt(
        _hex("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308"),
        _hex("cafebabefacedbaddecaf888"),
        _hex(
            "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
            "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662"
            "76fc6ece0f4e1768cddf8853bb2d551b"
        ),
        _hex("feedfacedeadbeeffeedfacedeadbeefabaddad2"),
    )
    assert plaintext == _hex(
        "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39"
    )


@check
def aes256_gcm_rejects_a_tampered_tag():
    key, iv = b"\x00" * 32, b"\x00" * 12
    try:
        proxy.aes256_gcm_decrypt(key, iv, b"\x00" * 32)
    except Exception:  # noqa: BLE001 - any rejection is a pass; the point is it raises
        return
    raise AssertionError("tampered ciphertext was accepted")


@check
def hkdf_sha256_matches_rfc5869_vector():
    """RFC 5869 test case 1."""
    okm = proxy._hkdf_sha256(
        _hex("0b" * 22),
        _hex("000102030405060708090a0b0c"),
        _hex("f0f1f2f3f4f5f6f7f8f9"),
        42,
    )
    assert okm == _hex(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"
    )


@check
def jwt_claims_decodes_unpadded_base64url():
    # oid/tid-shaped claims, deliberately sized so the payload segment needs
    # base64url re-padding -- a classic source of silent breakage.
    token = "eyJhbGciOiJub25lIn0.eyJvaWQiOiIxMjMiLCJ0aWQiOiI0NTYifQ.sig"
    assert proxy.jwt_claims(token) == {"oid": "123", "tid": "456"}


@check
def proxy_reports_a_three_part_semver():
    parts = proxy.PROXY_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), proxy.PROXY_VERSION


@check
def declared_floor_matches_this_interpreter():
    assert sys.version_info >= proxy.MIN_PYTHON, (
        f"selfcheck is running on {sys.version.split()[0]}, "
        f"below the proxy's declared floor {proxy.MIN_PYTHON}"
    )


@check
def vision_input_extracts_and_encodes():
    # A 1x1 PNG as an inline data: URI -> _extract_message_images must keep the
    # exact URI (that is what UploadFile wants) and decode bytes for the size
    # cap; _multipart_body must produce a well-formed body. Exercises base64
    # and the data-URI parsing on every interpreter.
    import base64

    png = _hex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408d763f8cfc0f01f0005000151d1c9b8000000004945"
        "4e44ae426082"
    )
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": uri}}]}
    ]
    images = proxy._extract_message_images(msgs)
    assert len(images) == 1, images
    assert images[0]["data_uri"] == uri
    assert images[0]["file_type"] == "png"
    ctype, body = proxy._multipart_body([("scenario", "UploadImage"), ("f", uri)])
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b"UploadImage" in body and uri.encode() in body


@check
def mcp_tool_surface_is_well_formed():
    tools = {t["name"]: t for t in proxy._mcp_tool_definitions()}
    assert set(tools) == {"ask_copilot", "describe_image"}, tools
    assert tools["ask_copilot"]["inputSchema"]["required"] == ["prompt"]
    # Unknown tool -> KeyError; bad image arg -> _MCPToolError (both are how the
    # handler decides JSON-RPC-error vs isError-result).
    try:
        proxy._mcp_run_tool(None, "does_not_exist", {})
        raise AssertionError("expected KeyError for unknown tool")
    except KeyError:
        pass
    try:
        proxy._mcp_image_from_data_uri("https://example.com/x.png")
        raise AssertionError("expected _MCPToolError for a non-data: URI")
    except proxy._MCPToolError:
        pass


def run():
    """Run every check. Returns a list of (name, exception) for failures."""
    failures = []
    for fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report, don't abort the run
            failures.append((fn.__name__, exc))
    return failures


def main():
    version = sys.version.split()[0]
    failures = run()
    for name, exc in failures:
        sys.stderr.write(f"FAIL  {name}: {type(exc).__name__}: {exc}\n")
    passed = len(CHECKS) - len(failures)
    sys.stderr.write(f"selfcheck: {passed}/{len(CHECKS)} passed on Python {version}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
