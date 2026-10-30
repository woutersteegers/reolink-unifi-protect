"""Camera passwords with shell or URL metacharacters must reach the camera intact.

The ffmpeg pipelines run through a shell, and the Reolink API takes the password as a
query parameter: a password containing $, `, ", &, # or % used to break streams or
logins (and in the shell case, run part of it as a command).
"""

import argparse
import logging
import shlex
import urllib.parse

import pytest

from unifi.cams.rtsp import RTSPCam

PASSWORD = "p@ss$(id)`x`\"'&#+%20"
# Written raw, as people put them in compose files; ffmpeg accepts these characters.
SOURCE = "rtsp://admin:pa$HOME$(id)`id`\"'x@192.0.2.10:554/main"
PASSWORDS = [
    "plain-secret",
    "pa'ssword",
    'pa"ssword',
    "pa ssword",
    "pa@ssword",
    "pa/ssword",
    "pa|ssword",
    "pa$HOME$(id)`id`",
    PASSWORD,
]


def make_cam() -> RTSPCam:
    c = RTSPCam.__new__(RTSPCam)
    c.logger = logging.getLogger("test")
    c.args = argparse.Namespace(
        reolink_ai_host="192.0.2.10",
        reolink_ai_user="admin",
        reolink_ai_password=PASSWORD,
        rtsp_transport="tcp",
        ffmpeg_args="-c:v copy",
        source=[SOURCE],
        hi_codec="h264",
    )
    c._ffmpeg_handles = {}
    c._stream_meta = {}
    c._needs_flv_timestamps = False
    return c


def test_api_url_carries_the_password_verbatim():
    url = make_cam()._reolink_api_url("GetAiState")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["password"] == [PASSWORD]
    assert query["user"] == ["admin"]
    assert query["cmd"] == ["GetAiState"]


@pytest.mark.parametrize("password", PASSWORDS)
async def test_stream_pipeline_quotes_source_without_logging_it(
    monkeypatch, caplog, password
):
    cam = make_cam()
    source_url = f"rtsp://admin:{password}@192.0.2.10:554/main"
    spawned = []

    class FakePopen:
        pid = 0

        def __init__(self, cmd, **kwargs):
            spawned.append(cmd)

        def poll(self):
            return None

    monkeypatch.setattr("unifi.cams.base.subprocess.Popen", FakePopen)
    cam.get_base_ffmpeg_args = lambda stream_index="": ""

    async def source(stream_index):
        return source_url

    cam.get_stream_source = source
    with caplog.at_level(logging.INFO):
        await cam.start_video_stream("video1", "s1", ("127.0.0.1", 7550))
    argv = shlex.split(spawned[0])
    assert argv[argv.index("-i") + 1] == source_url
    assert "Spawning ffmpeg for video1 (s1)" in caplog.text
    assert password not in caplog.text
    assert shlex.quote(source_url) not in caplog.text
    assert "ssword" not in caplog.text


@pytest.mark.parametrize("password", PASSWORDS)
def test_snapshot_stream_quotes_source_without_logging_it(
    monkeypatch, tmp_path, caplog, password
):
    cam = make_cam()
    source_url = f"rtsp://admin:{password}@192.0.2.10:554/main"
    cam.args.source = [source_url]
    cam.snapshot_stream = None
    cam.snapshot_dir = str(tmp_path)
    spawned = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned.append(cmd)

    monkeypatch.setattr("unifi.cams.rtsp.subprocess.Popen", FakePopen)
    with caplog.at_level(logging.INFO):
        cam.start_snapshot_stream()
    argv = shlex.split(spawned[0])
    assert argv[argv.index("-i") + 1] == source_url
    assert "Spawning stream for snapshots" in caplog.text
    assert password not in caplog.text
    assert shlex.quote(source_url) not in caplog.text
    assert "ssword" not in caplog.text
