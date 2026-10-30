"""A snapshot request landing before the capture process's first frame exists.

Real bug (session 2026-10-12): a GetRequest arriving right after adoption/reconnect, before
the ffmpeg-fed screen.jpg existed yet, failed outright with no retry ("not ready yet, skipping
upload") and nothing asked again, leaving Protect's PTZ panel with no image for that request.
process_snapshot_request now waits briefly for the file instead of giving up immediately.
"""

import argparse
import asyncio
import logging
from pathlib import Path

import pytest

from unifi.cams.rtsp import RTSPCam

LOG = logging.getLogger("test")


def make_cam(snapshot_path: Path) -> RTSPCam:
    c = RTSPCam.__new__(RTSPCam)
    c.logger = LOG
    c.args = argparse.Namespace()
    c._smart_snapshot_files = {}
    c._ssl_context = (
        False  # aiohttp accepts False for "no TLS verification needed here"
    )

    async def get_snapshot():
        return snapshot_path

    c.get_snapshot = get_snapshot
    return c


def msg(uri="http://127.0.0.1:1/upload"):
    return {
        "payload": {"what": "snapshot", "uri": uri, "formFields": {}},
        "responseExpected": False,
        "messageId": 1,
    }


def test_waits_for_a_file_that_appears_shortly_after(tmp_path, caplog):
    path = tmp_path / "screen.jpg"
    cam = make_cam(path)

    async def write_shortly():
        await asyncio.sleep(0.3)
        path.write_bytes(b"\xff\xd8\xff\xd9")

    async def scenario():
        writer = asyncio.create_task(write_shortly())
        with caplog.at_level("WARNING"):
            await cam.process_snapshot_request(msg())
        await writer

    asyncio.run(scenario())
    assert path.exists()
    assert "not ready yet" not in caplog.text


def test_gives_up_after_the_bounded_wait(tmp_path, caplog):
    path = tmp_path / "screen.jpg"  # never written
    cam = make_cam(path)

    async def scenario():
        with caplog.at_level("WARNING"):
            await cam.process_snapshot_request(msg())

    asyncio.run(scenario())
    assert "not ready yet" in caplog.text


def test_network_guard_blocks_real_upload_targets(tmp_path):
    # process_snapshot_request posts the file straight to whatever uri Protect gave it;
    # confirm a non-local target is still caught by the test-wide guard, not just the
    # PTZ/ISP modules' own clients.
    path = tmp_path / "screen.jpg"
    path.write_bytes(b"\xff\xd8\xff\xd9")
    cam = make_cam(path)

    async def scenario():
        await cam.process_snapshot_request(msg(uri="http://192.0.2.156:1/upload"))

    with pytest.raises(AssertionError, match="192.0.2.156"):
        asyncio.run(scenario())
