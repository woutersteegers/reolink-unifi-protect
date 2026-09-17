"""Self-heal watchdog for stalled video streams.

The proxy launches each stream as a shell pipeline
(``ffmpeg | hevc_flv | clock_sync | nc``) and tracked only the *shell*
PID. When the camera's uplink collapses (ffmpeg alive but starved) or the
NVR drops the FLV socket (nc dies), the shell stays alive, so the old
``poll()`` check reported the dead stream as healthy forever and recording
stopped until a manual restart. These tests pin the watchdog that detects
"alive but producing no output" and restarts the pipeline.
"""

import argparse
import logging
import os

import pytest

from unifi.cams.base import SmartDetectObjectType  # noqa: F401
from unifi.cams.rtsp import RTSPCam


class FakeHandle:
    def __init__(self, rc=None, pid=4242):
        self._rc = rc
        self.pid = pid
        self.killed = False

    @property
    def returncode(self):
        return self._rc

    def poll(self):
        return self._rc

    def kill(self):
        self.killed = True
        self._rc = -9


@pytest.fixture
def cert(tmp_path_factory):
    import subprocess

    d = tmp_path_factory.mktemp("cert")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-subj",
            "/CN=t",
        ],
        check=True,
        capture_output=True,
    )
    pem = d / "client.pem"
    pem.write_bytes(key.read_bytes() + crt.read_bytes())
    return pem


@pytest.fixture
def cam(cert, monkeypatch):
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))
    calls = {"start": [], "stop": []}

    async def fake_start(idx, name, dest):
        calls["start"].append((idx, name, dest))
        cam._ffmpeg_handles[idx] = FakeHandle()

    def fake_stop(idx):
        calls["stop"].append(idx)
        cam._ffmpeg_handles.pop(idx, None)
        cam._stream_meta.pop(idx, None)

    monkeypatch.setattr(cam, "start_video_stream", fake_start)
    monkeypatch.setattr(cam, "stop_video_stream", fake_stop)
    cam._test_calls = calls
    return cam


def arm(cam, idx="video1", rc=None, bytes_out=1000):
    cam._ffmpeg_handles[idx] = FakeHandle(rc=rc)
    cam._stream_meta[idx] = ("streamName", ("10.0.0.1", 7550))
    cam._output = {idx: bytes_out}
    return idx


async def test_silent_stream_is_restarted(cam, monkeypatch):
    idx = arm(cam, bytes_out=1000)
    monkeypatch.setattr(cam, "_stream_output_bytes", lambda i: cam._output.get(i))
    progress = {}
    # t=0 establishes a baseline; output never advances afterward.
    await cam._watchdog_tick(progress, now=0.0)
    await cam._watchdog_tick(progress, now=cam.STREAM_SILENCE_LIMIT_SEC - 1)
    assert cam._test_calls["stop"] == []  # not yet past the limit
    await cam._watchdog_tick(progress, now=cam.STREAM_SILENCE_LIMIT_SEC + 1)
    assert cam._test_calls["stop"] == [idx]
    assert cam._test_calls["start"] and cam._test_calls["start"][0][0] == idx


async def test_flowing_stream_not_restarted(cam, monkeypatch):
    idx = arm(cam, bytes_out=1000)
    monkeypatch.setattr(cam, "_stream_output_bytes", lambda i: cam._output.get(i))
    progress = {}
    for n in range(6):
        cam._output[idx] = 1000 + n * 50_000  # bytes keep flowing
        await cam._watchdog_tick(progress, now=n * cam.STREAM_WATCHDOG_INTERVAL_SEC)
    assert cam._test_calls["stop"] == []
    assert cam._test_calls["start"] == []


async def test_exited_stream_is_respawned(cam, monkeypatch):
    idx = arm(cam, rc=1)  # pipeline exited outright
    monkeypatch.setattr(cam, "_stream_output_bytes", lambda i: None)
    await cam._watchdog_tick({}, now=0.0)
    assert cam._test_calls["start"] and cam._test_calls["start"][0][0] == idx


async def test_protect_stopped_stream_is_left_alone(cam, monkeypatch):
    # Protect asked for the stream off: no handle, no meta -> watchdog skips.
    monkeypatch.setattr(cam, "_stream_output_bytes", lambda i: None)
    await cam._watchdog_tick({}, now=0.0)
    assert cam._test_calls["start"] == []
    assert cam._test_calls["stop"] == []


async def test_unmeasurable_output_is_not_restarted(cam, monkeypatch):
    # /proc unavailable (e.g. dev machine) -> None -> never a false restart.
    arm(cam, bytes_out=1000)
    monkeypatch.setattr(cam, "_stream_output_bytes", lambda i: None)
    progress = {}
    for n in range(5):
        await cam._watchdog_tick(progress, now=n * 60.0)
    assert cam._test_calls["stop"] == []


def test_real_stop_kills_group_and_forgets_stream(cert, monkeypatch):
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))
    h = FakeHandle(pid=4242)
    cam._ffmpeg_handles["video1"] = h
    cam._stream_meta["video1"] = ("s", ("10.0.0.1", 7550))

    killed_groups = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed_groups.append(pgid))

    cam.stop_video_stream("video1")

    assert killed_groups == [4242]  # whole pipeline group, not just the shell
    assert "video1" not in cam._ffmpeg_handles
    assert "video1" not in cam._stream_meta


def test_real_stop_falls_back_when_group_gone(cert, monkeypatch):
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))
    h = FakeHandle(pid=4242)
    cam._ffmpeg_handles["video1"] = h

    def boom(pid):
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", boom)
    cam.stop_video_stream("video1")
    assert h.killed
    assert "video1" not in cam._ffmpeg_handles


def test_close_streams_tolerates_handle_removal(cert):
    # stop now mutates _ffmpeg_handles; close_streams must not choke.
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))
    for i in ("video1", "video2", "video3"):
        cam._ffmpeg_handles[i] = FakeHandle(pid=1)
    cam.close_streams()  # must not raise "dict changed size during iteration"
    assert cam._ffmpeg_handles == {}
