import argparse
import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from unifi.cams.base import SmartDetectObjectType, UnifiCamBase


class RTSPCam(UnifiCamBase):
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        super().__init__(args, logger)
        self.args = args
        self.event_id = 0
        self.snapshot_dir = tempfile.mkdtemp()
        self.snapshot_stream = None
        self.runner = None
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

        if self.args.reolink_ai_host:
            await self._poll_reolink_ai()

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
                    for key, object_type in classes:
                        entry = value.get(key) or {}
                        if entry.get("support") and entry.get("alarm_state"):
                            desired = object_type
                            break
                    if desired and active is None:
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

        if self.snapshot_stream:
            self.snapshot_stream.kill()

    async def get_stream_source(self, stream_index: str) -> str:
        return self.stream_source[stream_index]
