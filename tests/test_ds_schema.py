"""EventSmartDetect frames must satisfy the typed parse in Protect's ds daemon.

ds (the device service that now owns smart-detect events) rejects a frame it cannot
parse and relays it raw to Protect, which logs "Received EventSmartDetect over avclient
which was not expected". Two real failures, both read from ds.log:
  - "missing field `level`": every zonesStatus entry needs a level next to its status
  - "invalid value: integer 179181301300, expected u32": trackerID overflowed because
    the event id was seeded from the epoch
"""

import argparse
import logging
import subprocess
import tempfile
from pathlib import Path

import pytest

from unifi.cams.base import SmartDetectObjectType
from unifi.cams.rtsp import RTSPCam

U32_MAX = 2**32 - 1
FAKE_JPEG = b"\xff\xd8\xff" + b"\0" * 64 + b"\xff\xd9"


@pytest.fixture
def cert(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("cert")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes"]
        + ["-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", "/CN=t"],
        check=True,
        capture_output=True,
    )
    pem = d / "client.pem"
    pem.write_bytes(key.read_bytes() + crt.read_bytes())
    return pem


@pytest.fixture
def cam(tmp_path, cert, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    args = argparse.Namespace(
        cert=str(cert),
        source=["rtsp://cam/main"],
        snapshot_url="http://cam/snap",
        rtsp_transport="tcp",
        hi_width=1920,
        hi_height=1080,
    )
    c = RTSPCam(args, logging.getLogger("test"))
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(FAKE_JPEG)
    c.sent = []

    async def send(msg):
        c.sent.append(msg)

    async def get_snapshot():
        return frame

    monkeypatch.setattr(c, "send", send)
    monkeypatch.setattr(c, "get_snapshot", get_snapshot)
    return c


def zone_entries(payload):
    return list(payload.get("zonesStatus", {}).values())


async def test_every_zone_status_has_a_level(cam):
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    descriptors = [
        cam.build_smart_descriptor("person", [100, 100, 200, 200], 0.9, 5, zones=[1])
    ]
    await cam.trigger_motion_update(descriptors)
    await cam.trigger_motion_stop()

    frames = [m["payload"] for m in cam.sent if m["functionName"] == "EventSmartDetect"]
    assert [p["edgeType"] for p in frames] == ["enter", "moving", "leave"]
    for p in frames:
        entries = zone_entries(p)
        assert entries
        for z in entries:
            assert isinstance(z["level"], int) and 0 <= z["level"] <= 100
            assert "status" in z


async def test_ids_fit_in_u32_even_after_many_events(cam):
    cam._motion_event_id = 100_000  # a very busy camera
    await cam.trigger_motion_start(SmartDetectObjectType.VEHICLE)
    start = next(
        m["payload"] for m in cam.sent if m["functionName"] == "EventSmartDetect"
    )
    assert start["eventId"] <= U32_MAX
    for d in start["descriptors"]:
        assert d["trackerID"] <= U32_MAX


async def test_fresh_process_starts_with_small_ids(cam):
    # Regression: seeding from time.time() made trackerID ~1.8e11 (> u32).
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    start = next(
        m["payload"] for m in cam.sent if m["functionName"] == "EventSmartDetect"
    )
    assert start["eventId"] < 1_000_000
    assert all(d["trackerID"] <= U32_MAX for d in start["descriptors"])


async def test_event_envelopes_carry_an_rfc3339_timestamp(cam):
    from datetime import datetime

    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    await cam.trigger_motion_stop()
    frames = [m for m in cam.sent if m["functionName"] == "EventSmartDetect"]
    assert len(frames) == 2
    for m in frames:
        # ds rejects an integer here ("expected an RFC 3339 formatted date and time")
        assert isinstance(m["timeStamp"], str)
        assert datetime.fromisoformat(m["timeStamp"]).tzinfo is not None


def test_handshake_mac_is_the_canonical_12_hex_form(cert):
    from unifi.core import Core

    args = argparse.Namespace(
        host="h", token="t", mac="02:aa:bb:cc:dd:ee", cert=str(cert)
    )
    core = Core(args, camera=None, logger=logging.getLogger("test"))
    # ds looks the camera up by this header; Protect stores it without colons.
    assert core._headers()["camera-mac"] == "02AABBCCDDEE"


async def test_leave_frame_announces_a_servable_full_fov_image(cam):
    # Real cameras name a full-frame jpeg + its size in the leave frame; ds then asks
    # for it and stores thumbnailFullfovId. Without it the event has no wide view.
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    await cam.trigger_motion_stop()
    leave = [m["payload"] for m in cam.sent if m["payload"]["edgeType"] == "leave"][0]
    name = leave["smartDetectSnapshotFullFoV"]
    assert name in cam._smart_snapshot_files
    assert cam._smart_snapshot_files[name].exists()
    assert "smartDetectSnapshotFullFoVWidth" in leave
    assert "smartDetectSnapshotFullFoVHeight" in leave


REAL_DESCRIPTOR_KEYS = {
    "attributes", "boxColor", "confidenceLevel", "coord", "coord3d", "firstShownTimeMs",
    "idleSinceTimeMs", "intelligenceZones", "lines", "loiterZones", "name", "objectType",
    "secondLensZones", "stationary", "tag", "trackerID", "zones",
}  # fmt: skip


def test_descriptor_has_exactly_the_fields_a_real_camera_sends(cam):
    d = cam.build_smart_descriptor("person", [100, 100, 200, 200], 0.9, 5)
    assert set(d) == REAL_DESCRIPTOR_KEYS
    assert d["boxColor"] == "red" and d["name"] == "" and d["attributes"] is None
    assert d["coord3d"] == [-1, -1] and d["idleSinceTimeMs"] == 0


def test_box_colours_follow_the_class_and_first_shown_is_stable_per_object(cam):
    colours = {
        t: cam.build_smart_descriptor(t, [0, 0, 1, 1], 0.9, i)["boxColor"]
        for i, t in enumerate(("person", "vehicle", "animal"))
    }
    assert colours == {"person": "red", "vehicle": "cyan", "animal": "white"}
    a = cam.build_smart_descriptor("person", [0, 0, 1, 1], 0.9, 7)
    b = cam.build_smart_descriptor("person", [5, 5, 1, 1], 0.9, 7)
    assert a["firstShownTimeMs"] == b["firstShownTimeMs"]


async def test_enter_and_moving_frames_carry_the_empty_snapshot_fields(cam):
    await cam.trigger_motion_start(SmartDetectObjectType.PERSON)
    desc = [cam.build_smart_descriptor("person", [1, 2, 3, 4], 0.9, 9)]
    await cam.trigger_motion_update(desc)
    frames = [m["payload"] for m in cam.sent if m["functionName"] == "EventSmartDetect"]
    assert [f["edgeType"] for f in frames] == ["enter", "moving"]
    for f in frames:
        assert f["smartDetectSnapshots"] == [] and f["smartDetectSnapshotFullFoV"] == ""
        assert f["smartDetectSnapshotFullFoVWidth"] == 0
