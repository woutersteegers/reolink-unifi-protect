import argparse
import json
import logging
import tempfile
from pathlib import Path

import aiohttp
import reolinkapi
from yarl import URL

from unifi.cams.base import UnifiCamBase


class Reolink(UnifiCamBase):
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        super().__init__(args, logger)
        self.snapshot_dir: str = tempfile.mkdtemp()
        self.motion_in_progress: bool = False
        self.substream = args.substream
        self.cam = reolinkapi.Camera(
            ip=args.ip,
            username=args.username,
            password=args.password,
        )
        self.stream_fps = self.get_stream_info(self.cam)

    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        super().add_parser(parser)
        parser.add_argument("--username", "-u", required=True, help="Camera username")
        parser.add_argument("--password", "-p", required=True, help="Camera password")
        parser.add_argument(
            "--channel",
            "-c",
            default=0,
            help="Camera channel (not needed, leaving for possible future)",
        )

        parser.add_argument(
            "--stream",
            "-m",
            default="main",
            type=str,
            choices=["main", "sub"],
            help="Stream profile to use for the higher quality stream",
        )

        parser.add_argument(
            "--substream",
            "-s",
            default="sub",
            type=str,
            choices=["main", "sub"],
            help="Stream profile to use for the lower quality stream",
        )

        # --- House customization: Intel VAAPI hardware transcode ---
        # The Reolink Elite Floodlight's main stream is H.265 (HEVC), which
        # UniFi Protect will not accept from the proxy (it wants H.264). These
        # flags let us hardware-transcode the high-quality stream H.265->H.264
        # on the Intel iGPU (both decode and encode stay on the GPU), instead of
        # the stock `-c:v copy` that only works for an already-H.264 source.
        parser.add_argument(
            "--hw-transcode",
            action="store_true",
            help="Hardware-transcode the high-quality stream to H.264 via Intel VAAPI",
        )
        parser.add_argument(
            "--hw-device",
            default="/dev/dri/renderD128",
            help="VAAPI render node used for --hw-transcode",
        )
        parser.add_argument(
            "--transcode-width",
            default=0,
            type=int,
            help="If >0, scale the transcoded stream to this width (aspect kept); "
            "helps Protect accept the camera's non-standard panoramic resolution",
        )
        parser.add_argument(
            "--transcode-bitrate",
            default="6M",
            help="Target video bitrate for the hardware H.264 encode",
        )

    def get_stream_info(self, camera) -> tuple[int, int]:
        info = camera.get_recording_encoding()
        return (
            info[0]["value"]["Enc"]["mainStream"]["frameRate"],
            info[0]["value"]["Enc"]["subStream"]["frameRate"],
        )

    async def get_snapshot(self) -> Path:
        img_file = Path(self.snapshot_dir, "screen.jpg")
        url = (
            f"http://{self.args.ip}"
            f"/cgi-bin/api.cgi?cmd=Snap&channel={self.args.channel}"
            f"&rs=6PHVjvf0UntSLbyT&user={self.args.username}"
            f"&password={self.args.password}"
        )
        self.logger.info(f"Grabbing snapshot: {url}")
        await self.fetch_to_file(url, img_file)
        return img_file

    async def run(self) -> None:
        url = (
            f"http://{self.args.ip}"
            f"/api.cgi?cmd=GetMdState&user={self.args.username}"
            f"&password={self.args.password}"
        )
        encoded_url = URL(url, encoded=True)

        body = (
            f'[{{ "cmd":"GetMdState", "param":{{ "channel":{self.args.channel} }} }}]'
        )
        while True:
            self.logger.info(f"Connecting to motion events API: {url}")
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(None)
                ) as session:
                    while True:
                        async with session.post(encoded_url, data=body) as resp:
                            data = await resp.read()

                            try:
                                json_body = json.loads(data)
                                if "value" in json_body[0]:
                                    if json_body[0]["value"]["state"] == 1:
                                        if not self.motion_in_progress:
                                            self.motion_in_progress = True
                                            self.logger.info("Trigger motion start")
                                            await self.trigger_motion_start()
                                    elif json_body[0]["value"]["state"] == 0:
                                        if self.motion_in_progress:
                                            self.motion_in_progress = False
                                            self.logger.info("Trigger motion end")
                                            await self.trigger_motion_stop()
                                else:
                                    self.logger.error(
                                        "Motion API request responded with "
                                        "unexpected JSON, retrying. "
                                        f"JSON: {data}"
                                    )

                            except json.JSONDecodeError as err:
                                self.logger.error(
                                    "Motion API request returned invalid "
                                    "JSON, retrying. "
                                    f"Error: {err}, "
                                    f"Response: {data}"
                                )

            except aiohttp.ClientError as err:
                self.logger.error(f"Motion API request failed, retrying. Error: {err}")

    def _transcoding(self, stream_index: str) -> bool:
        # Only the high-quality stream (video1) is transcoded; the substream is
        # already H.264 and is copied untouched.
        return bool(getattr(self.args, "hw_transcode", False)) and stream_index == "video1"

    def get_base_ffmpeg_args(self, stream_index: str = "") -> str:
        base = super().get_base_ffmpeg_args(stream_index)
        # Hardware-DECODE goes in the pre-input slot. -hwaccel_output_format
        # vaapi keeps the decoded frames as GPU surfaces so the encoder below
        # never round-trips them through system memory.
        if self._transcoding(stream_index):
            base = (
                f"-hwaccel vaapi -hwaccel_device {self.args.hw_device}"
                f" -hwaccel_output_format vaapi {base}"
            )
        return base

    def get_extra_ffmpeg_args(self, stream_index: str) -> str:
        if stream_index == "video1":
            fps = self.stream_fps[0]
        else:
            fps = self.stream_fps[1]

        # Hardware-ENCODE (H.264) in the post-input slot. scale_vaapi runs on the
        # GPU surfaces from decode; format=nv12 hands the encoder what it wants,
        # and an optional width constrains the camera's odd panoramic size to
        # something Protect will adopt.
        if self._transcoding(stream_index):
            scale = (
                f"scale_vaapi=w={self.args.transcode_width}:h=-2:format=nv12"
                if self.args.transcode_width
                else "scale_vaapi=format=nv12"
            )
            return (
                f'-vf "{scale}" -c:v h264_vaapi -b:v {self.args.transcode_bitrate}'
                f' -bsf:v "h264_metadata=tick_rate={fps*2}"'
                " -ar 32000 -ac 1 -codec:a aac -b:a 32k"
            )

        return (
            "-ar 32000 -ac 1 -codec:a aac -b:a 32k -c:v copy -vbsf"
            f' "h264_metadata=tick_rate={fps*2}"'
        )

    async def get_stream_source(self, stream_index: str) -> str:
        if stream_index == "video1":
            stream = self.args.stream
        else:
            stream = self.args.substream

        return (
            f"rtsp://{self.args.username}:{self.args.password}@{self.args.ip}:554"
            f"//h264Preview_{int(self.args.channel) + 1:02}_{stream}"
        )
