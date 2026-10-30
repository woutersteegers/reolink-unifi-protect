"""Drive a Reolink camera's pan/tilt/zoom motors from Protect's ``ContinuousMove``.

Protect sends one ``ContinuousMove`` per joystick/zoom-slider tick while held, each with a
``{x, y, z}`` vector on roughly a -1000..1000 "normalized" scale, and a final all-zero message on
release. Observed empirically against a real Reolink E1 Zoom (session 2026-10-12): pan/tilt and
zoom arrive as separate messages (a zoom tick has no x/y keys at all), ``x>0`` is right, ``y>0`` is
up. Reolink's own ``PtzCtrl`` (see the official HTTP API guide) takes a discrete, speed-scaled op
instead of a vector, and a single ``Stop`` halts whatever is moving. The guide lists diagonal pan/
tilt ops (LeftUp, RightDown, ...) and says ``ZoomDec``/``ZoomInc`` are zoom in/out, but on this
camera the diagonal ops return success (code 0) and never move it, and the zoom pair is the
opposite of the doc: confirmed by directly commanding the camera and watching it, not assumed.
So only the four cardinal pan/tilt ops are ever sent (picking whichever axis is dominant when a
drag is imprecisely diagonal), and ``z>0`` (what the UI treats as zoom in) maps to ``ZoomInc``.
"""

import asyncio
import logging
from typing import Any, Optional

import aiohttp

# Below this (on the -1000..1000 scale) a tick counts as released, matching the all-zero
# message Protect sends on release rather than requiring an exact 0.
DEADZONE = 20
MIN_SPEED = 1
# Reolink's own ceiling is 64. A full-deflection push at that speed felt too coarse in
# testing (noticeable overshoot, since Stop only reaches the camera after an HTTP round
# trip): capped lower here, then lowered a little further after trying 24 live. Revisit
# if this turns out to be too slow instead.
MAX_SPEED = 18


def plan_move(x: int, y: int, z: int) -> Optional[tuple[str, int]]:
    """Return (Reolink op, speed) for a ContinuousMove vector, or None to send Stop.

    Pan/tilt takes priority if both are present (Protect's own messages never mix them with
    zoom), and speed is the strongest axis scaled onto Reolink's 1-64 range.
    """
    if abs(x) >= DEADZONE or abs(y) >= DEADZONE:
        # This camera's firmware accepts diagonal ops but never actually moves for them
        # (confirmed by sending one directly and watching); only a true cardinal op does
        # anything, so pick whichever axis the drag leaned on harder.
        if abs(x) >= abs(y):
            op = "Right" if x > 0 else "Left"
        else:
            op = "Up" if y > 0 else "Down"
        return op, _scale_speed(max(abs(x), abs(y)))
    if abs(z) >= DEADZONE:
        return ("ZoomInc" if z > 0 else "ZoomDec"), _scale_speed(abs(z))
    return None


def _scale_speed(magnitude: int) -> int:
    return max(MIN_SPEED, min(MAX_SPEED, round(abs(magnitude) / 1000 * MAX_SPEED)))


class ReolinkPtz:
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
        # Many installs turn the camera's HTTPS off (Reolink's TLS is flaky).
        self.scheme = scheme
        self._token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # (op, speed) of the last command actually sent. Speed is part of the key:
        # Reolink has no speed-only update, so a stronger/weaker push mid-hold in the
        # same direction must still issue a new PtzCtrl, not just a direction change.
        self._last_sent: tuple[Optional[str], Optional[int]] = (None, None)

    async def _post(self, query: str, body: list) -> list:
        # One kept-alive connection: a fresh TLS handshake per command made every
        # PtzCtrl take ~420 ms against ~100 ms reused, so a Stop landed 300+ ms late
        # and the camera kept moving (overshoot on quick taps and direction changes).
        url = f"{self.scheme}://{self.host}/cgi-bin/api.cgi?{query}"
        for attempt in (0, 1):
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            try:
                async with self._session.post(
                    url, json=body, ssl=False, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return await resp.json(content_type=None)
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
                # The camera drops idle connections: reconnect once.
                await self.close()
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

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

    async def _ptz_ctrl(self, param: dict[str, Any]) -> bool:
        for attempt in (0, 1):
            if self._token is None:
                await self._login()
            res = await self._post(
                f"token={self._token}",
                [{"cmd": "PtzCtrl", "param": {"channel": self.channel, **param}}],
            )
            r = res[0]
            # rspCode -6 / -7 mean the token is no longer valid; any other failure
            # (bad op, camera busy, ...) is reported as-is, not blindly retried.
            if (
                attempt == 0
                and r.get("code") != 0
                and r.get("error", {}).get("rspCode") in (-6, -7)
            ):
                self._token = None
                continue
            return r.get("code") == 0

    async def goto_preset(self, preset_id: int, speed: int = 32) -> bool:
        """Drive the camera to one of its own saved presets (Reolink ``ToPos``).

        Never raises. Resets the dedup state so the next joystick tick is always sent.
        """
        async with self._lock:
            try:
                ok = await self._ptz_ctrl(
                    {"op": "ToPos", "id": preset_id, "speed": speed}
                )
                self._last_sent = (None, None)
                if ok:
                    self.logger.info("Reolink PTZ to preset %s", preset_id)
                else:
                    self.logger.warning("Reolink PTZ rejected preset %s", preset_id)
                return ok
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not go to preset %s: %s", preset_id, exc)
                return False

    async def continuous_move(self, x: int, y: int, z: int) -> None:
        """Apply one ContinuousMove tick. Never raises; a failure just logs and stops."""
        move = plan_move(x, y, z)
        op = move[0] if move else "Stop"
        speed = move[1] if move else None
        sent = (op, speed)
        if sent == self._last_sent:
            return  # identical op+speed already in effect, nothing to update
        async with self._lock:
            try:
                param = {"op": op}
                if move:
                    param["speed"] = speed
                ok = await self._ptz_ctrl(param)
                if ok:
                    self._last_sent = sent
                    self.logger.info("Reolink PTZ op %s (speed %s)", op, speed or "-")
                else:
                    self.logger.warning("Reolink PTZ rejected op %s", op)
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not send PTZ op %s: %s", op, exc)
