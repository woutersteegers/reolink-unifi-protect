"""The bundled AI box sidecar: how the proxy drives it (no Node needed)."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

import pytest

from unifi.cams.rtsp import RTSPCam


def make_cam(**over):
    c = RTSPCam.__new__(RTSPCam)
    args = dict(
        reolink_ai_host="cam",
        reolink_ai_user="u",
        reolink_ai_password="p",
        ai_sink_port=8099,
        ai_sidecar=True,
        ai_sidecar_profile="main",
        ai_sidecar_always_on=False,
    )
    args.update(over)
    c.args = argparse.Namespace(**args)
    c.logger = logging.getLogger("test")
    c._ai_active = None
    c._motion_event_ts = None
    c._motion_event_id = 0
    c._smart_snapshots = []
    c._smart_snapshot_files = {}
    return c


def test_state_is_idle_until_a_detection_is_in_progress():
    c = make_cam()

    def state():
        resp = asyncio.run(c._handle_sidecar_state(None))
        return json.loads(resp.text)["active"]

    assert state() is False
    c._ai_active = object()  # the push channel reports a detection
    assert state() is True
    c._ai_active, c._motion_event_ts = None, 123.0  # an event is still open
    assert state() is True


def test_on_demand_env_points_the_sidecar_at_loopback_and_the_main_stream():
    env = make_cam()._sidecar_env()
    assert env["CAM_IP"] == "cam" and env["CAM_USER"] == "u" and env["CAM_PASS"] == "p"
    assert env["PROFILE"] == "main"
    assert env["SINK_URL"] == "http://127.0.0.1:8099/detections"
    assert env["STATE_URL"] == "http://127.0.0.1:8099/state"


def test_always_on_has_no_state_url_so_the_stream_stays_open():
    env = make_cam(ai_sidecar_always_on=True)._sidecar_env()
    assert "STATE_URL" not in env


def test_only_the_meaningful_sidecar_lines_are_logged_at_info(caplog):
    c = make_cam()
    with caplog.at_level(logging.DEBUG, logger="test"):
        c._log_sidecar_line("stream on")
        c._log_sidecar_line("[SocketPool] Socket connected")
        c._log_sidecar_line("baichuan session ended: boom")
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["AI sidecar: stream on"] == logging.INFO
    assert levels["AI sidecar: [SocketPool] Socket connected"] == logging.DEBUG
    assert levels["AI sidecar: baichuan session ended: boom"] == logging.INFO


def test_real_boxes_replace_the_centred_placeholder_crop(tmp_path):
    c = make_cam()
    placeholder = c.synthetic_tracker_id()  # (0 + 1) * 100
    crop = Path(tmp_path, f"detect_{placeholder}.jpg")
    crop.write_bytes(b"x")
    c._smart_snapshot_files[crop.name] = crop
    c._smart_snapshots = [
        {"trackerID": placeholder, "smartDetectSnapshot": crop.name},
        {"trackerID": 3, "smartDetectSnapshot": "detect_3.jpg"},
    ]
    c._drop_placeholder_crop()
    assert [e["trackerID"] for e in c._smart_snapshots] == [3]
    assert crop.name not in c._smart_snapshot_files and not crop.exists()


def test_dropping_a_placeholder_that_is_not_there_is_a_no_op():
    c = make_cam()
    c._smart_snapshots = [{"trackerID": 3, "smartDetectSnapshot": "detect_3.jpg"}]
    c._drop_placeholder_crop()
    assert len(c._smart_snapshots) == 1


@pytest.mark.parametrize("flag", [True, False])
def test_flag_defaults_parse(flag):
    parser = argparse.ArgumentParser()
    RTSPCam.add_parser(parser.add_subparsers().add_parser("rtsp"))
    ns = parser.parse_args(
        ["rtsp", "-s", "rtsp://x"] + (["--ai-sidecar"] if flag else [])
    )
    assert ns.ai_sidecar is flag
    assert ns.ai_sidecar_profile == "main"
    assert ns.ai_sidecar_always_on is False
    assert ns.ai_sidecar_path == "/opt/ai-sidecar/index.mjs"


class _Stdout:
    def __init__(self, lines):
        self.lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.lines:
            return self.lines.pop(0)
        await asyncio.sleep(3600)  # the sidecar stays up


class _Proc:
    pid = 42
    returncode = None

    def __init__(self, lines):
        self.stdout = _Stdout(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    async def wait(self):
        return 0


def test_cancelling_the_supervisor_terminates_the_node_process(monkeypatch):
    # A demoted or stopping pod must not leave a stream open on the camera.
    procs = []

    async def fake_exec(*argv, **kw):
        assert argv[0] == "node" and argv[1].endswith("index.mjs")
        assert kw["env"]["PROFILE"] == "main"
        procs.append(_Proc([b"baichuan login OK\n"]))
        return procs[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    c = make_cam(ai_sidecar_path="/opt/ai-sidecar/index.mjs")

    async def scenario():
        task = asyncio.create_task(c._supervise_sidecar())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(procs) == 1 and procs[0].terminated


def test_a_missing_node_binary_never_raises_out_of_the_supervisor(monkeypatch):
    async def fake_exec(*a, **kw):
        raise FileNotFoundError("node")

    async def no_sleep(_):
        raise asyncio.CancelledError  # stop after the first failed start

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    c = make_cam(ai_sidecar_path="/nope")

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await c._supervise_sidecar()

    asyncio.run(scenario())


def _push_cam(**over):
    """A camera wired like test_reolink_push, with the boxes path recorded."""
    c = make_cam(**over)
    c.args.reolink_push_ai = True
    c.args.ai_classes = "person,vehicle,animal"
    c.args.ai_min_movement = 0
    c.args.ai_min_confidence = 0.0
    c.args.ai_confidence_overrides = ""
    c._ai_class_state = {}
    c._ai_last_event_ts = 0.0
    c._sink_last_ts = 0.0
    c._sink_last_boxes_ts = 0.0
    c._ai_active_lead = None
    c._ai_tracks = []
    c._ai_track_seq = 0
    c.args.ai_movement_box_ratio = 0.25
    c.args.ai_movement_reports = 2
    c.smart_detect_enabled = None
    c.smart_detect_zones = None
    c.smart_exclude_zones = None
    c.events = []

    async def start(object_type=None, descriptors=None):
        c.events.append(("start", object_type))
        c._motion_event_ts = 1.0

    async def stop():
        c.events.append(("stop",))
        c._motion_event_ts = None

    async def update(descriptors):
        c.events.append(("update", len(descriptors)))

    async def crop(*a, **k):
        return None

    c.trigger_motion_start, c.trigger_motion_stop = start, stop
    c.trigger_motion_update, c._make_object_crop = update, crop
    return c


BOX = {"type": "person", "confidence": 0.9, "coord": [400, 300, 100, 400]}


def test_on_demand_boxes_never_start_an_event():
    c = _push_cam()
    asyncio.run(c._on_ai_boxes([BOX]))
    assert c.events == []


def test_on_demand_boxes_update_the_running_event_and_empty_reports_do_not_end_it():
    c = _push_cam()
    c._motion_event_ts = 1.0  # started by the push channel
    asyncio.run(c._on_ai_boxes([BOX]))
    asyncio.run(c._on_ai_boxes([]))  # stream warming up / person briefly lost
    assert c.events == [("update", 1)]
    assert c._motion_event_ts == 1.0


def test_on_demand_sidecar_does_not_suppress_the_push_channel():
    c = _push_cam()
    c._sink_last_ts = __import__("time").time()  # sidecar reported a moment ago
    asyncio.run(c._apply_ai_states({"people": True}))
    assert c.events[0][0] == "start"  # an always-on sidecar would have suppressed this


def test_always_on_mode_keeps_the_old_behaviour():
    c = _push_cam(ai_sidecar_always_on=True)
    asyncio.run(c._on_ai_boxes([BOX]))
    assert c.events and c.events[0][0] == "start"  # boxes may start events
    c.events.clear()
    c._motion_event_ts = 1.0
    c._sink_last_boxes_ts = 0.0
    asyncio.run(c._on_ai_boxes([]))
    assert c.events == [("stop",)]  # and end them when the boxes disappear


def _gated_cam(**over):
    c = _push_cam(**over)
    c.args.ai_min_confidence = 0.75  # the event-creation gates, as deployed
    c.args.ai_min_movement = 10
    c.args.ai_movement_box_ratio = 0.25
    c.args.ai_movement_reports = 2
    c.args.ai_confidence_overrides = ""
    c._ai_track_seq = 0
    return c


STILL_CAT = {"type": "animal", "confidence": 0.33, "coord": [772, 572, 22, 46]}


def test_on_demand_a_still_low_confidence_cat_still_gets_its_box():
    # Seen live: a cat reads 30-70% and often sits still; the 75% / must-have-moved gates
    # (meant for starting events) were dropping every box so Protect drew none.
    c = _gated_cam()
    c._motion_event_ts = 1.0
    for _ in range(3):
        asyncio.run(c._on_ai_boxes([dict(STILL_CAT)]))
    assert c.events == [("update", 1)] * 3


def test_always_on_mode_still_applies_the_event_creation_gates():
    c = _gated_cam(ai_sidecar_always_on=True)
    c._motion_event_ts = 1.0
    asyncio.run(c._on_ai_boxes([dict(STILL_CAT)]))
    # 33% and never moved: filtered out, so no box update, and with nothing left the
    # always-on sidecar ends the event (its original behaviour)
    assert c.events == [("stop",)]


def test_on_demand_keeps_one_stable_track_id_per_object():
    c = _gated_cam()
    c._motion_event_ts = 1.0
    ids = []
    for dx in (0, 3, 6):
        box = dict(STILL_CAT, coord=[772 + dx, 572, 22, 46])
        asyncio.run(c._on_ai_boxes([box]))
        ids.append(box["track_id"])
    assert len(set(ids)) == 1


def test_push_start_resets_the_per_event_crop_bookkeeping():
    c = _push_cam()
    c._ai_crops_made = {7, 8, 9}
    asyncio.run(c._apply_ai_states({"people": True}))
    assert c._ai_crops_made == set()


def test_on_demand_a_fast_cat_is_one_object_not_three():
    # Seen live: reports at x=577, 645, 789 (70-150 units apart) became three crops.
    c = _gated_cam()
    c._motion_event_ts = 1.0
    ids = []
    for x in (577, 645, 789):
        box = dict(STILL_CAT, coord=[x, 600, 110, 90])
        asyncio.run(c._on_ai_boxes([box]))
        ids.append(box["track_id"])
    assert len(set(ids)) == 1


def test_on_demand_two_distant_animals_stay_two_objects():
    c = _gated_cam()
    c._motion_event_ts = 1.0
    a = dict(STILL_CAT, coord=[100, 600, 100, 90])
    b = dict(STILL_CAT, coord=[800, 600, 100, 90])
    asyncio.run(c._on_ai_boxes([a, b]))
    assert a["track_id"] != b["track_id"]
