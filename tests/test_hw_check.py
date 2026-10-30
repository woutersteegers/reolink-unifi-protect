"""The startup iGPU self-test must catch a wrong, unpinned or broken driver.

Fake vainfo/ffmpeg executables on PATH stand in for the GPU, so the real
subprocess handling is exercised without hardware.
"""

import argparse
import logging
import os
import stat

import pytest

from unifi import hw_check
from unifi.cams.rtsp import RTSPCam

IHD = "vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 24.1.0"
MESA = "vainfo: Driver version: Mesa Gallium driver 24.2.8 for AMD Radeon (radeonsi)"


def fake_tool(bin_dir, name, output="", rc=0):
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {rc}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def gpu(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    device = tmp_path / "renderD128"
    device.touch()
    fake_tool(bin_dir, "vainfo", IHD)
    fake_tool(bin_dir, "ffmpeg")
    return bin_dir, str(device)


async def test_pinned_working_igpu_passes(gpu):
    _, device = gpu
    ok, detail = await hw_check.check_hw(device, "qsv", "iHD")
    assert ok, detail
    assert "Intel iHD driver" in detail and "hevc_qsv" in detail


async def test_unpinned_driver_fails(gpu):
    _, device = gpu
    ok, detail = await hw_check.check_hw(device, "qsv", None)
    assert not ok and "no driver pinned" in detail


async def test_wrong_driver_loaded_fails(gpu):
    bin_dir, device = gpu
    fake_tool(bin_dir, "vainfo", MESA)
    ok, detail = await hw_check.check_hw(device, "qsv", "iHD")
    assert not ok and "radeonsi" in detail


async def test_driver_that_cannot_encode_fails(gpu):
    bin_dir, device = gpu
    fake_tool(bin_dir, "ffmpeg", "Error creating a MFX session: -9.", rc=1)
    ok, detail = await hw_check.check_hw(device, "qsv", "iHD")
    assert not ok and "MFX session" in detail


async def test_missing_render_node_fails(gpu, tmp_path):
    ok, detail = await hw_check.check_hw(str(tmp_path / "nope"), "qsv", "iHD")
    assert not ok and "not present" in detail


async def test_vainfo_failure_fails(gpu):
    bin_dir, device = gpu
    fake_tool(bin_dir, "vainfo", "va_openDriver() returns -1", rc=255)
    ok, detail = await hw_check.check_hw(device, "qsv", "iHD")
    assert not ok and "va_openDriver" in detail


async def test_amd_pin_matches_mesa(gpu):
    bin_dir, device = gpu
    fake_tool(bin_dir, "vainfo", MESA)
    ok, detail = await hw_check.check_hw(device, "vaapi", "radeonsi")
    assert ok and "hevc_vaapi" in detail


@pytest.fixture
def cam(gpu, monkeypatch):
    cam = RTSPCam.__new__(RTSPCam)
    cam.args = argparse.Namespace(
        lo_h265_transcode=True, hw_device=gpu[1], hw_backend="qsv"
    )
    cam.logger = logging.getLogger("test")
    monkeypatch.setenv("LIBVA_DRIVER_NAME", "iHD")
    return cam


async def test_failure_is_logged_as_error(cam, gpu, caplog):
    fake_tool(gpu[0], "vainfo", MESA)
    with caplog.at_level(logging.INFO):
        await cam._check_hw()
    assert any(
        r.levelno == logging.ERROR and "iGPU check FAILED" in r.message
        for r in caplog.records
    )


async def test_skipped_without_hw_transcode(cam, gpu, caplog):
    cam.args = argparse.Namespace(hw_device=gpu[1])
    with caplog.at_level(logging.INFO):
        await cam._check_hw()
    assert not caplog.records
