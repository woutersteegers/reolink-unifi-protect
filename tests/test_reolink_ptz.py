"""Protect's PTZ joystick/zoom -> Reolink PtzCtrl, with a fake camera API only.

Sign conventions (x>0=right, y>0=up, z>0=zoom in), the "zoom and pan/tilt never arrive
together" shape, that diagonal pan/tilt ops are accepted but never move this camera, and that
its zoom pair is the opposite of the official doc's naming, were all read from a real Reolink
E1 Zoom during manual testing (session 2026-10-12), not guessed; see reolink_ptz.py's module
docstring.
"""

import argparse
import asyncio
import logging

import aiohttp

from unifi.cams.rtsp import RTSPCam
from unifi.reolink_light import ReolinkLight
from unifi.reolink_ptz import ReolinkPtz, plan_move

LOG = logging.getLogger("test")


def test_deadzone_is_a_stop():
    assert plan_move(0, 0, 0) is None
    assert plan_move(5, -5, 10) is None


def test_cardinal_directions():
    assert plan_move(1000, 0, 0)[0] == "Right"
    assert plan_move(-1000, 0, 0)[0] == "Left"
    assert plan_move(0, 1000, 0)[0] == "Up"
    assert plan_move(0, -1000, 0)[0] == "Down"


def test_diagonal_drags_pick_the_dominant_axis_instead_of_a_diagonal_op():
    # Diagonal ops (LeftUp, RightDown, ...) are in the official doc and this camera's
    # GetAbility, and it answers code 0 for them, but never actually moves: confirmed by
    # sending one directly and watching the camera. So a diagonal-leaning drag has to
    # resolve to whichever cardinal axis is strongest, never a diagonal op.
    assert plan_move(1000, 999, 0)[0] == "Right"
    assert plan_move(999, 1000, 0)[0] == "Up"
    assert plan_move(-1000, -999, 0)[0] == "Left"
    assert plan_move(-999, -1000, 0)[0] == "Down"


def test_small_jitter_on_one_axis_stays_cardinal():
    assert plan_move(1000, 5, 0)[0] == "Right"
    assert plan_move(5, -1000, 0)[0] == "Down"


def test_pan_tilt_takes_priority_over_zoom():
    # Protect's own messages never mix them, but the mapping shouldn't assume that.
    op, _ = plan_move(1000, 0, 1000)
    assert op == "Right"


def test_zoom_direction_is_the_opposite_of_the_official_docs_naming():
    # The official doc says ZoomDec is zoom in and ZoomInc is zoom out, but on this camera
    # that's backwards: confirmed by commanding each op directly and watching it zoom the
    # wrong way. z>0 (what the UI treats as zoom in) has to map to ZoomInc here.
    assert plan_move(0, 0, 1000)[0] == "ZoomInc"
    assert plan_move(0, 0, -1000)[0] == "ZoomDec"


def test_speed_scales_from_the_strongest_axis():
    from unifi.reolink_ptz import MAX_SPEED

    assert plan_move(1000, 0, 0)[1] == MAX_SPEED
    assert plan_move(500, 0, 0)[1] == round(MAX_SPEED / 2)
    assert plan_move(20, 0, 0)[1] == 1  # at the deadzone edge, never 0


class FakeCam(ReolinkPtz):
    def __init__(self, fail=False):
        super().__init__("cam", "u", "p", LOG)
        self.fail = fail
        self.calls = []

    async def _post(self, query, body):
        self.calls.append(body)
        out = []
        for c in body:
            if c["cmd"] == "Login":
                out.append({"code": 0, "value": {"Token": {"name": "t"}}})
            else:
                out.append({"code": 1 if self.fail else 0})
        return out


def sent_ops(cam):
    return [
        c["param"]["op"] for call in cam.calls for c in call if c["cmd"] == "PtzCtrl"
    ]


def sent_speeds(cam):
    return [
        c["param"].get("speed")
        for call in cam.calls
        for c in call
        if c["cmd"] == "PtzCtrl"
    ]


def test_continuous_move_sends_the_op():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    assert sent_ops(cam) == ["Right"]


def test_identical_op_and_speed_is_not_resent():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    # x=995 rounds to the same speed as x=1000 (18): a genuine no-op tick, not just
    # the same direction, so it must not cause a second send.
    asyncio.run(
        cam.continuous_move(995, 5, 0)
    )  # still "Right" at the same speed: y=5 is inside the deadzone
    assert sent_ops(cam) == ["Right"]


