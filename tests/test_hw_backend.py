"""Backend selection, real pipeline construction, and advertised output formats."""

import argparse
import logging
import shlex
import time

import pytest

from unifi.cams.reolink import Reolink
from unifi.cams.rtsp import RTSPCam


def make_cam(*options, camera_type=RTSPCam):
    parser = argparse.ArgumentParser()
    camera_type.add_parser(parser)
    required = (
        ["--source", "rtsp://192.0.2.10/main"]
        if camera_type is RTSPCam
        else ["--username", "test", "--password", "test"]
    )
    cam = camera_type.__new__(camera_type)
    cam.args = parser.parse_args([*required, *options])
    cam.logger = logging.getLogger("test")
    cam._ffmpeg_handles = {}
    cam._stream_meta = {}
    cam._streams = {}
    cam._needs_flv_timestamps = False
    cam._msg_id = 0
    cam._init_time = time.time()
    cam.stream_fps = (20, 10)
    return cam


@pytest.mark.parametrize("backend", ["qsv", "vaapi"])
def test_backend_selection_alone_preserves_passthrough(backend, monkeypatch):
    cam = make_cam("--hw-backend", backend, "--ffmpeg-args=-c:v copy -c:a copy")
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    for stream in ("video1", "video2", "video3"):
        assert cam.get_extra_ffmpeg_args(stream) == "-c:v copy -c:a copy"
        assert "-hwaccel" not in shlex.split(cam.get_base_ffmpeg_args(stream))


def test_legacy_arguments_keep_intel_backend(monkeypatch):
    cam = make_cam("--hw-transcode", "--lo-h265-transcode")
    del cam.args.hw_backend  # pre-existing integrations can construct Namespace
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    assert cam.get_base_ffmpeg_args("video1").startswith(
        "-hwaccel qsv -qsv_device /dev/dri/renderD128 "
    )
    assert cam.get_extra_ffmpeg_args("video1") == (
        '-vf "vpp_qsv=w=2560:h=776" -c:v h264_qsv -b:v 6M'
        " -ar 32000 -ac 1 -codec:a aac -b:a 32k"
    )
    assert cam.get_extra_ffmpeg_args("video2") == (
        "-c:v hevc_qsv -b:v 1M -g 15 -c:a copy"
    )


@pytest.mark.parametrize("stream", ["video1", "video2", "video3"])
async def test_vaapi_pipeline_keeps_hardware_frames_and_correct_framing(
    stream, monkeypatch
):
    cam = make_cam("--hw-backend", "vaapi", "--hw-transcode", "--lo-h265-transcode")
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)

    async def source(_):
        return "rtsp://192.0.2.10/input"

    cam.get_stream_source = source
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    monkeypatch.setattr("unifi.cams.base.subprocess.Popen", Process)
    await cam.start_video_stream(stream, "test", ("127.0.0.1", 7550))
    argv = shlex.split(commands[0])
    input_index = argv.index("-i")
    assert argv[argv.index("-hwaccel") + 1] == "vaapi"
    assert argv[argv.index("-hwaccel_device") + 1] == "/dev/dri/renderD128"
    assert argv[argv.index("-hwaccel_output_format") + 1] == "vaapi"
    assert argv.index("-hwaccel_output_format") < input_index < argv.index("-vf")
    assert argv.index("-threads") < input_index
    assert argv[argv.index("-threads") + 1] == "1"
    assert "qsv" not in commands[0]
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-bf") + 1] == "0"
    if stream == "video1":
        assert argv[argv.index("-c:v") + 1] == "h264_vaapi"
        assert argv[argv.index("-vf") + 1] == "scale_vaapi=w=2560:h=776:format=nv12"
        assert "unifi.hevc_flv" not in argv
    else:
        assert argv[argv.index("-c:v") + 1] == "hevc_vaapi"
        assert argv[argv.index("-vf") + 1] == "scale_vaapi=format=nv12"
        assert "unifi.hevc_flv" in argv


def test_high_transcode_does_not_enable_lower_transcodes(monkeypatch):
    cam = make_cam("--hw-backend", "vaapi", "--hw-transcode")
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    for stream in ("video2", "video3"):
        assert "-hwaccel" not in cam.get_base_ffmpeg_args(stream)
        assert cam.get_extra_ffmpeg_args(stream) == cam.args.ffmpeg_args


def test_render_device_is_one_shell_argument(monkeypatch):
    device = "/dev/dri/render node;not-a-command"
    cam = make_cam("--hw-backend", "vaapi", "--hw-transcode", "--hw-device", device)
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    argv = shlex.split(cam.get_base_ffmpeg_args("video1"))
    assert argv[argv.index("-hwaccel_device") + 1] == device


