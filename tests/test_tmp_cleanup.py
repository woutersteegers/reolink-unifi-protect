"""Scratch-file hygiene for the camera adapters.

Regression tests for the /tmp leak that filled the host's docker.img:
every motion event leaked a full-frame snapshot, every tracked object
leaked its crop source frame and its finished crop, and every restart
orphaned the whole scratch directory.
"""

import argparse
import asyncio
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from unifi.cams.base import SmartDetectObjectType
from unifi.cams.rtsp import RTSPCam

# Smallest thing that survives being copied around as a "JPEG".
FAKE_JPEG = b"\xff\xd8\xff" + b"\0" * 64 + b"\xff\xd9"
BOX = [400, 400, 100, 100]


@pytest.fixture
def cert(tmp_path_factory) -> Path:
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
def scratch(tmp_path, monkeypatch) -> Path:
    """Route every tempfile.* call into a directory the test can audit."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@pytest.fixture
def cam(scratch, cert, monkeypatch) -> RTSPCam:
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",  # keeps __init__ from spawning ffmpeg
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))

    frame = scratch / "frame.jpg"
    frame.write_bytes(FAKE_JPEG)

    async def send(msg):
        pass

    async def get_snapshot():
        return frame

    async def fetch_to_file(url, dst):
        dst.write_bytes(FAKE_JPEG)
        return True

    monkeypatch.setattr(cam, "send", send)
    monkeypatch.setattr(cam, "get_snapshot", get_snapshot)
    monkeypatch.setattr(cam, "fetch_to_file", fetch_to_file)
    return cam


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Stand in for the ffmpeg crop: just write the output file."""

    class Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def exec_(*argv, **kw):
        Path(argv[-1]).write_bytes(FAKE_JPEG)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)


def loose_files(scratch: Path) -> list[Path]:
    """Files dropped directly into the temp dir (motion snapshots)."""
    return [p for p in scratch.iterdir() if p.is_file() and p.name != "frame.jpg"]


async def test_motion_snapshots_do_not_accumulate(cam, scratch):
    for _ in range(5):
        await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
        await cam.trigger_motion_stop()
    assert len(loose_files(scratch)) <= 1
    assert cam._motion_snapshot is not None and cam._motion_snapshot.exists()


async def test_crop_source_frame_removed_after_crop(cam, fake_ffmpeg):
    cam._motion_event_ts = time.time()
    await cam._make_object_crop(1, "person", BOX)
    d = Path(cam.snapshot_dir)
    assert not (d / "crop_src_1.jpg").exists()
    # The finished crop must survive: Protect fetches it after the stop payload.
    assert (d / "detect_1.jpg").exists()
    assert cam._smart_snapshot_files["detect_1.jpg"] == d / "detect_1.jpg"


def age(path: Path, cam: RTSPCam) -> None:
    stale = time.time() - cam.CROP_RETENTION_SEC - 1
    os.utime(path, (stale, stale))


async def test_crop_from_snapshot_stream_keeps_screen_jpg(
    scratch, cert, monkeypatch, fake_ffmpeg
):
    # Without --snapshot-url the crop source IS the live screen.jpg;
    # cleaning up the "fetched frame" must not delete it.
    monkeypatch.setattr(RTSPCam, "start_snapshot_stream", lambda self: None)
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url=None,
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    cam = RTSPCam(args, logging.getLogger("test"))
    screen = Path(cam.snapshot_dir, "screen.jpg")
    screen.write_bytes(FAKE_JPEG)
    cam._motion_event_ts = time.time()

    await cam._make_object_crop(1, "person", BOX)

    assert screen.exists()
    assert Path(cam.snapshot_dir, "detect_1.jpg").exists()
    assert not Path(cam.snapshot_dir, "crop_src_1.jpg").exists()


async def test_stale_detect_crops_are_pruned(cam, fake_ffmpeg):
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    await cam._make_object_crop(1, "person", BOX)
    await cam.trigger_motion_stop()  # crop 1 now belongs to a finished event
    old = Path(cam.snapshot_dir, "detect_1.jpg")
    age(old, cam)

    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    await cam._make_object_crop(2, "person", BOX)

    assert not old.exists()
    assert "detect_1.jpg" not in cam._smart_snapshot_files
    assert Path(cam.snapshot_dir, "detect_2.jpg").exists()


async def test_crops_of_open_event_survive_pruning(cam, fake_ffmpeg):
    # A subject loitering longer than the retention window must still
    # get its crop announced and fetched at event stop.
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    await cam._make_object_crop(1, "person", BOX)
    old = Path(cam.snapshot_dir, "detect_1.jpg")
    age(old, cam)

    await cam._make_object_crop(2, "person", BOX)

    assert old.exists()
    assert "detect_1.jpg" in cam._smart_snapshot_files


async def test_fresh_detect_crops_are_kept(cam, fake_ffmpeg):
    cam._motion_event_ts = time.time()
    await cam._make_object_crop(1, "person", BOX)
    await cam._make_object_crop(2, "person", BOX)
    assert Path(cam.snapshot_dir, "detect_1.jpg").exists()
    assert "detect_1.jpg" in cam._smart_snapshot_files


async def test_close_removes_scratch_files(cam, scratch):
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    snap = cam._motion_snapshot
    assert snap is not None and snap.exists()

    await cam.close()

    # Emptied, not removed: Core reuses the instance across reconnects.
    assert list(Path(cam.snapshot_dir).iterdir()) == []
    assert not snap.exists()
    assert loose_files(scratch) == []
