"""Drive a Reolink camera's built-in spotlight from Protect's "turn on the light".

A camera that advertises ``hasFlash`` gets Protect's light action (Alarm Manager "Turn
on light", the Spotlight toggle). Protect then sends an avclient ``TurnOnColorNightVision``
message with ``spotlightDuration`` in seconds and expects the light to go off by itself
afterwards. Reolink's ``SetWhiteLed`` takes a persistent ``state``, so the off is ours.

Only ``state`` (and optionally ``bright``) is written for a request. Protect's "Color Night
Vision" toggle (``colorNightVision`` in ``ChangeIspSettings``) maps to ``mode``: on is the
camera's own night mode, off disables the automatic light. The camera's schedule, brightness
and AI triggers (``GetWhiteLed``) are never touched.
"""

from typing import Optional

from unifi.reolink_ptz import ReolinkPtz

# Reolink WhiteLed.mode: 0 off, 1 night mode (the camera's own AI/schedule triggers)
OFF_MODE = 0
AUTO_MODE = 1


class ReolinkLight(ReolinkPtz):
    """Same session, login and token handling as the PTZ client, for ``SetWhiteLed``."""

    async def _api(self, cmd: str, param: dict) -> bool:
        for attempt in (0, 1):
            if self._token is None:
                await self._login()
            res = await self._post(
                f"token={self._token}", [{"cmd": cmd, "param": param}]
            )
            r = res[0]
            # rspCode -6 / -7: the token is no longer valid, log in once more
            if (
                attempt == 0
                and r.get("code") != 0
                and r.get("error", {}).get("rspCode") in (-6, -7)
            ):
                self._token = None
                continue
            return r.get("code") == 0
        return False

    async def set_spotlight(self, on: bool, brightness: Optional[int] = None) -> bool:
        """Switch the white LED on or off. Never raises."""
        led: dict = {"channel": self.channel, "state": 1 if on else 0}
        if on and brightness is not None:
            led["bright"] = max(0, min(100, int(brightness)))
        async with self._lock:
            try:
                ok = await self._api("SetWhiteLed", {"WhiteLed": led})
                level = self.logger.info if ok else self.logger.warning
                level("Reolink spotlight %s: %s", "on" if on else "off", ok)
                return ok
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not switch the spotlight: %s", exc)
                return False

    async def set_auto_mode(self, enabled: bool) -> bool:
        """Turn the camera's own automatic spotlight on (night mode) or off (never).

        Only ``mode`` is written, so the camera's schedule, brightness and AI triggers
        keep whatever they were set to. Never raises.
        """
        led = {"channel": self.channel, "mode": AUTO_MODE if enabled else OFF_MODE}
        async with self._lock:
            try:
                ok = await self._api("SetWhiteLed", {"WhiteLed": led})
                level = self.logger.info if ok else self.logger.warning
                level("Reolink spotlight auto mode %s: %s", enabled, ok)
                return ok
            except Exception as exc:  # noqa: BLE001
                self._token = None
                self.logger.warning("Could not set the spotlight mode: %s", exc)
                return False
