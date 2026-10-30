"""Startup self-test for the hardware transcode path.

The lower channels are re-encoded on the iGPU (--lo-h265-transcode). If the
image's libva picks the wrong driver, or the render node is missing, every
transcoded stream fails and the watchdog respawn-loops them with nothing in
the logs but ffmpeg noise. This checks once, up front, that the pinned
driver is the one that loaded and that the GPU can actually encode.
"""

import asyncio
import os
import re
from typing import Optional

# vainfo's "Driver version" line names the driver differently from
# LIBVA_DRIVER_NAME; map the pin to a substring of that line.
DRIVER_SIGNATURES = {
    "ihd": "intel ihd driver",
    "i965": "intel i965 driver",
    "radeonsi": "radeonsi",
}

ENCODERS = {"qsv": "hevc_qsv", "vaapi": "hevc_vaapi"}


async def _run(cmd: list[str], env: dict, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace")


def _driver_line(vainfo_output: str) -> Optional[str]:
    m = re.search(r"Driver version:\s*(.+)", vainfo_output)
    return m.group(1).strip() if m else None


def _encode_cmd(backend: str, device: str) -> list[str]:
    hwaccel = (
        ["-init_hw_device", f"qsv=hw,child_device={device}", "-filter_hw_device", "hw"]
        if backend == "qsv"
        else ["-vaapi_device", device]
    )
    upload = "format=nv12,hwupload=extra_hw_frames=16"
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        *hwaccel,
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
        "-frames:v", "5", "-vf", upload,
        "-c:v", ENCODERS[backend], "-f", "null", "-",
    ]  # fmt: skip


async def check_hw(
    device: str, backend: str, pinned: Optional[str], timeout: float = 20.0
) -> tuple[bool, str]:
    """(ok, detail). Fails if unpinned, wrong driver, or the encode fails."""
    env = dict(os.environ)
    if pinned:
        env["LIBVA_DRIVER_NAME"] = pinned
    else:
        return False, "no driver pinned (set LIBVA_DRIVER_NAME or --vaapi-driver)"
    if not os.path.exists(device):
        return False, f"render node {device} not present in the container"

    rc, out = await _run(
        ["vainfo", "--display", "drm", "--device", device], env, timeout
    )
    driver = _driver_line(out)
    if rc != 0 or not driver:
        tail = out.strip().splitlines()[-1:] or ["no output"]
        return False, f"vainfo failed (rc={rc}): {tail[0]}"
    signature = DRIVER_SIGNATURES.get(pinned.lower(), pinned.lower())
    if signature not in driver.lower():
        return False, f"pinned {pinned} but libva loaded: {driver}"

    rc, out = await _run(_encode_cmd(backend, device), env, timeout)
    if rc != 0:
        tail = out.strip().splitlines()[-1:] or ["no output"]
        return False, f"{ENCODERS[backend]} test encode failed (rc={rc}): {tail[0]}"
    return True, f"{driver}; {ENCODERS[backend]} encode OK on {device}"
