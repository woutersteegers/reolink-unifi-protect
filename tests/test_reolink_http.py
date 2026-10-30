"""Cameras with HTTPS turned off, and passwords that break user/password URLs.

Real case: a Reolink Elite Floodlight WiFi (firmware v3.2.0.5607) serves its API on plain
HTTP only, and rejects ``?user=...&password=...`` logins whenever the password has
special characters, even percent-encoded, while the token login (password in the POST
body) works. ``--reolink-scheme http`` and ``--reolink-snapshot`` cover both.
"""

import argparse
import asyncio
import json
import logging
import urllib.parse

import pytest
from aiohttp import web

from unifi.cams.rtsp import RTSPCam
from unifi.reolink_isp import ReolinkIsp
from unifi.reolink_ptz import ReolinkPtz

LOG = logging.getLogger("test")
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
PASSWORD = "p@ss&w#rd%+"


async def fake_camera(
    expire_first_token=False,
    *,
    login_delay=0,
    snapshot_status=200,
    snapshot_body=None,
    expired_token_response="json-list",
):
    """A local stand-in for a Reolink's /cgi-bin/api.cgi over plain HTTP."""
    state = {"logins": 0, "snaps": 0}

    async def api(request):
        cmd = request.query.get("cmd")
        if cmd == "Login":
            body = await request.json()
            user = body[0]["param"]["User"]
            state["logins"] += 1
            if user["password"] != PASSWORD:
                return web.json_response(
                    [{"cmd": "Login", "code": 1, "error": {"rspCode": -7}}]
                )
            token = f"tok{state['logins']}"
            await asyncio.sleep(login_delay)
            return web.json_response(
                [{"cmd": "Login", "code": 0, "value": {"Token": {"name": token}}}]
            )
        if cmd == "Snap":
            state["snaps"] += 1
            if snapshot_body is not None or snapshot_status != 200:
                return web.Response(body=snapshot_body, status=snapshot_status)
            if expire_first_token and request.query.get("token") == "tok1":
                error = {"cmd": "Snap", "code": 1, "error": {"rspCode": -6}}
                if expired_token_response == "http401":
                    return web.Response(status=401, text="Unauthorized")
                if expired_token_response == "json-object":
                    return web.json_response(error)
                return web.json_response([error])
            if not request.query.get("token", "").startswith("tok"):
                return web.json_response([{"cmd": "Snap", "code": 1}])
            return web.Response(body=JPEG, content_type="image/jpeg")
        return web.json_response([{"cmd": cmd, "code": 0}])

    app = web.Application()
    app.router.add_route("*", "/cgi-bin/api.cgi", api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"127.0.0.1:{port}", state


async def test_snapshot_uses_a_token_login_over_http(tmp_path):
    runner, host, state = await fake_camera()
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        dst = tmp_path / "screen.jpg"
        assert await client.snapshot(dst) is True
        assert dst.read_bytes() == JPEG
        assert await client.snapshot(dst) is True
        assert state == {"logins": 1, "snaps": 2}  # the token is reused
    finally:
        await runner.cleanup()


async def test_an_expired_token_logs_in_again(tmp_path):
    runner, host, state = await fake_camera(expire_first_token=True)
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        assert await client.snapshot(tmp_path / "screen.jpg") is True
        assert state["logins"] == 2
    finally:
        await runner.cleanup()


async def test_a_wrong_password_fails_cleanly(tmp_path, caplog):
    runner, host, state = await fake_camera()
    try:
        client = ReolinkIsp(host, "admin", "wrong", LOG, scheme="http")
        with caplog.at_level("WARNING"):
            assert await client.snapshot(tmp_path / "screen.jpg") is False
        assert "login rejected" in caplog.text
        assert state["logins"] == 1  # no retry storm against the camera
        assert not (tmp_path / "screen.jpg").exists()
    finally:
        await runner.cleanup()


async def test_an_unreachable_camera_fails_cleanly(tmp_path, caplog):
    client = ReolinkIsp("127.0.0.1:1", "admin", PASSWORD, LOG, scheme="http")
    with caplog.at_level("WARNING"):
        assert await client.snapshot(tmp_path / "screen.jpg") is False
    assert "Reolink snapshot failed" in caplog.text


def test_clients_default_to_https_and_honour_the_scheme():
    assert ReolinkIsp("cam", "u", "p", LOG)._url("cmd=x").startswith("https://cam/")
    http_client = ReolinkIsp("cam", "u", "p", LOG, scheme="http")
    assert http_client._url("cmd=x").startswith("http://cam/")
    assert ReolinkPtz("cam", "u", "p", LOG, scheme="http").scheme == "http"


def make_cam(tmp_path, **args) -> RTSPCam:
    c = RTSPCam.__new__(RTSPCam)
    c.logger = LOG
    c.snapshot_dir = str(tmp_path)
    c.args = argparse.Namespace(
        snapshot_url=None,
        reolink_ai_host="192.0.2.10",
        reolink_ai_user="admin",
        reolink_ai_password=PASSWORD,
        reolink_snapshot=False,
        reolink_scheme="https",
    )
    for k, v in args.items():
        setattr(c.args, k, v)
    return c


async def test_reolink_snapshot_replaces_the_rtsp_snapshot_stream(tmp_path):
    cam = make_cam(tmp_path, reolink_snapshot=True, reolink_scheme="http")
    assert cam._http_snapshots()
    fetched = []

    class FakeClient:
        async def snapshot(self, dst):
            fetched.append(dst)
            dst.write_bytes(JPEG)
            return True

    cam._isp = FakeClient()
    cam._isp_state = {}

    def no_stream():
        raise AssertionError("must not open an RTSP snapshot session")

    cam.start_snapshot_stream = no_stream
    path = await cam.get_snapshot()
    assert fetched == [path]
    assert path.read_bytes() == JPEG


def test_plain_cameras_keep_the_rtsp_snapshot_stream(tmp_path):
    assert not make_cam(tmp_path)._http_snapshots()
    assert make_cam(tmp_path, snapshot_url="http://cam/snap")._http_snapshots()


def test_api_url_follows_the_scheme(tmp_path):
    url = make_cam(tmp_path, reolink_scheme="http")._reolink_api_url("GetAiState")
    assert url.startswith("http://192.0.2.10/cgi-bin/api.cgi?")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["password"] == [
        PASSWORD
    ]
    assert make_cam(tmp_path)._reolink_api_url("GetAiState").startswith("https://")


@pytest.mark.parametrize("expired", [False, True])
async def test_concurrent_snapshots_share_login_and_refresh(tmp_path, expired):
    runner, host, state = await fake_camera(
        expire_first_token=expired, login_delay=0.03
    )
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        paths = [tmp_path / f"snapshot-{i}.jpg" for i in range(5)]
        results = await asyncio.wait_for(
            asyncio.gather(*(client.snapshot(path) for path in paths)), timeout=3
        )
        assert results == [True] * 5
        assert all(path.read_bytes() == JPEG for path in paths)
        assert state == {"logins": 2 if expired else 1, "snaps": 6 if expired else 5}
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("operation", ["apply", "reboot"])
async def test_snapshot_shares_session_lock_with_existing_operations(
    tmp_path, operation
):
    runner, host, state = await fake_camera(login_delay=0.03)
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        other = (
            client.apply({"brightness": 50})
            if operation == "apply"
            else client.reboot()
        )
        snapshot, result = await asyncio.wait_for(
            asyncio.gather(client.snapshot(tmp_path / "screen.jpg"), other),
            timeout=3,
        )
        assert snapshot is True
        assert state == {"logins": 1, "snaps": 1}
        if operation == "apply":
            assert client._applied == {"brightness": 50}
        else:
            assert result is True
            assert client._token is None
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "status,body",
    [
        (503, f"Camera unavailable: {PASSWORD}, tok1".encode()),
        (403, b"Permission denied"),
        (200, json.dumps([{"code": 1, "error": {"rspCode": -3}}]).encode()),
        (200, b"<html>Camera is busy</html>"),
        (503, JPEG),
    ],
)
async def test_non_auth_snapshot_failure_preserves_session_and_last_image(
    tmp_path, caplog, status, body
):
    runner, host, state = await fake_camera(snapshot_status=status, snapshot_body=body)
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        dst = tmp_path / "screen.jpg"
        previous = JPEG + b"previous"
        dst.write_bytes(previous)
        with caplog.at_level("WARNING"):
            assert await client.snapshot(dst) is False
        assert state == {"logins": 1, "snaps": 1}
        assert client._token == "tok1"
        assert dst.read_bytes() == previous
        assert "Reolink snapshot failed" in caplog.text
        assert PASSWORD not in caplog.text and "tok1" not in caplog.text
        assert await client.snapshot(dst) is False
        assert state == {"logins": 1, "snaps": 2}
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("response", ["json-list", "json-object", "http401"])
async def test_snapshot_refreshes_only_for_explicit_auth_failures(tmp_path, response):
    runner, host, state = await fake_camera(
        expire_first_token=True, expired_token_response=response
    )
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        dst = tmp_path / "screen.jpg"
        assert await client.snapshot(dst) is True
        assert dst.read_bytes() == JPEG
        assert state == {"logins": 2, "snaps": 2}
    finally:
        await runner.cleanup()


async def test_persistent_auth_failure_refreshes_once(tmp_path):
    runner, host, state = await fake_camera(
        snapshot_status=401, snapshot_body=b"Unauthorized"
    )
    try:
        client = ReolinkIsp(host, "admin", PASSWORD, LOG, scheme="http")
        assert await client.snapshot(tmp_path / "screen.jpg") is False
        assert state == {"logins": 2, "snaps": 2}
        assert client._token is None
    finally:
        await runner.cleanup()
