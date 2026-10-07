"""Movement gate for camera AI boxes.

Static scenery the camera misclassifies (a sprinkler read as an animal,
a neighbour's house read as a vehicle) must never become a Protect
event, while anything that genuinely moves must. The old gate latched
"moving" on the first report whose centre had drifted >= 10 units from
its origin, so a single jittery box on a static object let it through
forever — 5,312 phantom animal boxes in one 72h window.
"""

import argparse

import pytest

from unifi.cams import rtsp
from unifi.cams.rtsp import RTSPCam

REPORT_SEC = 0.4  # sidecar heartbeat cadence


@pytest.fixture
def cam(monkeypatch):
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = argparse.Namespace(
        ai_min_movement=10, ai_movement_box_ratio=0.25, ai_movement_reports=2
    )
    cam._ai_tracks = []
    cam._ai_track_seq = 0
    clock = {"t": 1000.0}
    monkeypatch.setattr(rtsp.time, "time", lambda: clock["t"])
    cam._clock = clock
    return cam


def feed(cam, coords, kind="animal"):
    """Run one box per report through the gate; return the pass flags."""
    passed = []
    for coord in coords:
        cam._clock["t"] += REPORT_SEC
        out = cam._filter_stationary([{"type": kind, "coord": list(coord)}])
        passed.append(bool(out))
    return passed


# Shapes taken from the production log for the backyard sprinkler.
STEADY = (805, 923, 34, 74)
WIDE = (804, 923, 75, 74)
TALL = (795, 895, 47, 102)


def test_sprinkler_with_one_outlier_never_passes(cam):
    assert not any(feed(cam, [STEADY] * 10 + [WIDE] + [STEADY] * 10))


def test_sprinkler_with_scattered_outliers_never_passes(cam):
    pattern = ([STEADY] * 5 + [WIDE] + [STEADY] * 5 + [TALL]) * 20
    assert not any(feed(cam, pattern))


def test_walking_person_passes_quickly(cam):
    # ~1 m/s across the yard is ~15 units per report on this camera.
    walk = [(300 + 15 * i, 500, 40, 120) for i in range(10)]
    passed = feed(cam, walk, kind="person")
    assert any(passed[:5]), passed
    assert all(passed[passed.index(True) :])


def test_person_who_stops_keeps_passing(cam):
    walk = [(300 + 15 * i, 500, 40, 120) for i in range(6)]
    stand = [walk[-1]] * 20
    passed = feed(cam, walk + stand, kind="person")
    assert all(passed[-20:])


def test_disabled_gate_passes_everything(cam):
    cam.args.ai_min_movement = 0
    assert all(feed(cam, [STEADY] * 5))