def test_speed_change_mid_hold_is_resent():
    # Regression for the bug where a held direction kept whatever speed the first
    # tick sent: Reolink has no speed-only update, so a weaker/stronger push in the
    # same direction must still issue a new PtzCtrl, not be deduped away.
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    asyncio.run(cam.continuous_move(500, 0, 0))
    assert sent_ops(cam) == ["Right", "Right"]
    assert sent_speeds(cam)[0] != sent_speeds(cam)[1]


def test_release_sends_stop_once():
    cam = FakeCam()
    asyncio.run(cam.continuous_move(1000, 0, 0))
    asyncio.run(cam.continuous_move(0, 0, 0))
    asyncio.run(cam.continuous_move(0, 0, 0))
    assert sent_ops(cam) == ["Right", "Stop"]


def test_failed_op_is_retried_next_tick():
    cam = FakeCam(fail=True)
    asyncio.run(cam.continuous_move(1000, 0, 0))
    cam.fail = False
    asyncio.run(cam.continuous_move(1000, 0, 0))
    assert sent_ops(cam) == ["Right", "Right"]


def make_ptz_cam(ptz_flag=True, host="cam"):
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(
        ptz=ptz_flag, reolink_ai_host=host, reolink_ai_user="u", reolink_ai_password="p"
    )
    c.logger = LOG
    return c


def test_rtspcam_hook_off_without_ptz_flag():
    c = make_ptz_cam(ptz_flag=False)
    asyncio.run(c.continuous_move(1000, 0, 0))
    assert not hasattr(c, "_ptz_task")


def test_rtspcam_hook_off_without_camera_host():
    c = make_ptz_cam(host=None)
    asyncio.run(c.continuous_move(1000, 0, 0))
    assert not hasattr(c, "_ptz_task")


def test_rtspcam_hook_never_cancels_an_in_flight_request_and_drains_the_next_tick():
    # The first cut of this cancelled the in-flight request on every new tick; since
    # Protect sends ticks faster than an HTTP round trip, no request ever survived long
    # enough to reach the camera. The worker must let each send finish, then
    # automatically pick up whatever tick is now latest, with no further message needed.
    c = make_ptz_cam()
    sent = []
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowPtz:
        async def continuous_move(self_inner, x, y, z):
            started.set()
            await finish.wait()
            sent.append((x, y, z))

    c._ptz = SlowPtz()

    async def scenario():
        await c.continuous_move(1000, 0, 0)
        await started.wait()  # the worker has genuinely picked this up and is blocked
        first = c._ptz_task
        await c.continuous_move(0, 1000, 0)  # must not cancel the in-flight send
        finish.set()
        await first
        return first

    first = asyncio.run(scenario())
    assert not first.cancelled()
    assert sent == [(1000, 0, 0), (0, 1000, 0)]


def test_rtspcam_hook_coalesces_ticks_that_arrive_while_sending():
    # The send already in flight (100,0,0) isn't cancelled and still completes, but a
    # middle tick (200,0,0) that lands before the worker reads the next one is coalesced
    # away by (300,0,0) rather than being queued and sent too.
    c = make_ptz_cam()
    sent = []
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowPtz:
        async def continuous_move(self_inner, x, y, z):
            started.set()
            await finish.wait()
            sent.append((x, y, z))

    c._ptz = SlowPtz()

    async def scenario():
        await c.continuous_move(100, 0, 0)
        await started.wait()  # the worker is now sending (100,0,0)
        await c.continuous_move(200, 0, 0)  # superseded before the worker reads again
        await c.continuous_move(300, 0, 0)
        finish.set()
        await c._ptz_task

    asyncio.run(scenario())
    assert sent == [(100, 0, 0), (300, 0, 0)]


def test_protect_continuous_move_message_reaches_the_hook_through_process():
    # Real regression: ContinuousMove always has responseExpected=False, and process()'s
    # early-return guard (for messages needing no reply) didn't list it as one that still
    # needs ACTING on, so every joystick tick was silently dropped before reaching any
    # handler. This exercises the real dispatcher, not just the hook directly.
    import json

    c = make_ptz_cam()
    calls = []

    async def hook(x, y, z):
        calls.append((x, y, z))

    c.continuous_move = hook
    msg = json.dumps(
        {
            "functionName": "ContinuousMove",
            "messageId": 1,
            "responseExpected": False,
            "payload": {"x": 500, "y": -250, "z": 0},
        }
    )
    asyncio.run(c.process(msg.encode()))
    assert calls == [(500, -250, 0)]


class _Resp:
    def __init__(self, fail):
        self.fail = fail

    async def __aenter__(self):
        if self.fail:
            raise aiohttp.ServerDisconnectedError()
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return [{"code": 0}]


