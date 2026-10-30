"""Protect talkback (AAC/ADTS over UDP) -> camera speaker via an ONVIF RTSP backchannel.

Self-contained: RTSP client in plain asyncio plus ffmpeg (already in the image) for
AAC -> G.711 mu-law 8 kHz. No sidecar.
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import struct
from typing import Optional
from urllib.parse import urlparse

REQUIRE = "www.onvif.org/ver20/backchannel"


def parse_sdp_backchannel(sdp: str) -> Optional[dict]:
    """Return {control, payload_type, codec} for the first sendonly audio section."""
    sections = re.split(r"(?m)^m=", sdp)[1:]
    for sec in sections:
        lines = sec.replace("\r", "").split("\n")
        if not lines[0].startswith("audio"):
            continue
        if not any(ln.strip() == "a=sendonly" for ln in lines):
            continue
        fmt = lines[0].split()[3] if len(lines[0].split()) > 3 else "0"
        control = next(
            (
                ln[len("a=control:") :].strip()
                for ln in lines
                if ln.startswith("a=control:")
            ),
            None,
        )
        codec = next(
            (ln.split(None, 1)[1] for ln in lines if ln.startswith(f"a=rtpmap:{fmt} ")),
            "",
        )
        return {"control": control, "payload_type": int(fmt), "codec": codec}
    return None


def build_rtp(seq: int, ts: int, ssrc: int, payload: bytes, pt: int = 0) -> bytes:
    return (
        struct.pack(">BBHII", 0x80, pt & 0x7F, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
        + payload
    )


class RtspBackchannel:
    def __init__(
        self, url: str, user: str, password: str, logger: logging.Logger
    ) -> None:
        self.url = url
        self.user = user
        self.password = password
        self.logger = logger
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._cseq = 0
        self._session: Optional[str] = None
        self._auth: Optional[dict] = None
        self._nc = 0
        self._chan = 0
        self._seq = 0
        self._ts = 0
        self._ssrc = int.from_bytes(os.urandom(4), "big")
        self._pt = 0
        self._content_base = url
        self._rx_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.ready = False

    def _auth_header(self, method: str, uri: str) -> Optional[str]:
        if not self._auth:
            return None
        if self._auth["scheme"] == "basic":
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return f"Basic {tok}"
        a = self._auth
        ha1 = hashlib.md5(
            f"{self.user}:{a['realm']}:{self.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if "auth" in a.get("qop", ""):
            self._nc += 1
            nc = f"{self._nc:08x}"
            cnonce = os.urandom(8).hex()
            resp = hashlib.md5(
                f"{ha1}:{a['nonce']}:{nc}:{cnonce}:auth:{ha2}".encode()
            ).hexdigest()
            return (
                f'Digest username="{self.user}", realm="{a["realm"]}", nonce="{a["nonce"]}", '
                f'uri="{uri}", response="{resp}", qop=auth, nc={nc}, cnonce="{cnonce}"'
            )
        resp = hashlib.md5(f"{ha1}:{a['nonce']}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{self.user}", realm="{a["realm"]}", nonce="{a["nonce"]}", '
            f'uri="{uri}", response="{resp}"'
        )

    @staticmethod
    def _parse_challenge(value: str) -> dict:
        if value.lower().startswith("basic"):
            return {"scheme": "basic"}
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', value))
        return {
            "scheme": "digest",
            "realm": fields.get("realm", ""),
            "nonce": fields.get("nonce", ""),
            "qop": fields.get("qop", ""),
        }

    async def _read_response(self) -> tuple[int, dict, bytes]:
        assert self._reader
        while True:
            first = await asyncio.wait_for(self._reader.readexactly(1), 10)
            if first == b"$":
                hdr = await self._reader.readexactly(3)
                await self._reader.readexactly(struct.unpack(">H", hdr[1:3])[0])
                continue
            head = first + await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"), 10
            )
            break
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = b""
        if "content-length" in headers:
            body = await self._reader.readexactly(int(headers["content-length"]))
        return status, headers, body

    async def _request(
        self, method: str, url: str, extra: Optional[dict] = None
    ) -> tuple[int, dict, bytes]:
        assert self._writer
        for attempt in (1, 2):
            self._cseq += 1
            lines = [
                f"{method} {url} RTSP/1.0",
                f"CSeq: {self._cseq}",
                "User-Agent: unifi-cam-proxy",
            ]
            if self._session:
                lines.append(f"Session: {self._session}")
            for k, v in (extra or {}).items():
                lines.append(f"{k}: {v}")
            auth = self._auth_header(method, url)
            if auth:
                lines.append(f"Authorization: {auth}")
            self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await self._writer.drain()
            status, headers, body = await self._read_response()
            if status == 401 and attempt == 1 and "www-authenticate" in headers:
                self._auth = self._parse_challenge(headers["www-authenticate"])
                continue
            return status, headers, body
        return status, headers, body

    async def open(self) -> None:
        u = urlparse(self.url)
        port = u.port or 554
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, port), 8
        )
        self.url = f"rtsp://{u.hostname}:{port}{u.path}"
        status, headers, body = await self._request(
            "DESCRIBE", self.url, {"Require": REQUIRE, "Accept": "application/sdp"}
        )
        if status != 200:
            raise RuntimeError(f"DESCRIBE failed: {status}")
        self._content_base = headers.get("content-base", self.url).rstrip("/")
        track = parse_sdp_backchannel(body.decode("latin-1"))
        if not track:
            raise RuntimeError("no sendonly audio (backchannel) track in SDP")
        self._pt = track["payload_type"]
        ctl = track["control"] or ""
        track_url = ctl if ctl.startswith("rtsp://") else f"{self._content_base}/{ctl}"
        self.logger.info(
            f"Backchannel track: {track['codec']} pt={self._pt} control={ctl}"
        )
        status, headers, _ = await self._request(
            "SETUP",
            track_url,
            {"Require": REQUIRE, "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        if status != 200:
            raise RuntimeError(f"SETUP failed: {status}")
        self._session = headers.get("session", "").split(";")[0] or None
        m = re.search(r"interleaved=(\d+)-(\d+)", headers.get("transport", ""))
        self._chan = int(m.group(1)) if m else 0
        status, _, _ = await self._request(
            "PLAY", self._content_base, {"Require": REQUIRE, "Range": "npt=0.000-"}
        )
        if status != 200:
            raise RuntimeError(f"PLAY failed: {status}")
        self.ready = True
        self._rx_task = asyncio.create_task(self._drain_rx())
        self.logger.info("Backchannel ready (RTSP PLAY ok)")

    async def _drain_rx(self) -> None:
        try:
            while True:
                await self._read_response()
        except Exception:
            self.ready = False

    async def send_mulaw(self, payload: bytes) -> None:
        if not self.ready or not self._writer:
            return
        pkt = build_rtp(self._seq, self._ts, self._ssrc, payload, self._pt)
        self._seq += 1
        self._ts += len(payload)  # 8 kHz, 1 byte per sample
        async with self._lock:
            self._writer.write(
                b"$" + bytes([self._chan]) + struct.pack(">H", len(pkt)) + pkt
            )
            await self._writer.drain()

    async def close(self) -> None:
        self.ready = False
        if self._rx_task:
            self._rx_task.cancel()
        if self._writer:
            try:
                self._cseq += 1
                self._writer.write(
                    f"TEARDOWN {self._content_base} RTSP/1.0\r\nCSeq: {self._cseq}\r\n"
                    f"Session: {self._session}\r\nRequire: {REQUIRE}\r\n\r\n".encode()
                )
                await self._writer.drain()
            except Exception:
                pass
            self._writer.close()


class TalkbackBridge:
    """Feeds Protect's ADTS AAC datagrams to the camera speaker; one active talk at a time."""

    IDLE_SECONDS = 3.0

    def __init__(
        self, url: str, user: str, password: str, logger: logging.Logger
    ) -> None:
        self.url, self.user, self.password, self.logger = url, user, password, logger
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=200)
        self._task: Optional[asyncio.Task] = None

    def feed(self, datagram: bytes) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        try:
            self._queue.put_nowait(datagram)
        except asyncio.QueueFull:
            pass

    async def _run(self) -> None:
        bc = RtspBackchannel(self.url, self.user, self.password, self.logger)
        ff = None
        try:
            await bc.open()
            ff = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-f",
                "aac",
                "-i",
                "pipe:0",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-f",
                "mulaw",
                "-flush_packets",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            self.logger.info("Talkback bridge active")
            sent = 0

            async def pump_out() -> None:
                nonlocal sent
                assert ff and ff.stdout
                while True:
                    chunk = await ff.stdout.readexactly(160)
                    await bc.send_mulaw(chunk)
                    sent += 1

            out_task = asyncio.create_task(pump_out())
            while True:
                try:
                    data = await asyncio.wait_for(self._queue.get(), self.IDLE_SECONDS)
                except asyncio.TimeoutError:
                    break
                assert ff.stdin
                ff.stdin.write(data)
                await ff.stdin.drain()
                if out_task.done():
                    break
            out_task.cancel()
            self.logger.info(f"Talkback bridge ended ({sent} G.711 packets sent)")
        except Exception as e:
            self.logger.warning(f"Talkback bridge failed: {e!r}")
        finally:
            if ff and ff.returncode is None:
                ff.kill()
            await bc.close()
            while not self._queue.empty():
                self._queue.get_nowait()
