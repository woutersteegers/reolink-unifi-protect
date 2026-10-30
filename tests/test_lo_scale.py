"""Backend-aware lower-channel scaling and opt-in audio controls.

A shared relay supplies one upstream connection; scaling alone does not.
"""

import argparse

from unifi.cams.rtsp import RTSPCam


def make_cam(**args) -> RTSPCam:
    c = RTSPCam.__new__(RTSPCam)
    c.args = argparse.Namespace(
        hw_transcode=False,
        lo_h265_transcode=True,
        lo_transcode_bitrate="1M",
        lo_width=1920,
        lo_height=576,
        lo_fps=20,
        ffmpeg_args="-c:v copy -c:a copy",
        hi_codec="h265",
    )
    for k, v in args.items():
        setattr(c.args, k, v)
    return c


def test_lo_scale_adds_the_qsv_scaler_to_the_lo_channels():
    cam = make_cam(lo_scale=True)
    for stream in ("video2", "video3"):
        args = cam.get_extra_ffmpeg_args(stream)
        assert args.startswith('-vf "vpp_qsv=w=1920:h=576" ')
        assert "-c:v hevc_qsv -b:v 1M -g 20" in args


def test_without_lo_scale_the_transcode_keeps_its_size():
    args = make_cam().get_extra_ffmpeg_args("video3")
    assert "vpp_qsv" not in args
    assert args.startswith("-c:v hevc_qsv")


def test_the_main_channel_is_never_scaled():
    assert make_cam(lo_scale=True).get_extra_ffmpeg_args("video1") == (
        "-c:v copy -c:a copy"
    )


def test_no_audio_drops_audio_on_every_channel():
    cam = make_cam(lo_scale=True, no_audio=True)
    for stream in ("video1", "video2", "video3"):
        assert cam.get_extra_ffmpeg_args(stream).endswith(" -an")


def test_audio_is_kept_by_default():
    cam = make_cam(lo_scale=True)
    assert "-an" not in cam.get_extra_ffmpeg_args("video1").split()
    assert "-c:a copy" in cam.get_extra_ffmpeg_args("video3")


def test_lo_audio_args_replace_the_audio_copy():
    cam = make_cam(lo_scale=True, lo_audio_args="-ar 16000 -ac 1 -codec:a aac -b:a 32k")
    args = cam.get_extra_ffmpeg_args("video3")
    assert args.endswith("-g 20 -ar 16000 -ac 1 -codec:a aac -b:a 32k")
    assert "-c:a copy" not in args