def _fake_sessions(monkeypatch, fail_first_post=False):
    made = []

    class Session:
        def __init__(self):
            self.closed = False
            self.posts = 0
            made.append(self)

        def post(self, *a, **kw):
            self.posts += 1
            return _Resp(fail_first_post and len(made) == 1)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return made


def test_commands_share_one_kept_alive_connection(monkeypatch):
    # A fresh TLS handshake per PtzCtrl took ~420 ms vs ~100 ms reused, so Stop landed
    # late and quick jogs overshot.
    made = _fake_sessions(monkeypatch)
    ptz = ReolinkPtz("cam", "u", "p", LOG)

    async def go():
        for _ in range(3):
            await ptz._post("token=t", [])
        await ptz.close()

    asyncio.run(go())
    assert len(made) == 1 and made[0].posts == 3 and made[0].closed


def test_dropped_connection_reconnects_once(monkeypatch):
    made = _fake_sessions(monkeypatch, fail_first_post=True)
    ptz = ReolinkPtz("cam", "u", "p", LOG)
    res = asyncio.run(ptz._post("token=t", []))
    assert res == [{"code": 0}]
    assert len(made) == 2 and made[0].closed


def _home_cam():
    c = make_ptz_cam()
    c.args.ptz_home_preset = 0
    c.sent = []
    c.gotos = []

    async def send(msg):
        c.sent.append(msg)

    class Client:
        async def goto_preset(self_inner, preset_id, speed=32):
            c.gotos.append(preset_id)
            return True

    c.send = send
    c._ptz = Client()
    c.gen_msg_id = lambda: 1
    return c


def _msg(fn, payload, expected=False, mid=7):
    import json

    return json.dumps(
        {
            "functionName": fn,
            "messageId": mid,
            "responseExpected": expected,
            "payload": payload,
        }
    ).encode()


def test_get_current_position_reply_has_steps_for_protect_to_build_its_home_preset():
    # Protect crashed ("reading 'pan'") because we echoed the request, which has no steps.
    c = _home_cam()
    asyncio.run(
        c.process(_msg("GetCurrentPosition", {"inDegree": True, "inSteps": True}, True))
    )
    reply = c.sent[0]["payload"]
    assert set(reply["steps"]) >= {"pan", "tilt", "zoom", "focus"}
    assert set(reply["degrees"]) >= {"pan", "tilt", "zoom"}


def test_home_view_goto_drives_the_cameras_default_preset():
    c = _home_cam()
    c.args.ptz_home_preset = 3
    # responseExpected False must still reach the handler (same trap as ContinuousMove).
    asyncio.run(c.process(_msg("Preset", {"action": "go", "index": -1})))
    assert c.gotos == [3]


def test_home_view_goto_is_found_inside_an_item_too():
    c = _home_cam()
    asyncio.run(c.process(_msg("Preset", {"action": "go", "item": {"index": -1}})))
    assert c.gotos == [0]


def test_setting_the_home_preset_does_not_move_the_camera():
    c = _home_cam()
    payload = {"action": "set", "item": {"index": -1, "name": "Home view", "pan": 0}}
    asyncio.run(c.process(_msg("Preset", payload, expected=True)))
    assert c.gotos == []
    assert c.sent  # still acknowledged so Protect does not stall


def test_other_protect_presets_are_not_mapped():
    c = _home_cam()
    asyncio.run(c.process(_msg("Preset", {"action": "go", "index": 2})))
    assert c.gotos == []


def test_goto_preset_sends_topos_and_resets_dedup():
    cam = FakeCam()
    cam._last_sent = ("Right", 18)
    assert asyncio.run(cam.goto_preset(0))
    call = [c for call in cam.calls for c in call if c["cmd"] == "PtzCtrl"][0]
    assert call["param"]["op"] == "ToPos" and call["param"]["id"] == 0
    assert cam._last_sent == (None, None)


# --- spotlight (Protect's TurnOnColorNightVision) ---


class FakeLight:
    def __init__(self):
        self.calls = []

    async def set_spotlight(self, on, brightness=None):
        self.calls.append((on, brightness))
        return True

    async def close(self):
        pass


def _light_cam(seconds_default=15, brightness=None):
    c = _home_cam()
    c.args.spotlight = True
    c.args.spotlight_brightness = brightness
    c.args.spotlight_default_seconds = seconds_default
    return c


def test_spotlight_flag_advertises_has_flash():
    c = _light_cam()

    async def go():
        return await c.get_feature_flags()

    flags = asyncio.run(go())
    assert flags["hasFlash"] is True and flags["flash"] is True
    assert "flash" not in asyncio.run(_home_cam().get_feature_flags())


