import argparse
import asyncio
import logging
import os
import shlex
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from unifi.cams.base import SmartDetectObjectType, UnifiCamBase, jpeg_size
from unifi.reolink_isp import ReolinkIsp
from unifi.reolink_light import ReolinkLight
from unifi.reolink_ptz import ReolinkPtz


class RTSPCam(UnifiCamBase):
    AI_CLEAR_AFTER_SEC = 2.5
    REBOOT_COOLDOWN_SEC = 300
    # Movement-gate tracker: boxes must displace before they count.
    AI_TRACK_MATCH_RADIUS = 60  # centre distance to join an existing track
    # On-demand mode keeps every box, so the tracker's only job is one stable id per object:
    # a cat covers 70-150 units between reports, which the tight radius split into three.
    AI_TRACK_MATCH_RADIUS_ENRICH = 250
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
        self._ai_active = None
        self._ai_last_event_ts = 0.0
        self.stream_source = dict()
        for i, stream_index in enumerate(["video1", "video2", "video3"]):
            if not i < len(self.args.source):
                i = -1
            self.stream_source[stream_index] = self.args.source[i]
        if not self._http_snapshots():
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
        parser.add_argument(
            "--doorbell",
            action="store_true",
            help="Advertise the camera as a doorbell (featureFlags.doorbell)",
        )
        parser.add_argument(
            "--ptz",
            action="store_true",
            help="Advertise a pan/tilt/zoom camera so Protect shows its PTZ controls",
        )
        parser.add_argument(
            "--ptz-home-preset",
            type=int,
            default=0,
            help="Reolink preset id the camera returns to for Protect's Home View"
            " (0 is the camera's 'default' preset)",
        )
        parser.add_argument(
            "--spotlight",
            action="store_true",
            help="Advertise a spotlight so Protect's light action switches the Reolink's"
            " white LED on for the requested time (needs --reolink-ai-host)",
        )
        parser.add_argument(
            "--spotlight-brightness",
            type=int,
            default=None,
            help="Brightness 0-100 when Protect turns the spotlight on"
            " (default: keep the camera's own setting)",
        )
        parser.add_argument(
            "--spotlight-default-seconds",
            type=int,
            default=15,
            help="How long the spotlight stays on when neither the request nor Protect's"
            " 'turn off after no motion' slider gives a duration",
        )
        parser.add_argument(
            "--speaker",
            action="store_true",
            help="Advertise hasSpeaker and log Protect's talkback audio (experiment)",
        )
        parser.add_argument(
            "--talkback-bridge",
            action="store_true",
            help="Play Protect's talkback audio on the camera speaker (needs --speaker)",
        )
        parser.add_argument(
            "--reolink-push-button",
            action="store_true",
            help="Detect the doorbell button from Baichuan push (port 9000, no HTTPS)"
            " instead of polling GetEvents",
        )
        parser.add_argument(
            "--reolink-push-ai",
            action="store_true",
            help="Drive AI detections (person/vehicle/animal) from Baichuan push"
            " instead of polling GetAiState; implies the push connection",
        )
        parser.add_argument(
            "--reolink-isp",
            action="store_true",
            help="Write Protect's image settings (brightness, contrast, WDR, ...)"
            " to the camera when they change (needs --reolink-ai-host)",
        )
        parser.add_argument(
            "--reolink-reboot",
            action="store_true",
            help="Reboot the Reolink camera when Protect's restart camera is used"
            " (needs --reolink-ai-host)",
        )
        parser.add_argument(
            "--reolink-scheme",
            default="https",
            choices=["https", "http"],
            help="Scheme for the camera's web API on --reolink-ai-host (use http when"
            " the camera's HTTPS is turned off)",
        )
        parser.add_argument(
            "--reolink-snapshot",
            action="store_true",
            help="Fetch snapshots from --reolink-ai-host with a token login instead of"
            " --snapshot-url; works with passwords that break user/password URLs",
        )
        parser.add_argument(
            "--reolink-visitor-poll",
            action="store_true",
            help="Poll GetEvents on --reolink-ai-host for doorbell button presses",
        )
        parser.add_argument(
            "--protect-ring-user",
            default=None,
            help="Local Protect user used to log in and ring on a button press",
        )
        parser.add_argument("--protect-ring-password", default=None)
        parser.add_argument(
            "--reolink-visitor-interval",
            default=1.0,
            type=float,
            help="Seconds between GetEvents visitor polls",
        )
        # Receives real detection BOXES from the nodelink-js sidecar
        # (deploy/ai-sidecar), which decodes the camera's own AI
        # rectangles off the Baichuan protocol. When box reports are
        # fresh, the coarse GetAiState poller stands down.
        parser.add_argument(
            "--ai-sink-port",
            default=0,
            type=int,
            help="Port to receive AI box reports from a standalone sidecar"
            " (defaults to loopback 8099 with --ai-sidecar)",
        )
        parser.add_argument(
            "--ai-sidecar",
            action="store_true",
            help="Run the bundled Baichuan box sidecar (Node, in the image) so events"
            " carry the camera's real moving detection boxes and per-object crops."
            " Needs --reolink-ai-host; the standby replica never starts it",
        )
        parser.add_argument(
            "--ai-sidecar-profile",
            default="main",
            choices=["main", "sub"],
            help="Baichuan stream the boxes are read from. Doorbells carry them on"
            " the main stream only",
        )
        parser.add_argument(
            "--ai-sidecar-always-on",
            action="store_true",
            help="Keep the sidecar's stream open continuously (about 4.5 Mbit/s for"
            " a doorbell's main stream) instead of only during detections",
        )
        parser.add_argument(
            "--ai-sidecar-path",
            default="/opt/ai-sidecar/index.mjs",
            help="Where the sidecar script lives in the image",
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
            "--ai-movement-box-ratio",
            default=0.25,
            type=float,
            help="Movement must also exceed this fraction of the box's"
            " smaller side, so box-size jitter on large static objects"
            " doesn't count as motion (0 = use --ai-min-movement only)",
        )
        parser.add_argument(
            "--ai-movement-reports",
            default=2,
            type=int,
            help="Consecutive box reports (~0.4s apart) that must show the"
            " movement before a track counts as moving; 1 = no smoothing",
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
                f"-i {shlex.quote(self.args.source[-1])} "
                "-r 1 "
                f"-update 1 {self.snapshot_dir}/screen.jpg"
            )
            # Keep credential-bearing shell commands out of startup logs.
            self.logger.info("Spawning stream for snapshots")
            self.snapshot_stream = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True
            )

    def _reolink_snapshots(self) -> bool:
        return bool(
            getattr(self.args, "reolink_snapshot", False)
            and getattr(self.args, "reolink_ai_host", None)
        )

    def _http_snapshots(self) -> bool:
        """Snapshots are fetched per request, not read from a 4th RTSP session."""
        return bool(
            getattr(self.args, "snapshot_url", None) or self._reolink_snapshots()
        )

    async def _fetch_snapshot(self, dst: Path) -> bool:
        if self._reolink_snapshots():
            return await self._reolink_client().snapshot(dst)
        return await self.fetch_to_file(self.args.snapshot_url, dst)

    def _reolink_scheme(self) -> str:
        return getattr(self.args, "reolink_scheme", None) or "https"

    async def get_snapshot(self) -> Path:
        img_file = Path(self.snapshot_dir, "screen.jpg")
        if self._http_snapshots():
            await self._fetch_snapshot(img_file)
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

        bundled_sidecar = bool(
            getattr(self.args, "ai_sidecar", False) and self.args.reolink_ai_host
        )
        if bundled_sidecar and not self.args.ai_sink_port:
            self.args.ai_sink_port = self.SIDECAR_SINK_PORT
        if self.args.ai_sink_port:
            self.logger.info(f"AI box sink listening on {self.args.ai_sink_port}")
            sink = web.Application()
            sink.add_routes([web.post("/detections", self._handle_detections)])
            sink.add_routes([web.get("/state", self._handle_sidecar_state)])
            self.sink_runner = web.AppRunner(sink)
            await self.sink_runner.setup()
            # The bundled sidecar is in this container: keep the sink off the pod network.
            host = "127.0.0.1" if bundled_sidecar else None
            await web.TCPSite(
                self.sink_runner, host=host, port=self.args.ai_sink_port
            ).start()
        if bundled_sidecar:
            self._sidecar_task = asyncio.create_task(self._supervise_sidecar())

        if self.args.reolink_ai_host and self.args.reolink_visitor_poll:
            self._visitor_task = asyncio.create_task(self._poll_reolink_visitor())
        if self.args.reolink_ai_host and (
            self.args.reolink_push_button or self.args.reolink_push_ai
        ):
            self._push_task = asyncio.create_task(self._push_reolink())

        if self.args.reolink_ai_host and not self.args.reolink_push_ai:
            await self._poll_reolink_ai()
        elif getattr(self, "_push_task", None):
            await self._push_task
        elif getattr(self, "_visitor_task", None):
            await self._visitor_task

    # Loopback port the bundled sidecar reports to when --ai-sink-port is not given.
    SIDECAR_SINK_PORT = 8099

    def _enrich_only(self) -> bool:
        """On-demand sidecar: the push channel starts/stops events, boxes only enrich them.

        The sidecar's stream is open only while a detection is in progress, so it must
        never start an event (it is not running yet) or end one (its first reports are
        empty while the stream warms up), and it must not suppress the push channel.
        """
        return bool(
            getattr(self.args, "ai_sidecar", False)
            and not getattr(self.args, "ai_sidecar_always_on", False)
        )

    async def _handle_sidecar_state(self, request) -> web.Response:
        # The sidecar opens its (expensive) camera stream only while this is true.
        active = (
            getattr(self, "_ai_active", None) is not None
            or getattr(self, "_motion_event_ts", None) is not None
        )
        return web.json_response({"active": active})

    def _sidecar_env(self) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "CAM_IP": str(self.args.reolink_ai_host),
                "CAM_USER": str(self.args.reolink_ai_user),
                "CAM_PASS": str(self.args.reolink_ai_password),
                "SINK_URL": f"http://127.0.0.1:{self.args.ai_sink_port}/detections",
                "PROFILE": self.args.ai_sidecar_profile,
            }
        )
        if not self.args.ai_sidecar_always_on:
            env["STATE_URL"] = f"http://127.0.0.1:{self.args.ai_sink_port}/state"
        return env

    def _log_sidecar_line(self, line: str) -> None:
        if (
            line.startswith(
                ("baichuan login", "stream on", "stream off", "sink post failed")
            )
            or "session ended" in line
        ):
            self.logger.info(f"AI sidecar: {line}")
        else:
            self.logger.debug(f"AI sidecar: {line}")

    async def _supervise_sidecar(self) -> None:
        # Never takes the proxy down: if node is missing or the sidecar keeps dying, events
        # simply keep coming from the push channel.
        delay = 5.0
        while True:
            started = time.monotonic()
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "node",
                    self.args.ai_sidecar_path,
                    env=self._sidecar_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                self.logger.info(f"AI sidecar started (pid {proc.pid})")
                async for raw in proc.stdout:
                    self._log_sidecar_line(raw.decode(errors="replace").rstrip())
                self.logger.warning(f"AI sidecar exited (rc={await proc.wait()})")
            except asyncio.CancelledError:
                if proc is not None and proc.returncode is None:
                    proc.terminate()
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"AI sidecar could not run: {exc}")
            if time.monotonic() - started > 60:
                delay = 5.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    def _drop_placeholder_crop(self) -> None:
        """Real boxes supersede the centred placeholder crop the push start made."""
        tid = self.synthetic_tracker_id()
        kept = [e for e in self._smart_snapshots if e["trackerID"] != tid]
        if len(kept) == len(self._smart_snapshots):
            return
        self._smart_snapshots = kept
        path = self._smart_snapshot_files.pop(f"detect_{tid}.jpg", None)
        if path is not None:
            path.unlink(missing_ok=True)

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
        enrich = self._enrich_only()
        if not self.args.ai_min_movement and not enrich:
            return boxes
        now = time.time()
        self._ai_tracks = [
            t
            for t in self._ai_tracks
            if now - t["last_ts"]
            < (self.AI_TRACK_EXPIRE_SEC if t["moving"] else self.AI_STATIC_EXPIRE_SEC)
        ]
        out = []
        for box in boxes:
            coord = box.get("coord") or []
            if len(coord) != 4:
                continue
            cx = coord[0] + coord[2] / 2
            cy = coord[1] + coord[3] / 2
            track = None
            best = (
                self.AI_TRACK_MATCH_RADIUS_ENRICH
                if enrich
                else self.AI_TRACK_MATCH_RADIUS
            )
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
                    "moving": False,
                    "streak": 0,
                }
                self._ai_tracks.append(track)
            # Stable per-object id: without it the Protect UI stacks a
            # fresh box per update instead of moving one.
            box["track_id"] = track["id"]
            track["cx"], track["cy"], track["last_ts"] = cx, cy, now
            # Latched: a person who walks in and then stands still keeps
            # their event. The latch is why the decision itself must be
            # robust — one jittery report used to flip a sprinkler to
            # "moving" for good (5k phantom animal boxes in 72h).
            if not track["moving"]:
                track["moving"] = self._track_has_moved(
                    track,
                    abs(cx - track["ox"]) + abs(cy - track["oy"]),
                    coord[2],
                    coord[3],
                )
            # Enrich-only keeps every box: a cat sitting still is still the detection. The
            # tracks are still matched so each object keeps one stable id across reports.
            if track["moving"] or enrich:
                out.append(box)
        return out

    def _track_has_moved(
        self, track: dict, displacement: float, w: float, h: float
    ) -> bool:
        """Does this report show a not-yet-moving track really moving?

        `displacement` is the Manhattan distance (0-1000 units) of the box
        centre from where the track was first seen; `w`/`h` are this
        report's box size. `track["streak"]` is free for counting
        consecutive qualifying reports (reports arrive every ~0.4s).

        Must ignore detector jitter on static scenery: the backyard
        sprinkler reports as [805, 923, 34, 74] but occasionally as
        75 wide or 148 tall, shifting its centre ~20 units for a single
        report. Must still pass a person walking through the yard.
        """
        threshold = max(
            self.args.ai_min_movement,
            self.args.ai_movement_box_ratio * min(w, h),
        )
        if displacement >= threshold:
            track["streak"] += 1
        else:
            track["streak"] = 0
        return track["streak"] >= self.args.ai_movement_reports

    # In on-demand mode the camera has already declared the detection, so boxes only decorate
    # it: a cat reads 30-70% and should still get its box. Junk-box protection is for
    # *starting* events, which boxes no longer do there.
    SIDECAR_MIN_CONFIDENCE = 0.2

    def _min_confidence(self, kind: str) -> float:
        if self._enrich_only():
            return self.SIDECAR_MIN_CONFIDENCE
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
        if self._enrich_only() and not self._motion_event_ts:
            return
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

        # Box classes come straight off the wire now (type2 = class on
        # this firmware, per-object, same source the app labels from) —
        # no relabeling needed.
        descriptors = []
        for i, box in enumerate(boxes):
            kind = box.get("type")
            coord = box.get("coord")
            if kind not in priority or not coord or len(coord) != 4:
                continue
            # Classes the user disabled in Protect's camera settings
            # (ChangeSmartMotionSettings) are dropped here — the camera
            # keeps classifying them on the wire regardless of any
            # Reolink-app toggle.
            if (
                self.smart_detect_enabled is not None
                and kind not in self.smart_detect_enabled
            ):
                continue
            confidence = float(box.get("confidence") or 0.8)
            if confidence < self._min_confidence(kind):
                continue
            # Honor the zones drawn in Protect: a detection whose box
            # center sits outside every smart-detect zone (or inside an
            # exclude zone) never becomes an event.
            zone_ids = self.smart_zones_containing(
                kind, coord[0] + coord[2] / 2, coord[1] + coord[3] / 2
            )
            if zone_ids is not None and not zone_ids:
                continue
            descriptors.append(
                self.build_smart_descriptor(
                    kind,
                    coord,
                    confidence,
                    box.get("track_id", 9000 + i),
                    zones=zone_ids,
                )
            )
        if descriptors:
            self._sink_last_boxes_ts = time.time()
            self._drop_placeholder_crop()
            present = {d["objectType"] for d in descriptors}
            lead = next(t for t in priority if t in present)
            detail = ", ".join(
                f"{d['objectType']}@{d['confidenceLevel']}%" for d in descriptors
            )
            if not self._motion_event_ts:
                self.logger.info(f"AI boxes: {detail} — starting smart event")
                self._ai_crops_made = set()
                await self.trigger_motion_start(
                    SmartDetectObjectType(lead), descriptors=descriptors
                )
                self._ai_active_lead = lead
            elif (
                not self._enrich_only()
                and self._ai_active_lead
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
                saved_crops = list(self._smart_snapshots)
                await self.trigger_motion_stop()
                await self.trigger_motion_start(
                    SmartDetectObjectType(lead), descriptors=descriptors
                )
                # Carry the crops over to the retyped event.
                self._smart_snapshots = saved_crops
                self._ai_active_lead = lead
            else:
                await self.trigger_motion_update(descriptors)
            # One square crop per tracked object, generated from a fresh
            # camera snapshot around its current box — the source of the
            # zoomed event thumbnail Protect shows (see base.py notes).
            made = getattr(self, "_ai_crops_made", None)
            if made is None:
                made = self._ai_crops_made = set()
            for d in descriptors:
                if d["trackerID"] not in made:
                    made.add(d["trackerID"])
                    asyncio.create_task(
                        self._make_object_crop(
                            d["trackerID"], d["objectType"], d["coord"]
                        )
                    )
        elif (
            self._motion_event_ts
            and not self._enrich_only()
            and time.time() - getattr(self, "_sink_last_boxes_ts", 0)
            > self.AI_CLEAR_AFTER_SEC
        ):
            self.logger.info("AI boxes clear — ending smart event")
            await self.trigger_motion_stop()
            self._ai_active_lead = None

    async def _make_object_crop(self, tracker_id: int, kind: str, coord: list) -> None:
        """Square crop around one detection, from a fresh camera snapshot.

        Announced via smartDetectSnapshots at event stop; Protect fetches
        it by filename and shows it as the event's zoomed thumbnail. The
        crop is square (Protect's isRealCropped requires it) and padded
        around the box so the subject has context.
        """
        fetched = Path(self.snapshot_dir, f"crop_src_{tracker_id}.jpg")
        try:
            if self._http_snapshots():
                if not await self._fetch_snapshot(fetched):
                    return
                src = fetched
            else:
                snap = await self.get_snapshot()
                if not snap.exists():
                    return
                src = snap
            ts_ms = int(round(time.time() * 1000))

            # Size the crop from the frame actually being cropped: the snapshot
            # can be the small sub-stream image, not the main stream's size.
            size = jpeg_size(src)
            if size:
                fw, fh = size
            else:
                fw = int(getattr(self.args, "hi_width", 0) or 1920)
                fh = int(getattr(self.args, "hi_height", 0) or 1080)
            x, y, w, h = (float(c) for c in coord)
            box_w, box_h = w * fw / 1000, h * fh / 1000
            cx, cy = (x + w / 2) * fw / 1000, (y + h / 2) * fh / 1000
            side = int(min(fw, fh, max(320, min(fh, max(box_w, box_h) * 2.2))))
            left = int(min(max(cx - side / 2, 0), fw - side))
            top = int(min(max(cy - side / 2, 0), fh - side))

            fname = f"detect_{tracker_id}.jpg"
            out = Path(self.snapshot_dir, fname)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vf",
                f"crop={side}:{side}:{left}:{top}",
                str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
            if proc.returncode != 0 or not out.exists():
                self.logger.warning(f"Object crop failed for tracker {tracker_id}")
                return
            self._smart_snapshot_files[fname] = out
            self.prune_stale_crops()
            if not self._motion_event_ts:
                # Event already ended; the stop payload went out without
                # this crop and appending now would pollute the next one.
                return
            self._smart_snapshots.append(
                {
                    "trackerID": int(tracker_id),
                    "clockBestMonotonic": int(self.get_uptime()),
                    "clockBestWall": ts_ms,
                    "confidenceLevel": 90,
                    "coord": [int(c) for c in coord],
                    "framingRect": [int(c) for c in coord],
                    "reVerifyEligible": False,
                    "smartDetectSnapshot": fname,
                    "smartDetectSnapshotHeight": side,
                    "smartDetectSnapshotType": kind,
                    "smartDetectSnapshotName": fname,
                    "smartDetectSnapshotWidth": side,
                }
            )
            self.logger.info(
                f"Object crop ready: {kind}#{tracker_id}"
                f" {side}x{side}@{left},{top} -> {fname}"
            )
        except Exception:
            self.logger.exception("Failed to build object crop")
        finally:
            # The fetched full frame is only an ffmpeg input; the crop is
            # what Protect asks for. Never touches screen.jpg, which the
            # snapshot-stream path uses as its source instead.
            fetched.unlink(missing_ok=True)

    async def get_video_settings(self) -> dict:
        # Protect owns these values: echo back what it last set, never the camera's.
        return dict(getattr(self, "_isp_state", {}))

    def _reolink_client(self) -> ReolinkIsp:
        if not hasattr(self, "_isp"):
            self._isp = ReolinkIsp(
                self.args.reolink_ai_host,
                self.args.reolink_ai_user,
                self.args.reolink_ai_password,
                self.logger,
                scheme=self._reolink_scheme(),
            )
            self._isp_state = {}
        return self._isp

    async def change_video_settings(self, options) -> None:
        # Protect's "Turn off spotlight after no motion for" slider (5-900 s)
        slider = options.get("spotlightDuration")
        if getattr(self.args, "spotlight", False) and isinstance(slider, int):
            self._spotlight_slider_s = slider
        cnv = options.get("colorNightVision")
        if (
            getattr(self.args, "spotlight", False)
            and self.args.reolink_ai_host
            and cnv in (0, 1, True, False)
        ):
            # Protect's Color Night Vision toggle is the on/off for the camera's own
            # automatic spotlight (applied on every settings push, so Protect wins)
            await self._spotlight_client().set_auto_mode(bool(cnv))
        if not (getattr(self.args, "reolink_isp", False) and self.args.reolink_ai_host):
            return
        client = self._reolink_client()
        self._isp_state.update(options)
        # Reply to Protect now; the camera writes can take seconds.
        self._isp_task = asyncio.create_task(client.apply(dict(options)))

    async def reboot_camera(self) -> None:
        """Protect's "restart camera": reboot the Reolink too, if enabled."""
        if not (
            getattr(self.args, "reolink_reboot", False) and self.args.reolink_ai_host
        ):
            return
        now = time.monotonic()
        last = getattr(self, "_last_reboot", None)
        if last is not None and now - last < self.REBOOT_COOLDOWN_SEC:
            self.logger.info("Ignoring repeated reboot request from Protect")
            return
        self._last_reboot = now
        # Reply now; the camera takes a moment to answer and then goes away.
        self._reboot_task = asyncio.create_task(self._reolink_client().reboot())

    def _reolink_ptz_client(self) -> ReolinkPtz:
        if not hasattr(self, "_ptz"):
            self._ptz = ReolinkPtz(
                self.args.reolink_ai_host,
                self.args.reolink_ai_user,
                self.args.reolink_ai_password,
                self.logger,
                scheme=self._reolink_scheme(),
            )
        return self._ptz

    # Protect's reserved Home View preset slot (PTZ_PRESET_HOME_SLOT in its backend).
    PROTECT_HOME_SLOT = -1

    def _ptz_enabled(self) -> bool:
        return bool(getattr(self.args, "ptz", False) and self.args.reolink_ai_host)

    async def ptz_position(self) -> Optional[dict]:
        # Reolink has no absolute-position readback, but Protect creates its home
        # preset at connect from this reply and crashes ("reading 'pan'") without
        # steps. Report a fixed origin; the home view is the camera's own preset.
        if not self._ptz_enabled():
            return None
        origin = {"pan": 0, "tilt": 0, "zoom": 0}
        return {
            "inDegree": True,
            "inSteps": True,
            "degrees": dict(origin),
            "steps": {**origin, "focus": 0},
        }

    async def ptz_preset(self, payload: dict) -> None:
        if not self._ptz_enabled():
            return
        action = str(payload.get("action", "")).lower()
        item = payload.get("item") or (payload.get("items") or [{}])[0] or {}
        index = payload.get("index", item.get("index"))
        if payload.get("function") == "Home" or (
            action == "go" and index == self.PROTECT_HOME_SLOT
        ):
            await self._reolink_ptz_client().goto_preset(self.args.ptz_home_preset)
        elif action == "go":
            self.logger.info(f"Ignoring goto of Protect preset {index}: not mapped")

    def _spotlight_client(self) -> ReolinkLight:
        light = getattr(self, "_light", None)
        if light is None:
            light = self._light = ReolinkLight(
                self.args.reolink_ai_host,
                self.args.reolink_ai_user,
                self.args.reolink_ai_password,
                self.logger,
                scheme=self._reolink_scheme(),
            )
        return light

    async def spotlight(self, duration_s: int) -> None:
        if not (getattr(self.args, "spotlight", False) and self.args.reolink_ai_host):
            return
        light = self._spotlight_client()
        seconds = duration_s
        if seconds <= 0:  # no duration in the request: Protect's slider, then the flag
            seconds = getattr(self, "_spotlight_slider_s", 0) or (
                self.args.spotlight_default_seconds
            )
        prev = getattr(self, "_light_task", None)
        if prev is not None:
            # A newer request replaces the pending switch-off; wait for the old one to
            # finish its own "off" so it cannot land after the new "on".
            prev.cancel()
            await asyncio.gather(prev, return_exceptions=True)
        self._light_task = asyncio.create_task(self._spotlight_for(light, seconds))

    async def _spotlight_for(self, light: ReolinkLight, seconds: int) -> None:
        try:
            await light.set_spotlight(True, self.args.spotlight_brightness)
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        finally:
            # Also on cancel (replaced or shutting down): never leave the light on
            await asyncio.shield(light.set_spotlight(False))

    async def continuous_move(self, x: int, y: int, z: int) -> None:
        if not (getattr(self.args, "ptz", False) and self.args.reolink_ai_host):
            return
        # Protect sends a ContinuousMove tick faster than an HTTP round trip to the
        # camera, so cancelling and replacing the in-flight request (the first cut of
        # this) meant no request ever survived long enough to reach the camera. Instead,
        # remember only the latest tick and let one worker drain it: finish the request
        # that's in flight, then immediately send whatever is now the latest tick
        # (including a release's all-zero Stop) rather than waiting for another message.
        self._ptz_pending = (x, y, z)
        prev = getattr(self, "_ptz_task", None)
        if prev is None or prev.done():
            self._ptz_task = asyncio.create_task(self._drain_ptz_moves())

    async def _drain_ptz_moves(self) -> None:
        client = self._reolink_ptz_client()
        while True:
            move = self._ptz_pending
            await client.continuous_move(*move)
            if self._ptz_pending == move:
                return

    async def _push_reolink(self) -> None:
        from reolink_aio.api import Host

        # Never gives up: retries with a doubling cool-off (min..max), reset only after
        # the connection has stayed up for `stable` seconds so a flapping camera is not
        # hammered. Knobs are attributes so tests can shrink them.
        min_delay = getattr(self, "_push_min_delay", 2.0)
        max_delay = getattr(self, "_push_max_delay", 120.0)
        stable = getattr(self, "_push_stable_after", 300.0)
        poll = getattr(self, "_push_watch_interval", 30.0)
        lost_limit = getattr(self, "_push_lost_limit", 120.0)
        loop = asyncio.get_running_loop()
        delay = min_delay
        while True:
            host = None
            connected_at = None
            reason = "connection ended"
            try:
                host = Host(
                    self.args.reolink_ai_host,
                    self.args.reolink_ai_user,
                    self.args.reolink_ai_password,
                    bc_only=True,
                )
                state = {"pressed": False}
                if self.args.reolink_push_ai:
                    # The library only tracks AI states for keys that already exist
                    # (normally created by its HTTP startup, which we skip on purpose).
                    # Pinned to reolink-aio 0.21.18: revisit if it is upgraded.
                    host._ai_detection_states[0] = {
                        "people": False,
                        "vehicle": False,
                        "dog_cat": False,
                    }

                def on_event(host=host, state=state) -> None:
                    if self.args.reolink_push_button:
                        now_pressed = bool(host.visitor_detected(0))
                        if now_pressed and not state["pressed"]:
                            loop.call_soon_threadsafe(
                                lambda: asyncio.ensure_future(self.trigger_ring())
                            )
                        state["pressed"] = now_pressed
                    if self.args.reolink_push_ai:
                        states = {
                            k: bool(host.ai_detected(0, k))
                            for k in ("people", "vehicle", "dog_cat")
                        }
                        loop.call_soon_threadsafe(
                            lambda: asyncio.ensure_future(self._apply_ai_states(states))
                        )

                await host.baichuan.login()
                host.baichuan.register_callback(
                    "unifi-cam-proxy", on_event, cmd_id=33, channel=0
                )
                await host.baichuan.subscribe_events()
                connected_at = time.time()
                self.logger.info(
                    f"Baichuan push connected to {self.args.reolink_ai_host}"
                    f" (button={self.args.reolink_push_button},"
                    f" ai={self.args.reolink_push_ai})"
                )
                lost_since = None
                while True:
                    await asyncio.sleep(poll)
                    await self._expire_stale_ai()
                    if host.baichuan.session_active:
                        lost_since = None
                    else:
                        lost_since = lost_since or time.time()
                        if time.time() - lost_since > lost_limit:
                            raise RuntimeError(f"session lost for >{lost_limit:.0f}s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = repr(e)
            finally:
                try:
                    if host is not None:
                        await host.baichuan.logout()
                except Exception:
                    pass
            await self._apply_ai_states({}, force_clear=True)
            if connected_at is not None and time.time() - connected_at >= stable:
                delay = min_delay
            self.logger.warning(
                f"Baichuan push down ({reason}); retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    async def _apply_ai_states(self, states: dict, force_clear: bool = False) -> None:
        # Every push event runs this in its own task, and starting an event awaits a
        # camera snapshot and a crop. Without the lock a "clear" arriving during that
        # wait saw no active detection and was dropped, leaving the event open until
        # the 90 s expiry. Serialised, events apply in arrival order.
        lock = self.__dict__.setdefault("_ai_states_lock", asyncio.Lock())
        async with lock:
            await self._apply_ai_states_locked(states, force_clear)

    async def _apply_ai_states_locked(
        self, states: dict, force_clear: bool = False
    ) -> None:
        # Same decision as _poll_reolink_ai, fed by Baichuan push instead of GetAiState.
        classes = [
            ("people", SmartDetectObjectType.PERSON),
            ("vehicle", SmartDetectObjectType.VEHICLE),
            ("dog_cat", SmartDetectObjectType.ANIMAL),
        ]
        now = time.time()
        self._ai_last_event_ts = now
        desired = None
        if not force_clear:
            for key, object_type in classes:
                if (
                    self.smart_detect_enabled is not None
                    and object_type.value not in self.smart_detect_enabled
                ):
                    continue
                if states.get(key):
                    self._ai_class_state[object_type.value] = now
                    if desired is None:
                        desired = object_type
        if time.time() - self._sink_last_ts < 10 and not self._enrich_only():
            self._ai_active = None  # the box sidecar drives events while it is alive
        elif desired and self._ai_active is None:
            self.logger.info(f"Reolink AI (push): {desired.value} detected")
            self._ai_crops_made = set()  # per-event crop bookkeeping starts fresh
            await self.trigger_motion_start(desired)
            self._ai_active = desired
            # Push events carry no coordinates, so crop the same centred box
            # trigger_motion_start announced. Without a per-object crop ds
            # stores no detectedThumbnails and Protect's Person/Animal/Vehicle
            # filters never list the event.
            await self._make_object_crop(
                self.synthetic_tracker_id(), desired.value, self.SYNTHETIC_BOX
            )
        elif desired is None and self._ai_active is not None:
            self.logger.info("Reolink AI (push): clear")
            await self.trigger_motion_stop()
            self._ai_active = None

    async def _expire_stale_ai(self) -> None:
        # Safety net: if the push goes quiet mid-event, never leave motion stuck on.
        if self._ai_active is not None and time.time() - self._ai_last_event_ts > 90:
            self.logger.warning("Reolink AI (push): no events for 90s, clearing")
            await self._apply_ai_states({}, force_clear=True)

    def _reolink_api_url(self, cmd: str) -> str:
        # Credentials are percent-encoded: a password containing &, #, + or %
        # otherwise corrupts the query string and every poll fails to log in.
        query = urllib.parse.urlencode(
            {
                "cmd": cmd,
                "channel": 0,
                "user": self.args.reolink_ai_user,
                "password": self.args.reolink_ai_password,
            }
        )
        host = self.args.reolink_ai_host
        return f"{self._reolink_scheme()}://{host}/cgi-bin/api.cgi?{query}"

    async def _poll_reolink_visitor(self) -> None:
        url = self._reolink_api_url("GetEvents")
        self.logger.info(
            f"Polling Reolink doorbell button on {self.args.reolink_ai_host}"
            f" every {self.args.reolink_visitor_interval}s"
        )
        pressed = False
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(
                        url, ssl=False, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json(content_type=None)
                    entry = (data[0].get("value", {}) or {}).get("visitor") or {}
                    now_pressed = bool(entry.get("alarm_state"))
                    if now_pressed and not pressed:
                        await self.trigger_ring()
                    pressed = now_pressed
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.warning(
                        f"Reolink visitor poll failed: {self.redact_secrets(str(e))}"
                    )
                await asyncio.sleep(self.args.reolink_visitor_interval)

    async def _poll_reolink_ai(self) -> None:
        url = self._reolink_api_url("GetAiState")
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
                        if (
                            self.smart_detect_enabled is not None
                            and object_type.value not in self.smart_detect_enabled
                        ):
                            continue
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
                    self.logger.warning(
                        f"Reolink AI poll failed: {self.redact_secrets(str(e))}"
                    )
                await asyncio.sleep(self.args.reolink_ai_interval)

    async def close(self) -> None:
        await super().close()
        if self.runner:
            await self.runner.cleanup()
        if self.sink_runner:
            await self.sink_runner.cleanup()
        sidecar = getattr(self, "_sidecar_task", None)
        if sidecar is not None:
            sidecar.cancel()  # the supervisor terminates the node process
        ptz = getattr(self, "_ptz", None)
        if ptz is not None:
            await ptz.close()  # reopened lazily if this instance is reused
        light_task = getattr(self, "_light_task", None)
        if light_task is not None:
            light_task.cancel()
            await asyncio.gather(light_task, return_exceptions=True)  # light off first
        light = getattr(self, "_light", None)
        if light is not None:
            await light.close()
            self._light = None

        if self.snapshot_stream:
            self.snapshot_stream.kill()

        # Empty (not remove) the scratch dir: Core reuses this instance
        # across websocket reconnects and run() writes here again.
        for path in Path(self.snapshot_dir).iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)

    async def get_stream_source(self, stream_index: str) -> str:
        return self.stream_source[stream_index]
