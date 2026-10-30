"""Write Protect's image settings (brightness, WDR, ...) to a Reolink camera.

Protect is the source of truth: when it sends ``ChangeIspSettings`` the changed
values are written to the camera once. Nothing is polled or read back.

Mapping (Protect 0-100 sliders to Reolink 0-255, where 50 is the 128 midpoint):

* brightness/contrast/saturation/hue/sharpness -> ``SetImage``
* denoise -> ``nr3d`` (on above 0), mirror -> ``mirroring``, flip -> ``rotation``
* wdr -> ``hdr`` on cameras that have it (doorbells): Protect low = Reolink Auto
  (2), middle and high = On (1), off = Off (0); otherwise ``backLight``
  ``DynamicRangeControl`` with ``drc`` set from the level, or ``Off`` at 0
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import aiohttp

IMAGE_FIELDS = {
    "brightness": "bright",
    "contrast": "contrast",
    "saturation": "saturation",
    "hue": "hue",
    "sharpness": "sharpen",
}
HANDLED = {*IMAGE_FIELDS, "denoise", "mirror", "flip", "wdr"}
# Reolink HDR is off/on/auto, so Protect's levels fold: low=auto, middle/high=on.
HDR_BY_WDR = {0: 0, 1: 2, 2: 1, 3: 1}
# Protect's WDR levels are 0 (off) to 3; spread them over the DRC range.
WDR_DRC_LEVELS = {1: 85, 2: 170, 3: 255}


def _auth_error(response: Any) -> bool:
    """Recognize the camera's explicit invalid/expired-session responses."""
    if isinstance(response, dict):
        response = [response]
    return isinstance(response, list) and any(
        isinstance(result, dict)
        and result.get("code") != 0
        and isinstance(result.get("error"), dict)
        and result["error"].get("rspCode") in (-6, -7)
        for result in response
    )


def scale(value: Any) -> int:
    return max(0, min(255, round(float(value) * 255 / 100)))


def plan_changes(
    options: dict[str, Any], isp: Optional[dict[str, Any]]
) -> tuple[dict[str, int], dict[str, Any]]:
    """Return (Image fields, Isp fields) to write for Protect's changed options.

    ``isp`` is the camera's current Isp block; it decides how WDR maps.
    """
    image: dict[str, int] = {}
    for key, field in IMAGE_FIELDS.items():
        if key in options:
            image[field] = scale(options[key])

    out: dict[str, Any] = {}
    if "denoise" in options:
        out["nr3d"] = 1 if int(options["denoise"]) > 0 else 0
    if "mirror" in options:
        out["mirroring"] = 1 if int(options["mirror"]) else 0
    if "flip" in options:
        out["rotation"] = 1 if int(options["flip"]) else 0
    if "wdr" in options:
        level = max(0, min(3, int(options["wdr"])))
        if isp is not None and "hdr" in isp:
            out["hdr"] = HDR_BY_WDR[level]
        elif level:
            out["backLight"] = "DynamicRangeControl"
            out["drc"] = WDR_DRC_LEVELS[level]
        else:
            out["backLight"] = "Off"
    return image, out