def test_turn_on_color_night_vision_reaches_the_handler_without_a_response_expected():
    c = _light_cam()
    seen = []

    async def spot(d):
        seen.append(d)

    c.spotlight = spot
    asyncio.run(
        c.process(_msg("TurnOnColorNightVision", {"spotlightDuration": 7}, False))
    )
    assert seen == [7]


def test_spotlight_turns_on_then_off_after_the_duration(monkeypatch):
    c = _light_cam(brightness=60)
    light = FakeLight()
    c._light = light
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def go():
        await c.spotlight(5)
        await c._light_task

    asyncio.run(go())
    assert light.calls == [(True, 60), (False, None)]
    assert slept == [5]


def test_spotlight_zero_duration_uses_the_default(monkeypatch):
    c = _light_cam(seconds_default=22)
    c._light = FakeLight()
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def go():
        await c.spotlight(0)
        await c._light_task

    asyncio.run(go())
    assert slept == [22]


def test_new_spotlight_request_replaces_the_old_and_ends_on():
    c = _light_cam()
    light = FakeLight()
    c._light = light

    async def go():
        await c.spotlight(300)
        await asyncio.sleep(0)  # let the first one switch on
        await c.spotlight(300)
        await asyncio.sleep(0)
        c._light_task.cancel()
        await asyncio.gather(c._light_task, return_exceptions=True)

    asyncio.run(go())
    # on, (old one cancelled -> off), on, (cancelled -> off): never on at the end
    assert [on for on, _ in light.calls] == [True, False, True, False]


def test_spotlight_is_off_without_the_flag():
    c = _home_cam()
    asyncio.run(c.spotlight(5))
    assert not hasattr(c, "_light_task")


class FakeLightApi(ReolinkLight):
    def __init__(self):
        super().__init__("cam", "u", "p", LOG)
        self.calls = []

    async def _post(self, query, body):
        self.calls.append(body)
        return [
            (
                {"code": 0, "value": {"Token": {"name": "t"}}}
                if c["cmd"] == "Login"
                else {"code": 0}
            )
            for c in body
        ]


def test_set_spotlight_writes_only_state_and_brightness():
    cam = FakeLightApi()
    assert asyncio.run(cam.set_spotlight(True, 40))
    led = [c for call in cam.calls for c in call if c["cmd"] == "SetWhiteLed"][0]
    assert led["param"]["WhiteLed"] == {"channel": 0, "state": 1, "bright": 40}
    cam.calls.clear()
    assert asyncio.run(cam.set_spotlight(False, 40))
    led = [c for call in cam.calls for c in call if c["cmd"] == "SetWhiteLed"][0]
    assert led["param"]["WhiteLed"] == {"channel": 0, "state": 0}


def test_spotlight_slider_value_is_the_default_duration(monkeypatch):
    c = _light_cam(seconds_default=15)
    c._light = FakeLight()
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def go():
        await c.change_video_settings({"spotlightDuration": 45, "brightness": 50})
        await c.spotlight(0)  # an alarm without its own duration
        await c._light_task
        await c.spotlight(7)  # an alarm with one wins
        await c._light_task

    asyncio.run(go())
    assert slept == [45, 7]


def test_spotlight_slider_is_ignored_without_the_flag():
    c = _home_cam()
    asyncio.run(c.change_video_settings({"spotlightDuration": 45}))
    assert not hasattr(c, "_spotlight_slider_s")


def test_color_night_vision_toggle_sets_the_cameras_auto_mode():
    c = _light_cam()
    c.args.reolink_ai_host = "cam"
    modes = []

    class Light(FakeLight):
        async def set_auto_mode(self_inner, enabled):
            modes.append(enabled)
            return True

    c._light = Light()
    asyncio.run(c.change_video_settings({"colorNightVision": 1, "brightness": 50}))
    asyncio.run(c.change_video_settings({"colorNightVision": 0}))
    asyncio.run(c.change_video_settings({"brightness": 60}))  # untouched: no write
    assert modes == [True, False]


def test_color_night_vision_is_ignored_without_the_spotlight_flag():
    c = _home_cam()
    c.args.reolink_ai_host = "cam"
    asyncio.run(c.change_video_settings({"colorNightVision": 0}))
    assert not hasattr(c, "_light")


def test_set_auto_mode_writes_only_the_mode():
    cam = FakeLightApi()
    assert asyncio.run(cam.set_auto_mode(True))
    assert asyncio.run(cam.set_auto_mode(False))
    leds = [
        c["param"]["WhiteLed"]
        for call in cam.calls
        for c in call
        if c["cmd"] == "SetWhiteLed"
    ]
    assert leds == [
        {"channel": 0, "mode": 1},
        {"channel": 0, "mode": 0},
    ]
