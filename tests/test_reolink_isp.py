"""Protect image settings -> Reolink SetImage/SetIsp, with a fake camera API."""

import argparse
import asyncio
import logging

from unifi.cams.rtsp import RTSPCam
from unifi.reolink_isp import ReolinkIsp, plan_changes, scale

LOG = logging.getLogger("test")


def test_scale_maps_protect_midpoint_to_reolink_midpoint():
    assert scale(0) == 0
    assert scale(50) == 128
    assert scale(100) == 255


def test_image_sliders_scale():
    image, isp = plan_changes({"brightness": 100, "sharpness": 0}, {})
    assert image == {"bright": 255, "sharpen": 0}
    assert isp == {}


def test_wdr_uses_hdr_on_cameras_that_have_it():
    # off -> Off, low -> Auto, middle/high -> On
    assert [plan_changes({"wdr": w}, {"hdr": 0})[1]["hdr"] for w in range(4)] == [
        0,
        2,
        1,
        1,
    ]


def test_wdr_uses_drc_otherwise():
    assert plan_changes({"wdr": 0}, {"backLight": "Off"})[1] == {"backLight": "Off"}
    assert plan_changes({"wdr": 3}, {"backLight": "Off"})[1] == {
        "backLight": "DynamicRangeControl",
        "drc": 255,
    }


def test_denoise_mirror_flip():
    _, isp = plan_changes({"denoise": 0, "mirror": 1, "flip": 0}, {})
    assert isp == {"nr3d": 0, "mirroring": 1, "rotation": 0}


class FakeCam(ReolinkIsp):
    def __init__(self, isp, fail=False):
        super().__init__("cam", "u", "p", LOG)
        self.isp = isp
        self.fail = fail
        self.calls = []

    async def _post(self, query, body):
        self.calls.append(body)
        out = []
        for c in body:
            if c["cmd"] == "Login":
                out.append({"code": 0, "value": {"Token": {"name": "t"}}})
            elif c["cmd"] == "GetIsp":
                out.append({"code": 0, "value": {"Isp": dict(self.isp)}})
            else:
                out.append({"code": 1 if self.fail else 0})
        return out


def sent(cam, cmd):
    return [c for call in cam.calls for c in call if c["cmd"] == cmd]


def test_apply_writes_image_and_merged_isp():
    cam = FakeCam({"channel": 0, "hdr": 0, "blc": 128})
    asyncio.run(cam.apply({"brightness": 100, "wdr": 2, "aeMode": "auto"}))
    assert sent(cam, "SetImage")[0]["param"]["Image"] == {"channel": 0, "bright": 255}
    assert sent(cam, "SetIsp")[0]["param"]["Isp"] == {
        "channel": 0,
        "hdr": 1,
        "blc": 128,
    }


def test_apply_skips_unchanged_and_irrelevant():
    cam = FakeCam({"channel": 0})
    asyncio.run(cam.apply({"brightness": 50}))
    n = len(cam.calls)
    asyncio.run(cam.apply({"brightness": 50}))
    asyncio.run(cam.apply({"aeMode": "auto"}))
    assert len(cam.calls) == n


def test_failed_write_is_retried_next_time():
    cam = FakeCam({"channel": 0}, fail=True)
    asyncio.run(cam.apply({"contrast": 60}))
    cam.fail = False
    asyncio.run(cam.apply({"contrast": 60}))
    assert len(sent(cam, "SetImage")) == 2


def test_rtspcam_hooks_are_off_without_flag():
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(reolink_isp=False, reolink_ai_host="cam")
    asyncio.run(c.change_video_settings({"brightness": 10}))
    assert not hasattr(c, "_isp")


# Reboot wiring. Everything here uses fakes: nothing reaches a camera (conftest.py
# also fails any test that tries to open a connection beyond this machine).


def make_reboot_cam(reboot_flag=True, host="cam"):
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(
        reolink_reboot=reboot_flag,
        reolink_ai_host=host,
        reolink_ai_user="u",
        reolink_ai_password="p",
    )
    c.logger = LOG
    c.rebooted = []

    class Fake:
        async def reboot(self_inner):
            c.rebooted.append(1)
            return True

    c._isp = Fake()
    c._isp_state = {}
    return c


async def _reboot_and_settle(c, times=1):
    for _ in range(times):
        await c.reboot_camera()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_reboot_is_off_without_flag():
    c = make_reboot_cam(reboot_flag=False)
    asyncio.run(_reboot_and_settle(c))
    assert c.rebooted == []


def test_reboot_needs_a_camera_host():
    c = make_reboot_cam(host=None)
    asyncio.run(_reboot_and_settle(c))
    assert c.rebooted == []


def test_reboot_sent_once_per_cooldown():
    c = make_reboot_cam()
    asyncio.run(_reboot_and_settle(c, times=3))
    assert c.rebooted == [1]


def test_reboot_allowed_again_after_cooldown():
    c = make_reboot_cam()
    asyncio.run(_reboot_and_settle(c))
    c._last_reboot -= RTSPCam.REBOOT_COOLDOWN_SEC + 1
    asyncio.run(_reboot_and_settle(c))
    assert c.rebooted == [1, 1]


def test_client_reboot_sends_only_the_reboot_command():
    cam = FakeCam({"channel": 0})
    assert asyncio.run(cam.reboot()) is True
    assert [c["cmd"] for call in cam.calls for c in call] == ["Login", "Reboot"]


def test_client_reboot_reports_a_refusal():
    cam = FakeCam({"channel": 0}, fail=True)
    assert asyncio.run(cam.reboot()) is False


def test_protect_reboot_message_triggers_hook_and_reconnect():
    import json

    c = make_reboot_cam()
    calls = []

    async def hook():
        calls.append(1)

    c.reboot_camera = hook
    msg = json.dumps({"functionName": "Reboot", "messageId": 1})
    assert asyncio.run(c.process(msg.encode())) is True
    assert calls == [1]


def test_network_guard_blocks_real_addresses():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            s.connect(("192.0.2.159", 443))
        except AssertionError as exc:
            assert "192.0.2.159" in str(exc)
        else:
            raise AssertionError("guard did not block a non-local connect")
    finally:
        s.close()


def test_ptz_flag_sets_the_feature_keys():
    from unifi.cams.base import UnifiCamBase

    for ptz in (False, True):
        c = RTSPCam.__new__(RTSPCam)
        c.args = argparse.Namespace(ptz=ptz, hi_codec="h264")
        flags = asyncio.run(UnifiCamBase.get_feature_flags(c))
        assert flags["isPtz"] is ptz
        assert ("ptz" in flags) is ptz
