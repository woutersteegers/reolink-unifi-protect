"""Protect's nightly firmware push must not knock the camera offline.

When the advertised --fw-version falls behind Ubiquiti's latest release,
Protect sends UpdateFirmwareRequest at the auto-update hour. The proxy
"pretends" to upgrade by adopting the new version and reconnecting. It
used to parse that version out of the firmware binary's header, which is
obfuscated on current (sav837gw) firmware, so it reconnected with 50
bytes of junk as fwVersion; Protect closed the socket with a clean 1000,
_run only caught ConnectionClosedError, and the process crashed — about
50 s of lost recording every night.
"""

import argparse
import asyncio
import logging

import pytest
import websockets.exceptions

from unifi.cams import base
from unifi.cams.rtsp import RTSPCam
from unifi.core import RetryableError

URI = (
    "https://fw-download.ubnt.com/data/uvc/"
    "e7fc-sav837gw-5.4.132-8da7f8fd-e4f6-4d94-9705-ea8e2fc9f934.bin"
)


@pytest.fixture
def cam():
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = argparse.Namespace(
        fw_version="5.4.122", sysid="0xa598", host="192.0.2.1"
    )
    cam.logger = logging.getLogger("test")
    return cam


def upgrade_msg(uri):
    return {"functionName": "UpdateFirmwareRequest", "payload": {"uri": uri}}


class NoNetwork:
    def __init__(self, *a, **kw):
        raise AssertionError("must not download the firmware binary")


async def test_version_taken_from_download_filename(cam, monkeypatch):
    monkeypatch.setattr(base.aiohttp, "ClientSession", NoNetwork)
    await cam.process_upgrade(upgrade_msg(URI))
    assert cam.args.fw_version == "5.4.132"


async def test_unparseable_header_keeps_current_version(cam, monkeypatch):
    async def junk_header(uri):
        return bytes(range(0x80, 0xB6))  # obfuscated, like sav837gw .bin

    monkeypatch.setattr(cam, "_fetch_firmware_header", junk_header)
    await cam.process_upgrade(upgrade_msg("https://fw.example/firmware.bin"))
    assert cam.args.fw_version == "5.4.122"


async def test_legacy_plaintext_header_still_parsed(cam, monkeypatch):
    cam.args = argparse.Namespace(fw_version="UVC.S2L.v4.23.8.67.0eba6e3.200526.1046")
    new = b"UVC.S2L.v4.30.0.67.1234567.210101.0000"

    async def header(uri):
        return b"\x00" * 4 + new + b"\x00" * (50 - len(new))

    monkeypatch.setattr(cam, "_fetch_firmware_header", header)
    await cam.process_upgrade(upgrade_msg("https://fw.example/firmware.bin"))
    assert cam.args.fw_version == new.decode()


class ClosingSocket:
    def __init__(self, exc):
        self.exc = exc

    async def recv(self):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        websockets.exceptions.ConnectionClosedOK(None, None),
        websockets.exceptions.ConnectionClosedError(None, None),
    ],
)
async def test_any_close_is_retried_not_fatal(cam, monkeypatch, exc):
    async def noop():
        return None

    monkeypatch.setattr(cam, "init_adoption", noop)
    monkeypatch.setattr(cam, "_watch_streams", noop)
    cam._watchdog_task = None
    with pytest.raises(RetryableError):
        await cam._run(ClosingSocket(exc))
    await asyncio.sleep(0)  # let the watchdog stub finish