@pytest.mark.parametrize("backend", ["qsv", "vaapi"])
async def test_advertisement_matches_encoded_output(backend):
    cam = make_cam(
        "--hw-backend",
        backend,
        "--hw-transcode",
        "--hi-codec",
        "h265",
        "--hi-width",
        "5120",
        "--hi-height",
        "1552",
        "--transcode-width",
        "1280",
        "--transcode-height",
        "720",
        "--lo-h265-transcode",
    )
    response = await cam.process_video_settings({"messageId": 1, "payload": None})
    video = response["payload"]["video"]
    assert video["videoCodec"] == video["video1"]["type"] == "h264"
    assert (video["video1"]["width"], video["video1"]["height"]) == (1280, 720)
    assert video["video2"]["type"] == video["video3"]["type"] == "h265"
    assert "h265" in (await cam.get_feature_flags())["videoCodecs"]


@pytest.mark.parametrize(
    "stream,encoder", [("video1", "h264_vaapi"), ("video2", "hevc_vaapi")]
)
def test_reolink_override_honours_hardware_backend(stream, encoder):
    cam = make_cam(
        "--hw-backend",
        "vaapi",
        "--hw-transcode",
        "--lo-h265-transcode",
        camera_type=Reolink,
    )
    argv = shlex.split(cam.get_extra_ffmpeg_args(stream))
    assert argv[argv.index("-c:v") + 1] == encoder
    assert "-vbsf" not in argv


def test_unknown_backend_is_rejected():
    with pytest.raises(SystemExit):
        make_cam("--hw-backend", "unknown")


@pytest.mark.parametrize("driver", [None, "iHD", "radeonsi"])
async def test_driver_override_is_optional_and_local_to_pipeline(driver, monkeypatch):
    options = ["--vaapi-driver", driver] if driver else []
    cam = make_cam(*options)
    environments = []

    class Process:
        def __init__(self, command, **kwargs):
            environments.append(kwargs["env"])

    async def source(_):
        return "rtsp://192.0.2.10/input"

    cam.get_stream_source = source
    monkeypatch.setenv("LIBVA_DRIVER_NAME", "inherited")
    monkeypatch.setattr("unifi.cams.base.subprocess.check_output", lambda _: b"")
    monkeypatch.setattr("unifi.cams.base.subprocess.Popen", Process)
    await cam.start_video_stream("video1", "test", ("127.0.0.1", 7550))
    import os

    assert environments[0]["LIBVA_DRIVER_NAME"] == (driver or "inherited")
    assert os.environ["LIBVA_DRIVER_NAME"] == "inherited"


@pytest.mark.parametrize("camera_type", [RTSPCam, Reolink])
@pytest.mark.parametrize("backend", ["qsv", "vaapi"])
@pytest.mark.parametrize("transcoding", [False, True])
def test_no_audio_all_sources_and_channels(camera_type, backend, transcoding):
    options = ["--no-audio", "--hw-backend", backend]
    if transcoding:
        options += ["--hw-transcode", "--lo-h265-transcode"]
    cam = make_cam(*options, camera_type=camera_type)
    for stream in ("video1", "video2", "video3"):
        assert shlex.split(cam.get_extra_ffmpeg_args(stream))[-1] == "-an"


@pytest.mark.parametrize("backend", ["qsv", "vaapi"])
@pytest.mark.parametrize("scaling", [False, True])
def test_lower_audio_override_and_scaling_preserve_main(backend, scaling):
    options = [
        "--hw-backend",
        backend,
        "--lo-h265-transcode",
        "--lo-width",
        "640",
        "--lo-height",
        "480",
        "--lo-audio-args=-ar 16000 -ac 1 -c:a aac -b:a 32k",
        "--ffmpeg-args=-c:v copy -c:a copy",
    ]
    if scaling:
        options += ["--lo-scale"]
    cam = make_cam(*options)
    for stream in ("video2", "video3"):
        argv = shlex.split(cam.get_extra_ffmpeg_args(stream))
        assert argv[argv.index("-c:a") + 1] == "aac"
        if scaling:
            assert "w=640:h=480" in argv[argv.index("-vf") + 1]
        elif backend == "vaapi":
            assert argv[argv.index("-vf") + 1] == "scale_vaapi=format=nv12"
        else:
            assert "-vf" not in argv
    assert cam.get_extra_ffmpeg_args("video1") == "-c:v copy -c:a copy"


def test_lower_audio_and_scaling_do_not_enable_transcoding():
    cam = make_cam("--lo-scale", "--lo-audio-args=-an")
    for stream in ("video1", "video2", "video3"):
        assert cam.get_extra_ffmpeg_args(stream) == cam.args.ffmpeg_args
