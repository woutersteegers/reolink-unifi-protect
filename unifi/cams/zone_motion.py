"""Optional RTSP foreground detection using Protect motion zones."""
import asyncio
import contextlib
import time

import cv2
import numpy as np

WIDTH, HEIGHT, FPS = 320, 180, 4
FRAME_BYTES = WIDTH * HEIGHT


def zone_masks(payload):
    if payload.get("enable") is not True:
        return {}
    masks = {}
    scale = np.array([WIDTH - 1, HEIGHT - 1]) / 1000
    for zone_id, zone in payload.get("zones", {}).items():
        points = np.asarray(zone.get("coord", []), dtype=float)
        if points.ndim != 1 or points.size < 6 or points.size % 2:
            continue
        if not np.isfinite(points).all() or np.any((points < 0) | (points > 1000)):
            continue
        polygon = np.rint(points.reshape(-1, 2) * scale).astype(np.int32)
        masks[str(zone_id)] = cv2.fillPoly(
            np.zeros((HEIGHT, WIDTH), dtype=np.uint8), [polygon], 255
        )
    return masks


class ZoneMotion:
    def __init__(self, camera):
        self.camera = camera
        self.masks = {}
        self.revision = 0
        self.settings_connection = None
        self.connection = None
        self.event_id = 0
        self.started = {}

    def configure(self, payload):
        self.masks = zone_masks(payload)
        self.settings_connection = self.camera._session
        self.revision += 1
        levels = {zone_id: zone.get("level") for zone_id, zone in (payload.get("zones") or {}).items()
                  if isinstance(zone, dict)}
        self.thresholds = {
            zone: round(90 * 3 ** ((50 - level) / 50))
            if type(level := levels.get(zone)) is int and 0 <= level <= 100 else 90
            for zone in self.masks
        }
        self.camera.logger.info("Protect motion enabled=%s levels=%s thresholds=%s",
                                payload.get("enable"), levels, self.thresholds)

    async def edge(self, edge, zone):
        if self.connection is None or self.connection is not self.camera._session:
            self.started.clear()
            return
        tick = round(time.monotonic() * 1000)
        wall = round(time.time() * 1000)
        if edge == "stop" and zone not in self.started:
            return
        best_tick, best_wall = self.started[zone] if edge == "stop" else (0, 0)
        self.event_id += 1
        payload = {
            "clockBestMonotonic": best_tick, "clockBestWall": best_wall,
            "clockMonotonic": tick, "clockStream": round(self.camera.get_uptime() * 1000),
            "clockStreamRate": 1000, "clockWall": wall,
            "edgeType": edge, "eventId": self.event_id, "eventType": "motion",
            "motionHeatmap": "", "motionHeatmapHeight": 0, "motionHeatmapWidth": 0,
            "motionRawHeatmapNPZ": "", "motionSnapshot": "", "motionSnapshotFullFoV": "",
            "motionSnapshotFullFoVHeight": 0, "motionSnapshotFullFoVWidth": 0,
            "motionSnapshotHeight": 0, "motionSnapshotWidth": 0, "sensorID": "0",
        }
        if edge == "start":
            payload["levels"] = {zone: 100}
        await self.camera.send(self.camera.gen_event("EventSmartMotion", payload))
        if edge == "start":
            self.started[zone] = (tick, wall)
        else:
            self.started.pop(zone, None)
        self.camera.logger.info("Basic motion %s zone=%s", edge, zone)

    async def stop_active(self):
        for zone in list(self.started):
            await self.edge("stop", zone)

    async def track(self):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", self.camera.args.rtsp_transport,
            "-i", self.camera.args.source[-1], "-an",
            "-vf", "fps=4,scale=320:180,format=gray",
            "-f", "rawvideo", "pipe:1", stdout=asyncio.subprocess.PIPE,
        )
        background = cv2.createBackgroundSubtractorMOG2(
            history=120, varThreshold=16, detectShadows=True
        )
        kernel = np.ones((3, 3), np.uint8)
        previous = None
        revision = -1
        consecutive, quiet = {}, {}
        suppress = 20
        try:
            while True:
                data = await asyncio.wait_for(proc.stdout.readexactly(FRAME_BYTES), 10)
                if self.connection is not self.camera._session:
                    self.connection = self.camera._session
                    self.started.clear()
                    consecutive.clear()
                    quiet.clear()
                if revision != self.revision:
                    await self.stop_active()
                    revision = self.revision
                    consecutive.clear()
                    quiet.clear()
                frame = np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH)
                mean = float(frame.mean())
                if previous is not None and abs(mean - previous) >= 18:
                    suppress = 8
                    consecutive.clear()
                previous = mean
                mask = background.apply(frame, learningRate=0.12 if suppress else -1)
                foreground = cv2.inRange(mask, 255, 255)
                zones = set()
                if suppress:
                    suppress -= 1
                elif self.connection is self.settings_connection:
                    for zone, mask in self.masks.items():
                        cropped = cv2.bitwise_and(foreground, mask)
                        cropped = cv2.morphologyEx(cropped, cv2.MORPH_OPEN, kernel)
                        _, _, stats, _ = cv2.connectedComponentsWithStats(cropped)
                        if stats[1:, cv2.CC_STAT_AREA].max(initial=0) >= self.thresholds[zone]:
                            zones.add(zone)
                for zone in list(self.masks):
                    qualifies = zone in zones
                    consecutive[zone] = consecutive.get(zone, 0) + 1 if qualifies else 0
                    quiet[zone] = 0 if qualifies else quiet.get(zone, 0) + 1
                    if zone not in self.started and consecutive[zone] >= 3:
                        await self.edge("start", zone)
                    elif zone in self.started and quiet[zone] >= 2 * FPS:
                        await self.edge("stop", zone)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.stop_active(), 2)

    async def run(self):
        while True:
            try:
                await self.track()
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                self.camera.logger.exception("Motion source interrupted; retrying")
                await asyncio.sleep(3)
