"""Reolink Baichuan push: AI detections and the doorbell button without polling.

The camera's push channel replaces the HTTPS ``GetAiState`` / ``GetEvents``
pollers (which can wedge a small camera's web server). These tests pin the
detection state machine fed by push events and the never-give-up reconnect
loop, using a fake ``reolink_aio`` client so no camera is needed.
"""

import argparse
import asyncio
import logging
import sys
import time
import types

import pytest

from unifi.cams.base import SmartDetectObjectType
from unifi.cams.rtsp import RTSPCam


@pytest.fixture
def cam():
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(
        reolink_ai_host="cam",
        reolink_ai_user="u",
        reolink_ai_password="p",
        reolink_push_button=True,
        reolink_push_ai=True,
    )
    c.logger = logging.getLogger("test")
    c._ai_class_state = {}
    c._ai_active = None
    c._ai_last_event_ts = 0.0
    c._sink_last_ts = 0.0
    c.smart_detect_enabled = None
    c.events = []
    c.crops = []
    c._motion_event_id = 0

    async def crop(tracker_id, kind, coord):
        c.crops.append((tracker_id, kind, coord))

    c._make_object_crop = crop

    async def start(object_type=None, descriptors=None):
        c.events.append(("start", object_type))

    async def stop():
        c.events.append(("stop",))

    c.trigger_motion_start = start
    c.trigger_motion_stop = stop
    return c


def states(**on):
    base = {"people": False, "vehicle": False, "dog_cat": False}
    base.update(on)
    return base


async def test_detection_starts_once_and_stops_on_clear(cam):
    await cam._apply_ai_states(states(people=True))
    await cam._apply_ai_states(states(people=True))  # repeated event: no restart
    await cam._apply_ai_states(states())
    assert cam.events == [("start", SmartDetectObjectType.PERSON), ("stop",)]


async def test_class_priority_and_filtering(cam):
    await cam._apply_ai_states(states(dog_cat=True, vehicle=True))
    assert cam.events == [("start", SmartDetectObjectType.VEHICLE)]
    cam._ai_active = None
    cam.events.clear()
    cam.smart_detect_enabled = ["animal"]
    await cam._apply_ai_states(states(people=True, vehicle=True, dog_cat=True))
    assert cam.events == [("start", SmartDetectObjectType.ANIMAL)]


async def test_lost_connection_clears_an_active_detection(cam):
    await cam._apply_ai_states(states(people=True))
    await cam._apply_ai_states({}, force_clear=True)
    assert cam.events[-1] == ("stop",)
    assert cam._ai_active is None


async def test_silent_detection_expires(cam):
    await cam._apply_ai_states(states(vehicle=True))
    await cam._expire_stale_ai()  # recent: stays active
    assert cam._ai_active is not None
    cam._ai_last_event_ts = time.time() - 100
    await cam._expire_stale_ai()
    assert cam._ai_active is None and cam.events[-1] == ("stop",)


async def test_box_sidecar_takes_over_while_alive(cam):
    cam._sink_last_ts = time.time()
    await cam._apply_ai_states(states(people=True))
    assert cam.events == []


def _fake_reolink(monkeypatch, fail_logins):
    log = {"logins": [], "events": 0}

    class Baichuan:
        session_active = True

        async def login(self):
            log["logins"].append(time.time())
            if len(log["logins"]) <= fail_logins:
                raise RuntimeError("login refused")

        def register_callback(self, *args, **kwargs):
            log["callback"] = args[1]

        async def subscribe_events(self):
            pass

        async def logout(self):
            pass

    class Host:
        def __init__(self, *args, **kwargs):
            self.baichuan = Baichuan()
            self._ai_detection_states = {}
            self._visitor = False

        def visitor_detected(self, channel):
            return self._visitor

        def ai_detected(self, channel, key):
            return False

    pkg = types.ModuleType("reolink_aio")
    api = types.ModuleType("reolink_aio.api")
    api.Host = Host
    monkeypatch.setitem(sys.modules, "reolink_aio", pkg)
    monkeypatch.setitem(sys.modules, "reolink_aio.api", api)
    return log


async def test_push_retries_forever_with_capped_backoff(cam, monkeypatch):
    log = _fake_reolink(monkeypatch, fail_logins=6)
    cam._push_min_delay = 0.02
    cam._push_max_delay = 0.08
    cam._push_stable_after = 60
    cam._push_watch_interval = 0.01
    task = asyncio.create_task(cam._push_reolink())
    await asyncio.sleep(0.8)
    assert not task.done(), "the push loop must never stop"
    assert len(log["logins"]) == 7  # six refusals, then it connected
    gaps = [b - a for a, b in zip(log["logins"], log["logins"][1:])]
    assert gaps[1] > gaps[0] * 1.5  # grows ...
    assert max(gaps) < 0.3  # ... but is capped (0.08 s plus scheduling slack)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_push_button_press_rings_once_per_press(cam, monkeypatch):
    log = _fake_reolink(monkeypatch, fail_logins=0)
    rings = []

    async def trigger_ring():
        rings.append(1)

    cam.trigger_ring = trigger_ring
    cam._push_watch_interval = 0.01
    task = asyncio.create_task(cam._push_reolink())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "callback" in log:
            break
    callback = log["callback"]
    host = callback.__defaults__[0]
    host._visitor = True
    callback()
    callback()  # still pressed: not a second ring
    host._visitor = False
    callback()
    host._visitor = True
    callback()
    await asyncio.sleep(0.05)
    assert len(rings) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_ai_states_are_preseeded_for_the_push_callback(cam, monkeypatch):
    log = _fake_reolink(monkeypatch, fail_logins=0)

    async def run():
        task = asyncio.create_task(cam._push_reolink())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "callback" in log:
                break
        host = log["callback"].__defaults__[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return host

    host = asyncio.run(run())
    assert host._ai_detection_states[0] == states()


async def test_push_detection_requests_a_per_object_crop(cam):
    # Without a crop ds stores no detectedThumbnails and Protect's Person/Animal/
    # Vehicle filters never list the event.
    await cam._apply_ai_states(states(people=True))
    await cam._apply_ai_states(states(people=True))  # repeated: no second crop
    assert cam.crops == [(100, "person", cam.SYNTHETIC_BOX)]


async def test_clear_during_a_slow_start_still_stops_the_event(cam):
    # Real risk on Wi-Fi cameras: the start awaits a snapshot and a crop that take
    # seconds; a clear pushed meanwhile used to be dropped, holding the event open
    # until the 90 s expiry.
    gate = asyncio.Event()

    async def slow_start(object_type=None, descriptors=None):
        cam.events.append(("start", object_type))
        await gate.wait()

    cam.trigger_motion_start = slow_start
    starting = asyncio.ensure_future(cam._apply_ai_states(states(people=True)))
    await asyncio.sleep(0)
    clearing = asyncio.ensure_future(cam._apply_ai_states(states()))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(starting, clearing)
    assert cam.events == [("start", SmartDetectObjectType.PERSON), ("stop",)]
    assert cam._ai_active is None
