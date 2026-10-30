"""A camera that stops answering must not stall the Protect connection.

Real risk on Wi-Fi Reolinks: a snapshot fetch to a camera that dropped off the network
hung for aiohttp's default 5 minutes, inside the websocket message loop, so every other
Protect request waited behind it; the eventual timeout was not a ClientError and escaped.
"""

import argparse
import asyncio
import json
import logging
import time

import aiohttp

from unifi.cams.rtsp import RTSPCam

LOG = logging.getLogger("test")


def make_cam() -> RTSPCam:
    c = RTSPCam.__new__(RTSPCam)
    c.logger = LOG
    c.args = argparse.Namespace()
    c._msg_id = 0
    c._init_time = time.time()
    c._smart_snapshot_files = {}
    c._ssl_context = False
    c._session = None
    return c


async def _silent_server():
    """A local TCP server that accepts and then never answers."""
    held = []

    async def handle(reader, writer):
        held.append(writer)
        await asyncio.sleep(3600)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def test_fetch_to_file_gives_up_on_a_silent_camera(tmp_path, caplog):
    cam = make_cam()
    cam.SNAPSHOT_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=0.5)

    async def scenario():
        server, port = await _silent_server()
        try:
            started = time.monotonic()
            ok = await cam.fetch_to_file(
                f"http://admin:secret@127.0.0.1:{port}/snap?password=secret",
                tmp_path / "screen.jpg",
            )
            return ok, time.monotonic() - started
        finally:
            server.close()

    with caplog.at_level("WARNING"):
        ok, elapsed = asyncio.run(scenario())
    assert ok is False
    assert elapsed < 5
    assert "Snapshot fetch failed" in caplog.text
    assert not (tmp_path / "screen.jpg").exists()


def test_snapshot_request_does_not_block_the_message_loop(tmp_path):
    cam = make_cam()
    sent = []

    async def send(msg):
        sent.append(msg)

    cam.send = send

    async def scenario():
        gate = asyncio.Event()

        async def slow_snapshot_request(m):
            await gate.wait()
            return cam.gen_response("GetRequest", response_to=m["messageId"])

        cam.process_snapshot_request = slow_snapshot_request
        get = {
            "functionName": "GetRequest",
            "messageId": 7,
            "responseExpected": True,
            "payload": {"what": "snapshot", "uri": "http://127.0.0.1:1/x"},
        }
        time_req = {
            "functionName": "ubnt_avclient_time",
            "messageId": 8,
            "responseExpected": True,
            "payload": {},
        }
        # Both handled immediately although the snapshot is still pending.
        await asyncio.wait_for(cam.process(json.dumps(get).encode()), timeout=1)
        await asyncio.wait_for(cam.process(json.dumps(time_req).encode()), timeout=1)
        assert [m["inResponseTo"] for m in sent] == [8]
        gate.set()
        await asyncio.sleep(0.05)
        assert [m["inResponseTo"] for m in sent] == [8, 7]

    asyncio.run(scenario())


def test_a_failing_snapshot_request_is_logged_not_raised(caplog):
    cam = make_cam()

    async def boom(m):
        raise RuntimeError("camera went away")

    cam.process_snapshot_request = boom

    async def scenario():
        msg = {
            "functionName": "GetRequest",
            "messageId": 1,
            "responseExpected": True,
            "payload": {"what": "snapshot", "uri": "http://127.0.0.1:1/x"},
        }
        assert await cam.process(json.dumps(msg).encode()) is False
        await asyncio.sleep(0.05)

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())
    assert "Snapshot request failed" in caplog.text


def test_close_cancels_pending_snapshot_requests():
    cam = make_cam()

    async def scenario():
        task = cam._spawn_request(asyncio.sleep(3600))
        cam._cancel_request_tasks()
        await asyncio.gather(task, return_exceptions=True)
        return task

    assert asyncio.run(scenario()).cancelled()


def test_redacts_the_query_string_password_too():
    # fetch_to_file logs the exception text, which can include the URL.
    assert "secret" not in RTSPCam.redact_secrets(
        "https://192.168.30.36/cgi-bin/api.cgi?cmd=Snap&user=admin&password=secret"
    )
    assert "secret" not in RTSPCam.redact_secrets("rtsp://admin:secret@cam/x")
