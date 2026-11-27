"""Translate active detector zones into native Protect motion events."""
import time


class MotionReporter:
    def __init__(self, camera):
        self.camera = camera
        self.started = {}
        self.event_id = 0

    async def edge(self, edge, zone):
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
