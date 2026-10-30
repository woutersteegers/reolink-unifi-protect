"""Talkback bridge: Protect's AAC datagrams -> camera speaker over an ONVIF
RTSP backchannel. These tests pin the pieces that do not need a camera: SDP
parsing, RTP framing, RTSP Digest auth, and the whole DESCRIBE / SETUP / PLAY
handshake plus interleaved RTP against a fake RTSP server.
"""

import asyncio
import hashlib
import logging
import re
import struct

import pytest

from unifi import talkback
from unifi.talkback import RtspBackchannel, build_rtp, parse_sdp_backchannel

SDP = (
    "v=0\r\n"
    "m=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
    "m=audio 0 RTP/AVP 8\r\na=recvonly\r\na=control:trackID=1\r\n"
    "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    "a=sendonly\r\na=control:track3\r\n"
)


def test_sdp_picks_the_sendonly_audio_track():
    track = parse_sdp_backchannel(SDP)
    assert track == {"control": "track3", "payload_type": 0, "codec": "PCMU/8000"}


def test_sdp_without_backchannel_returns_none():
    assert parse_sdp_backchannel("v=0\r\nm=video 0 RTP/AVP 96\r\n") is None


def test_rtp_header_layout():
    pkt = build_rtp(65535, 160, 0xDEADBEEF, b"\xff" * 4, pt=0)
    assert pkt[:12] == bytes.fromhex("80" "00" "ffff" "000000a0" "deadbeef")
    assert pkt[12:] == b"\xff" * 4
    # sequence and timestamp wrap instead of overflowing
    wrapped = build_rtp(65536, 2**32 + 1, 1, b"")
    assert struct.unpack(">HI", wrapped[2:8]) == (0, 1)


def test_digest_auth_matches_independent_computation(monkeypatch):
    monkeypatch.setattr(talkback.os, "urandom", lambda n: b"\x01" * n)
    cam = RtspBackchannel("rtsp://x/y", "Mufasa", "Circle Of Life", logging.getLogger())
    cam._auth = cam._parse_challenge(
        'Digest realm="testrealm@host.com", qop="auth", nonce="abc123"'
    )
    header = cam._auth_header("DESCRIBE", "rtsp://x/y")
    cnonce = "01" * 8
    ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
    ha2 = hashlib.md5(b"DESCRIBE:rtsp://x/y").hexdigest()
    expected = hashlib.md5(
        f"{ha1}:abc123:00000001:{cnonce}:auth:{ha2}".encode()
    ).hexdigest()
    assert f'response="{expected}"' in header
    assert 'username="Mufasa"' in header and "nc=00000001" in header


def test_basic_auth_challenge():
    cam = RtspBackchannel("rtsp://x/y", "u", "p", logging.getLogger())
    cam._auth = cam._parse_challenge('Basic realm="IPC"')
    assert cam._auth_header("DESCRIBE", "rtsp://x/y") == "Basic dTpw"


class FakeRtspServer:
    """Minimal RTSP server: optional Digest challenge, then the ONVIF backchannel."""

    def __init__(self, require_auth=False):
        self.require_auth = require_auth
        self.requests = []
        self.frames = []
        self.torn_down = asyncio.Event()
        self.got_frame = asyncio.Event()

    async def handle(self, reader, writer):
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    hdr = await reader.readexactly(3)
                    payload = await reader.readexactly(struct.unpack(">H", hdr[1:])[0])
                    self.frames.append((hdr[0], payload))
                    self.got_frame.set()
                    continue
                head = (first + await reader.readuntil(b"\r\n\r\n")).decode()
                lines = head.split("\r\n")
                method = lines[0].split()[0]
                headers = {
                    k.strip().lower(): v.strip()
                    for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)
                }
                self.requests.append((method, headers))
                cseq = headers["cseq"]
                if self.require_auth and "authorization" not in headers:
                    resp = (
                        f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                        'WWW-Authenticate: Digest realm="IPC", nonce="n0nce", '
                        'qop="auth"\r\n\r\n'
                    )
                elif method == "DESCRIBE":
                    body = SDP.encode()
                    resp = (
                        f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                        "Content-Base: rtsp://127.0.0.1/stream/\r\n"
                        "Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n{SDP}"
                    )
                elif method == "SETUP":
                    resp = (
                        f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: ABCD1234;timeout=60\r\n"
                        "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n"
                    )
                elif method == "TEARDOWN":
                    self.torn_down.set()
                    resp = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n"
                else:
                    resp = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n"
                writer.write(resp.encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()


async def _serve(server):
    srv = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


@pytest.mark.parametrize("require_auth", [False, True])
async def test_backchannel_handshake_and_rtp(require_auth):
    server = FakeRtspServer(require_auth=require_auth)
    srv, port = await _serve(server)
    cam = RtspBackchannel(
        f"rtsp://127.0.0.1:{port}/stream", "user", "pw", logging.getLogger()
    )
    try:
        await cam.open()
        assert cam.ready
        await cam.send_mulaw(b"\xff" * 160)
        await asyncio.wait_for(server.got_frame.wait(), 2)
    finally:
        await cam.close()
        srv.close()

    methods = [m for m, _ in server.requests]
    assert [m for m in methods if m != "TEARDOWN"][-3:] == ["DESCRIBE", "SETUP", "PLAY"]
    for method, headers in server.requests:
        if method in ("DESCRIBE", "SETUP", "PLAY"):
            assert headers["require"] == talkback.REQUIRE
    if require_auth:
        assert any("authorization" in h for _, h in server.requests)
        assert re.search(r'username="user"', server.requests[1][1]["authorization"])
    setup = next(h for m, h in server.requests if m == "SETUP")
    assert "interleaved" in setup["transport"]
    chan, payload = server.frames[0]
    assert chan == 0
    assert payload[0] == 0x80 and payload[1] == 0  # RTP v2, PCMU
    assert payload[12:] == b"\xff" * 160


async def test_open_fails_without_a_backchannel_track():
    server = FakeRtspServer()
    srv, port = await _serve(server)

    original = talkback.parse_sdp_backchannel
    talkback.parse_sdp_backchannel = lambda sdp: None
    cam = RtspBackchannel(f"rtsp://127.0.0.1:{port}/s", "u", "p", logging.getLogger())
    try:
        with pytest.raises(RuntimeError, match="backchannel"):
            await cam.open()
        assert not cam.ready
    finally:
        talkback.parse_sdp_backchannel = original
        await cam.close()
        srv.close()
