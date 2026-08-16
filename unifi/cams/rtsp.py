import argparse
import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from unifi.cams.base import SmartDetectObjectType, UnifiCamBase


class RTSPCam(UnifiCamBase):
    AI_CLEAR_AFTER_SEC = 2.5
    # Movement-gate tracker: boxes must displace before they count.
    AI_TRACK_MATCH_RADIUS = 60  # centre distance to join an existing track
    AI_TRACK_EXPIRE_SEC = 3.0  # forget a moving track this long after loss
    AI_STATIC_EXPIRE_SEC = 60.0  # remember never-moved spots much longer
    # so an intermittently re-detected building stays suppressed

    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        super().__init__(args, logger)
        self.args = args
        self.event_id = 0
        self.snapshot_dir = tempfile.mkdtemp()
        self.snapshot_stream = None
        self.runner = None
        self.sink_runner = None
        self._sink_last_ts = 0.0
        self._sink_last_boxes_ts = 0.0
        self._ai_tracks: list = []
        self._ai_track_seq = 0
        self._ai_active_lead = None
        self._ai_class_state: dict = {}
        self.stream_source = dict()
        for i, stream_index in enumerate(["video1", "video2", "video3"]):
            if not i < len(self.args.source):
                i = -1
            self.stream_source[stream_index] = self.args.source[i]
        if not self.args.snapshot_url:
            self.start_snapshot_stream()

    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        super().add_parser(parser)
        parser.add_argument(
            "--source",
            "-s",
            nargs="+",
            required=True,
            help="Source(s) for up to three streams in order of descending quality",
        )
        parser.add_argument(
            "--http-api",
            default=0,
            type=int,
            help="Specify a port number to enable the HTTP API (default: disabled)",
        )
        parser.add_argument(
            "--snapshot-url",
            "-i",
            default=None,
            type=str,
            required=False,
            help="HTTP endpoint to fetch snapshot image from",
        )
        # --- House: bridge the Reolink's ON-CAMERA AI into Protect smart
        # detections. Polls the HTTPS api (GetAiState) and maps
        # people/vehicle/dog_cat alarms to EventSmartDetect
        # person/vehicle/animal — no AI Port needed.
        parser.add_argument(
            "--reolink-ai-host",
            default=None,
            help="Camera IP; enables polling GetAiState over HTTPS",
        )
        parser.add_argument("--reolink-ai-user", default="admin")
        parser.add_argument("--reolink-ai-password", default=None)
        parser.add_argument(
            "--reolink-ai-interval",
            default=1.0,
            type=float,
            help="Seconds between GetAiState polls",
        )
        # Receives real detection BOXES from the nodelink-js sidecar
        # (deploy/ai-sidecar), which decodes the camera's own AI
        # rectangles off the Baichuan protocol. When box reports are
        # fresh, the coarse GetAiState poller stands down.
        parser.add_argument(
            "--ai-sink-port",
            default=0,
            type=int,
            help="Port to receive AI box reports from the sidecar",
        )
        parser.add_argument(
            "--ai-min-confidence",
            default=0.75,
            type=float,
            help="Ignore AI detections below this confidence (0-1)",
        )
        parser.add_argument(
            "--ai-min-movement",
            default=10,
            type=int,
            help="Suppress boxes until they move this far (0-1000 units;"
            " kills stationary false positives like buildings; 0 disables)",
        )
        parser.add_argument(
            "--ai-classes",
            default="person,vehicle,animal",
            help="Detection classes to forward, in priority order",
        )
        parser.add_argument(
            "--ai-confidence-overrides",
            default="",
            help="Per-class minimum confidence, e.g. 'animal=0.9' — for"
            " classes the camera misfires on (a person read as dog_cat)",
        )

    def start_snapshot_stream(self) -> None:
        if not self.snapshot_stream or self.snapshot_stream.poll() is not None:
            cmd = (
                f"ffmpeg -nostdin -y -re -rtsp_transport {self.args.rtsp_transport} "
                f'-i "{self.args.source[-1]}" '
                "-r 1 "
                f"-update 1 {self.snapshot_dir}/screen.jpg"
            )
            self.logger.info(
                f"Spawning stream for snapshots: {self.redact_secrets(cmd)}"
            )
            self.snapshot_stream = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True
            )

    async def get_snapshot(self) -> Path:
        img_file = Path(self.snapshot_dir, "screen.jpg")
        if self.args.snapshot_url:
            await self.fetch_to_file(self.args.snapshot_url, img_file)
        else:
            self.start_snapshot_stream()
        return img_file

    async def run(self) -> None:
        if self.args.http_api:
            self.logger.info(f"Enabling HTTP API on port {self.args.http_api}")

            app = web.Application()

            async def start_motion(request):
                self.logger.debug("Starting motion")
                await self.trigger_motion_start()
                return web.Response(text="ok")

            async def stop_motion(request):
                self.logger.debug("Starting motion")
                await self.trigger_motion_stop()
                return web.Response(text="ok")

            app.add_routes([web.get("/start_motion", start_motion)])
            app.add_routes([web.get("/stop_motion", stop_motion)])

            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, port=self.args.http_api)
            await site.start()

        if self.args.ai_sink_port:
            self.logger.info(f"AI box sink listening on {self.args.ai_sink_port}")
            sink = web.Application()
            sink.add_routes([web.post("/detections", self._handle_detections)])
            self.sink_runner = web.AppRunner(sink)
            await self.sink_runner.setup()
            await web.TCPSite(self.sink_runner, port=self.args.ai_sink_port).start()

        if self.args.reolink_ai_host:
            await self._poll_reolink_ai()

    async def _handle_detections(self, request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        try:
            await self._on_ai_boxes(data.get("boxes") or [])
        except Exception:
            self.logger.exception("Failed to process AI box report")
        return web.Response(text="ok")

    def _filter_stationary(self, boxes: list) -> list:
        """Drop boxes that have never moved.

        Scenery misclassified by the camera (a distant house read as a
        person, a parked car) produces a box that sits still; anything
        real displaces within a report or two. Tracks that never moved
        are remembered for a long time so intermittent re-detections of
        the same spot stay suppressed.
        """
        if not self.args.ai_min_movement:
            return boxes
        now = time.time()
        self._ai_tracks = [
            t
            for t in self._ai_tracks
            if now - t["last_ts"]
            < (
                self.AI_TRACK_EXPIRE_SEC
                if t["moved"] >= self.args.ai_min_movement
                else self.AI_STATIC_EXPIRE_SEC
            )
        ]
        out = []
        for box in boxes:
            coord = box.get("coord") or []
            if len(coord) != 4:
                continue
            cx = coord[0] + coord[2] / 2
            cy = coord[1] + coord[3] / 2
            track = None
            best = self.AI_TRACK_MATCH_RADIUS
            for t in self._ai_tracks:
                if t["type"] != box.get("type"):
                    continue
                d = abs(t["cx"] - cx) + abs(t["cy"] - cy)
                if d < best:
                    best, track = d, t
            if track is None:
                self._ai_track_seq += 1
                track = {
                    "id": self._ai_track_seq,
                    "type": box.get("type"),
                    "ox": cx,
                    "oy": cy,
                    "first_ts": now,
                    "moved": 0.0,
                }
                self._ai_tracks.append(track)
            # Stable per-object id: without it the Protect UI stacks a
            # fresh box per update instead of moving one.
            box["track_id"] = track["id"]
            track["cx"], track["cy"], track["last_ts"] = cx, cy, now
            track["moved"] = max(
                track["moved"], abs(cx - track["ox"]) + abs(cy - track["oy"])
            )
            if track["moved"] >= self.args.ai_min_movement:
                out.append(box)
        return out

    def _min_confidence(self, kind: str) -> float:
        overrides = getattr(self, "_conf_overrides", None)
        if overrides is None:
            overrides = {}
            for part in (self.args.ai_confidence_overrides or "").split(","):
                if "=" in part:
                    key, _, value = part.partition("=")
                    try:
                        overrides[key.strip()] = float(value)
                    except ValueError:
                        pass
            self._conf_overrides = overrides
        return overrides.get(kind, self.args.ai_min_confidence)

    async def _on_ai_boxes(self, boxes: list) -> None:
        self._sink_last_ts = time.time()
        # Raw view of everything the camera reports, before any filter —
        # the ground truth when diagnosing missed/misclassified subjects.
        if boxes and time.time() - getattr(self, "_ai_raw_log_ts", 0) > 1.0:
            self._ai_raw_log_ts = time.time()
            raw = ", ".join(
                f"{b.get('type')}@{int((b.get('confidence') or 0) * 100)}%"
                f"{b.get('coord')}"
                for b in boxes
            )
            self.logger.info(f"AI raw: {raw}")
        boxes = self._filter_stationary(boxes)
        priority = [c.strip() for c in self.args.ai_classes.split(",") if c.strip()]

        # Relabel from the authoritative class state (GetAiState poller):
        # the sidecar's box labels are a shape guess at best. With a
        # single active class every box is that class; with several, keep
        # the shape guess if it's plausible, else assign the top active.
        now = time.time()
        active = [
            t
            for t in priority
            if now - self._ai_class_state.get(t, 0) < 3.0
        ]
        if active:
            for box in boxes:
                if box.get("type") not in active:
                    box["type"] = active[0]
                if len(active) == 1:
                    box["type"] = active[0]
        elif not self._motion_event_ts:
            # No class authority yet (poll lag ≤1s): hold off starting an
            # event under a guessed label; the next report will know.
            return
        descriptors = []
        for i, box in enumerate(boxes):
            kind = box.get("type")
            coord = box.get("coord")
            if kind not in priority or not coord or len(coord) != 4:
                continue
            confidence = float(box.get("confidence") or 0.8)
            if confidence < self._min_confidence(kind):
                continue
            descriptors.append(
                self.build_smart_descriptor(
                    kind, coord, confidence, box.get("track_id", 9000 + i)
                )
            )
        if descriptors:
            self._sink_last_boxes_ts = time.time()
            present = {d["objectType"] for d in descriptors}
            lead = next(t for t in priority if t in present)
            detail = ", ".join(
                f"{d['objectType']}@{d['confidenceLevel']}%" for d in descriptors
            )
            if not self._motion_event_ts:
                self.logger.info(f"AI boxes: {detail} — starting smart event")
                await self.trigger_motion_start(
                    SmartDetectObjectType(lead), descriptors=descriptors
                )
                self._ai_active_lead = lead
            elif (
                self._ai_active_lead
                and lead in priority
                and priority.index(lead) < priority.index(self._ai_active_lead)
            ):
                # A higher-priority class appeared (e.g. the camera first
                # misread a person as an animal, then recognized them):
                # retype by restarting the event under the better class.
                self.logger.info(
                    f"AI class upgrade {self._ai_active_lead} -> {lead}"
                    f" ({detail}) — retyping event"
                )
                await self.trigger_motion_stop()
                await self.trigger_motion_start(
                    SmartDetectObjectType(lead), descriptors=descriptors
                )
                self._ai_active_lead = lead
            else:
                await self.trigger_motion_update(descriptors)
        elif (
            self._motion_event_ts
            and time.time() - getattr(self, "_sink_last_boxes_ts", 0)
            > self.AI_CLEAR_AFTER_SEC
        ):
            self.logger.info("AI boxes clear — ending smart event")
            await self.trigger_motion_stop()
            self._ai_active_lead = None

    async def _poll_reolink_ai(self) -> None:
        url = (
            f"https://{self.args.reolink_ai_host}/cgi-bin/api.cgi"
            f"?cmd=GetAiState&channel=0&user={self.args.reolink_ai_user}"
            f"&password={self.args.reolink_ai_password}"
        )
        self.logger.info(
            f"Polling Reolink AI state on {self.args.reolink_ai_host}"
            f" every {self.args.reolink_ai_interval}s"
        )
        # Priority when several classes fire at once; Protect models one
        # active event at a time.
        classes = [
            ("people", SmartDetectObjectType.PERSON),
            ("vehicle", SmartDetectObjectType.VEHICLE),
            ("dog_cat", SmartDetectObjectType.ANIMAL),
        ]
        active: Optional[SmartDetectObjectType] = None
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(
                        url, ssl=False, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json(content_type=None)
                    value = data[0].get("value", {})
                    desired = None
                    now = time.time()
                    for key, object_type in classes:
                        entry = value.get(key) or {}
                        if entry.get("support") and entry.get("alarm_state"):
                            # CLASS AUTHORITY for the box sink: GetAiState
                            # is the same detector state the Reolink app
                            # labels from (always right in practice); the
                            # Baichuan box wrappers carry no class at all
                            # on this firmware (identical confidence under
                            # every wrapper).
                            self._ai_class_state[object_type.value] = now
                            if desired is None:
                                desired = object_type
                    # While the box sidecar is alive it drives the events;
                    # the poller then only maintains the class states.
                    if time.time() - self._sink_last_ts < 10:
                        active = None
                    elif desired and active is None:
                        self.logger.info(f"Reolink AI: {desired.value} detected")
                        await self.trigger_motion_start(desired)
                        active = desired
                    elif desired is None and active is not None:
                        self.logger.info("Reolink AI: clear")
                        await self.trigger_motion_stop()
                        active = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Reolink AI poll failed: {e}")
                await asyncio.sleep(self.args.reolink_ai_interval)

    async def close(self) -> None:
        await super().close()
        if self.runner:
            await self.runner.cleanup()
        if self.sink_runner:
            await self.sink_runner.cleanup()

        if self.snapshot_stream:
            self.snapshot_stream.kill()

    async def get_stream_source(self, stream_index: str) -> str:
        return self.stream_source[stream_index]