class ReolinkIsp:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        logger: logging.Logger,
        channel: int = 0,
        scheme: str = "https",
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.logger = logger
        self.channel = channel
        self.scheme = scheme
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()
        self._applied: dict[str, Any] = {}

    def _url(self, query: str) -> str:
        return f"{self.scheme}://{self.host}/cgi-bin/api.cgi?{query}"

    async def _post(self, query: str, body: list) -> list:
        url = self._url(query)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, ssl=False, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return await resp.json(content_type=None)

    async def _login(self) -> None:
        res = await self._post(
            "cmd=Login",
            [
                {
                    "cmd": "Login",
                    "param": {
                        "User": {
                            "Version": "0",
                            "userName": self.user,
                            "password": self.password,
                        }
                    },
                }
            ],
        )
        self._token = res[0]["value"]["Token"]["name"]

    async def _call(self, cmds: list) -> list:
        for attempt in (0, 1):
            if self._token is None:
                await self._login()
            res = await self._post(f"token={self._token}", cmds)
            # rspCode -6 / -7 mean the token is no longer valid.
            if attempt == 0 and _auth_error(res):
                self._token = None
                continue
            return res
        return res

    async def apply(self, options: dict[str, Any]) -> None:
        """Write the options that changed since the last successful apply."""
        changed = {
            k: v
            for k, v in options.items()
            if k in HANDLED and self._applied.get(k) != v
        }
        if not changed:
            return
        async with self._lock:
            try:
                isp_res = await self._call(
                    [{"cmd": "GetIsp", "action": 0, "param": {"channel": self.channel}}]
                )
                isp = isp_res[0].get("value", {}).get("Isp")
                image, isp_fields = plan_changes(changed, isp)
                cmds = []
                if image:
                    cmds.append(
                        {
                            "cmd": "SetImage",
                            "param": {"Image": {"channel": self.channel, **image}},
                        }
                    )
                if isp_fields and isp is not None:
                    cmds.append(
                        {
                            "cmd": "SetIsp",
                            "param": {"Isp": {**isp, **isp_fields}},
                        }
                    )
                if not cmds:
                    return
                res = await self._call(cmds)
                bad = [r for r in res if r.get("code") != 0]
                if bad:
                    self.logger.warning("Reolink rejected image settings: %s", bad)
                    return
                self._applied.update(changed)
                self.logger.info(
                    "Applied image settings to Reolink: %s", sorted(changed)
                )
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not apply image settings: %s", exc)

    async def reboot(self) -> bool:
        """Ask the camera to reboot. It stops answering for about a minute."""
        async with self._lock:
            try:
                res = await self._call([{"cmd": "Reboot", "action": 0, "param": {}}])
                ok = all(r.get("code") == 0 for r in res)
                if ok:
                    self.logger.info("Asked the Reolink camera to reboot")
                else:
                    self.logger.warning("Reolink refused the reboot: %s", res)
                return ok
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Could not reboot the Reolink camera: %s", exc)
                return False
            finally:
                self._token = None

    SNAP_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_connect=5)

    async def snapshot(self, dst: Path) -> bool:
        """Save a JPEG from the camera to dst, logged in with a token.

        A ``--snapshot-url`` with ``user=``/``password=`` in the query fails on recent
        firmware whenever the password has special characters, even percent-encoded;
        the token login takes the password in the request body and always works.
        """
        # Settings and reboot already hold this lock around _call(). Lock the
        # whole snapshot transaction here; locking _login() would deadlock those
        # callers, and a reboot must not invalidate a token during its Snap.
        async with self._lock:
            try:
                for attempt in (0, 1):
                    if self._token is None:
                        await self._login()
                    query = urllib.parse.urlencode(
                        {
                            "cmd": "Snap",
                            "channel": self.channel,
                            "rs": int(time.time() * 1000),
                            "token": self._token,
                        }
                    )
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            self._url(query), ssl=False, timeout=self.SNAP_TIMEOUT
                        ) as resp:
                            status = resp.status
                            body = await resp.read()
                    if 200 <= status < 300 and body[:2] == b"\xff\xd8":
                        # Readers must never see a half-written JPEG.
                        tmp = dst.with_name(dst.name + ".part")
                        tmp.write_bytes(body)
                        os.replace(tmp, dst)
                        return True
                    try:
                        response = json.loads(body)
                    except ValueError:
                        response = None
                    expired = status == 401 or (
                        (200 <= status < 300 or status == 403) and _auth_error(response)
                    )
                    if expired:
                        self._token = None
                        if attempt == 0:
                            continue
                        self.logger.warning(
                            "Reolink snapshot failed: authentication rejected"
                        )
                    else:
                        # An outage, denied permission, or malformed image is
                        # not evidence that the shared login has expired.
                        self.logger.warning(
                            "Reolink snapshot failed: HTTP %s returned no JPEG",
                            status,
                        )
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self.logger.warning("Reolink snapshot failed: %s", type(exc).__name__)
            except (KeyError, IndexError, TypeError, ValueError):
                self._token = None
                self.logger.warning("Reolink snapshot failed: login rejected")
            return False
