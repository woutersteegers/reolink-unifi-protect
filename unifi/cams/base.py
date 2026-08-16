import argparse
import atexit
import json
import logging
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib
from abc import ABCMeta, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiohttp
import packaging
import websockets

from unifi.core import RetryableError

AVClientRequest = AVClientResponse = dict[str, Any]


class SmartDetectObjectType(Enum):
    PERSON = "person"
    VEHICLE = "vehicle"


class UnifiCamBase(metaclass=ABCMeta):
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        self.args = args
        self.logger = logger

        self._msg_id: int = 0
        self._init_time: float = time.time()
        self._streams: dict[str, str] = {}
        self._motion_snapshot: Optional[Path] = None
        self._motion_event_id: int = 0
        self._motion_event_ts: Optional[float] = None
        self._motion_object_type: Optional[SmartDetectObjectType] = None
        self._ffmpeg_handles: dict[str, subprocess.Popen] = {}

        # Set up ssl context for requests
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        self._ssl_context.load_cert_chain(args.cert, args.cert)
        self._session: Optional[websockets.legacy.client.WebSocketClientProtocol] = None
        atexit.register(self.close_streams)

        self._needs_flv_timestamps: bool = False

    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--ffmpeg-args",
            "-f",
            default="-c:v copy -ar 32000 -ac 1 -codec:a aac -b:a 32k",
            help="Transcoding args for `ffmpeg -i <src> <args> <dst>`",
        )
        parser.add_argument(
            "--rtsp-transport",
            default="tcp",
            choices=["tcp", "udp", "http", "udp_multicast"],
            help="RTSP transport protocol used by stream",
        )
        # --- House: Intel QSV hardware transcode (any source) ---
        # UniFi Protect cannot play a proxied H.265 live stream (the snapshot
        # renders but live view spins forever), so the camera's HEVC main stream
        # must reach Protect as H.264. Measured on this NAS (i5-1235U Iris Xe):
        # HEVC decode 1.12x, but 3840-wide H.264 encode only 0.66x — the encode
        # block is the limit, not the API (VAAPI and QSV performed the same).
        # 2560x776 sustains 1.02x, so scale down as part of the transcode.
        # QSV over VAAPI: VAAPI failed with "Cannot allocate memory" at every
        # size here; QSV via vpp_qsv is stable. (scale_qsv does NOT work — it
        # fails to configure its input pad; vpp_qsv is the working scaler.)
        parser.add_argument(
            "--hw-transcode",
            action="store_true",
            help="Hardware-transcode video to H.264 via Intel QSV (iGPU)",
        )
        parser.add_argument(
            "--hw-device",
            default="/dev/dri/renderD128",
            help="Intel render node for --hw-transcode",
        )
        parser.add_argument(
            "--transcode-width",
            default=2560,
            type=int,
            help="Output width for --hw-transcode (height from --transcode-height)",
        )
        parser.add_argument(
            "--transcode-height",
            default=776,
            type=int,
            help="Output height for --hw-transcode (vpp_qsv needs both explicitly)",
        )
        parser.add_argument(
            "--transcode-bitrate",
            default="6M",
            help="Target bitrate for the hardware H.264 encode",
        )
        # --- House: honest per-channel codec + resolution advertisement ---
        # Upstream hardcodes every channel as "h264" at 1080p. If the hi stream
        # actually carries HEVC, Protect is told h264, tries to decode it as
        # h264, and live view spins forever (the snapshot still works because
        # ffmpeg renders that separately). Declaring the truth per channel lets
        # the hi stream be H.265 passthrough while the low streams stay H.264 —
        # no transcoding anywhere. Runtime flags so this is tunable without a
        # rebuild.
        parser.add_argument(
            "--hi-codec",
            default="h264",
            choices=["h264", "h265"],
            help="Codec ACTUALLY carried by the high-quality stream (video1)",
        )
        parser.add_argument("--hi-width", default=1920, type=int)
        parser.add_argument("--hi-height", default=1080, type=int)
        parser.add_argument("--hi-fps", default=15, type=int)
        parser.add_argument(
            "--lo-codec",
            default="h264",
            choices=["h264", "h265"],
            help="Codec actually carried by the medium/low streams (video2/3)",
        )
        parser.add_argument("--lo-width", default=1280, type=int)
        parser.add_argument("--lo-height", default=720, type=int)
        parser.add_argument("--lo-fps", default=15, type=int)

    async def _run(self, ws) -> None:
        self._session = ws
        await self.init_adoption()
        while True:
            try:
                msg = await ws.recv()
            except websockets.exceptions.ConnectionClosedError:
                self.logger.info(f"Connection to {self.args.host} was closed.")
                raise RetryableError()

            if msg is not None:
                force_reconnect = await self.process(msg)
                if force_reconnect:
                    self.logger.info("Reconnecting...")
                    raise RetryableError()

    async def run(self) -> None:
        return

    async def get_video_settings(self) -> dict[str, Any]:
        return {}

    async def change_video_settings(self, options) -> None:
        return

    @abstractmethod
    async def get_snapshot(self) -> Path:
        raise NotImplementedError("You need to write this!")

    @abstractmethod
    async def get_stream_source(self, stream_index: str) -> str:
        raise NotImplementedError("You need to write this!")

    @staticmethod
    def redact_secrets(text: str) -> str:
        # `docker logs` is shared freely; stream URLs carry the camera
        # password (rtsp://user:pass@host), so scrub credentials before any
        # command line or URL is logged.
        return re.sub(r"//[^/@\s\"]+@", "//***:***@", text)

    def _hw_transcoding(self, stream_index: str = "") -> bool:
        # Only the HIGH-quality stream is transcoded. Protect opens several
        # channels at once, and this iGPU fits exactly ONE 2560-wide transcode
        # (1.02x); transcoding two starves the encoder and both respawn-loop.
        # The other channels are served from an already-H.264 source instead.
        if not getattr(self.args, "hw_transcode", False):
            return False
        return stream_index in ("", "video1")

    def get_extra_ffmpeg_args(self, stream_index: str = "") -> str:
        # Post-input (encode) slot: scale + encode H.264, both on the iGPU.
        if self._hw_transcoding(stream_index):
            return (
                f'-vf "vpp_qsv=w={self.args.transcode_width}'
                f':h={self.args.transcode_height}"'
                f" -c:v h264_qsv -b:v {self.args.transcode_bitrate}"
                " -ar 32000 -ac 1 -codec:a aac -b:a 32k"
            )
        return self.args.ffmpeg_args

    async def get_feature_flags(self) -> dict[str, Any]:
        # Protect gates codec support on the CAPABILITIES the camera reports,
        # not on the per-channel "type" in ChangeVideoSettings: the bootstrap
        # keeps a camera-level videoCodec (default h264) that governs decode,
        # and the "enhanced encoding" (H.265) option only exists when the
        # camera's featureFlags.videoCodecs contains "h265" (observed empty
        # for this proxy vs ["h264","h265",...] on real G6/Doorbell cams).
        # Protect renames feature keys on ingest (videoMode->videoModes,
        # motionDetect->motionAlgorithms), so the camera-side key for the
        # codec list is uncertain — send singular and plural forms; unknown
        # keys are ignored.
        # Vocabulary matched to a real G5's measured hello (rjmotion/finch):
        # plural key names, mic as an int, capability booleans alongside.
        codecs = ["h264"]
        if getattr(self.args, "hi_codec", "h264") == "h265":
            codecs.append("h265")
        return {
            "mic": 1,
            "aec": [],
            "videoMode": ["default"],
            "motionDetect": ["enhanced"],
            "smartDetect": [],
            "videoCodecs": codecs,
            "audioCodecs": ["aac"],
            "audioStyle": ["nature"],
            "hasHdr": False,
            "hasWdr": True,
            "hasMic": True,
            "hasSpeaker": False,
            "hasInfrared": False,
            "hasMotionZones": True,
            "hasPrivacyMask": False,
            "isPtz": False,
        }

    # API for subclasses
    async def trigger_motion_start(
        self, object_type: Optional[SmartDetectObjectType] = None
    ) -> None:
        if not self._motion_event_ts:
            payload: dict[str, Any] = {
                "clockBestMonotonic": 0,
                "clockBestWall": 0,
                "clockMonotonic": int(self.get_uptime()),
                "clockStream": int(self.get_uptime()),
                "clockStreamRate": 1000,
                "clockWall": int(round(time.time() * 1000)),
                "edgeType": "start",
                "eventId": self._motion_event_id,
                "eventType": "motion",
                "levels": {"0": 47},
                "motionHeatmap": "",
                "motionSnapshot": "",
            }
            if object_type:
                payload.update(
                    {
                        "objectTypes": [object_type.value],
                        "edgeType": "enter",
                        "zonesStatus": {"0": 48},
                        "smartDetectSnapshot": "",
                    }
                )

            self.logger.info(
                f"Triggering motion start (idx: {self._motion_event_id})"
                + f" for {object_type.value}"
                if object_type
                else ""
            )
            await self.send(
                self.gen_response(
                    "EventSmartDetect" if object_type else "EventAnalytics",
                    payload=payload,
                ),
            )
            self._motion_event_ts = time.time()
            self._motion_object_type = object_type

            # Capture snapshot at beginning of motion event for thumbnail
            motion_snapshot_path: str = tempfile.NamedTemporaryFile(delete=False).name
            try:
                shutil.copyfile(await self.get_snapshot(), motion_snapshot_path)
                self.logger.debug(f"Captured motion snapshot to {motion_snapshot_path}")
                self._motion_snapshot = Path(motion_snapshot_path)
            except FileNotFoundError:
                pass

    async def trigger_motion_stop(self) -> None:
        motion_start_ts = self._motion_event_ts
        motion_object_type = self._motion_object_type
        if motion_start_ts:
            payload: dict[str, Any] = {
                "clockBestMonotonic": int(self.get_uptime()),
                "clockBestWall": int(round(motion_start_ts * 1000)),
                "clockMonotonic": int(self.get_uptime()),
                "clockStream": int(self.get_uptime()),
                "clockStreamRate": 1000,
                "clockWall": int(round(time.time() * 1000)),
                "edgeType": "stop",
                "eventId": self._motion_event_id,
                "eventType": "motion",
                "levels": {"0": 49},
                "motionHeatmap": "heatmap.png",
                "motionSnapshot": "motionsnap.jpg",
            }
            if motion_object_type:
                payload.update(
                    {
                        "objectTypes": [motion_object_type.value],
                        "edgeType": "leave",
                        "zonesStatus": {"0": 48},
                        "smartDetectSnapshot": "motionsnap.jpg",
                    }
                )
            self.logger.info(
                f"Triggering motion stop (idx: {self._motion_event_id})"
                + f" for {motion_object_type.value}"
                if motion_object_type
                else ""
            )
            await self.send(
                self.gen_response(
                    "EventSmartDetect" if motion_object_type else "EventAnalytics",
                    payload=payload,
                ),
            )
            self._motion_event_id += 1
            self._motion_event_ts = None
            self._motion_object_type = None

    def update_motion_snapshot(self, path: Path) -> None:
        self._motion_snapshot = path

    async def fetch_to_file(self, url: str, dst: Path) -> bool:
        try:
            # ssl=False: snapshot URLs point at LAN cameras with self-signed
            # certs (this Reolink is HTTPS-only), which fail verification.
            async with aiohttp.request("GET", url, ssl=False) as resp:
                if resp.status != 200:
                    self.logger.error(f"Error retrieving file {resp.status}")
                    return False
                with dst.open("wb") as f:
                    f.write(await resp.read())
                    return True
        except aiohttp.ClientError:
            return False

    # Protocol implementation
    def gen_msg_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def init_adoption(self) -> None:
        self.logger.info(
            f"Adopting with token [{self.args.token[:4]}…] and mac [{self.args.mac}]"
        )
        payload: dict[str, Any] = {
            "adoptionCode": self.args.token,
            "connectionHost": self.args.host,
            "connectionSecurePort": 7442,
            "fwVersion": self.args.fw_version,
            "hwrev": 19,
            "idleTime": 191.96,
            "ip": self.args.ip,
            "mac": self.args.mac,
            "model": self.args.model,
            "name": self.args.name,
            "protocolVersion": 67,
            "rebootTimeoutSec": 30,
            "semver": "v4.4.8",
            "totalLoad": 0.5474,
            "upgradeTimeoutSec": 150,
            "uptime": int(self.get_uptime()),
            "features": await self.get_feature_flags(),
        }
        if getattr(self.args, "sysid", None):
            # Full identity block: Protect >=3.0 no longer copies `model`
            # from the hello (service.js dropped `type: o.model`), so model
            # recognition keys on sysid/platform/firmwareBuild plus the hex
            # camera-model WSS header (core.py). fwVersion must be the
            # SHORT form (e.g. "5.3.95") when this is used.
            payload.update(
                {
                    "sysid": int(self.args.sysid, 0),
                    "platform": self.args.platform,
                    "firmwareBuild": self.args.fw_build,
                    "semver": f"v{self.args.fw_version}",
                    "hwaddr": ":".join(
                        self.args.mac[i : i + 2] for i in range(0, 12, 2)
                    ).lower(),
                    "lensmodel": self.args.model,
                    "cameraName": self.args.name,
                    "isGen5s": True,
                    "isDoorbellSeries": False,
                }
            )
        await self.send(self.gen_response("ubnt_avclient_hello", payload=payload))

    async def process_hello(self, msg: AVClientRequest) -> None:
        controller_version = packaging.version.parse(
            msg["payload"].get("controllerVersion")
        )
        self._needs_flv_timestamps = controller_version >= packaging.version.parse(
            "1.21.4"
        )

    async def process_param_agreement(self, msg: AVClientRequest) -> AVClientResponse:
        return self.gen_response(
            "ubnt_avclient_paramAgreement",
            msg["messageId"],
            {
                "authToken": self.args.token,
                "features": await self.get_feature_flags(),
            },
        )

    async def process_upgrade(self, msg: AVClientRequest) -> None:
        url = msg["payload"]["uri"]
        headers = {"Range": "bytes=0-100"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as r:
                # Parse the new version string from the upgrade binary
                content = await r.content.readexactly(54)
                version = ""
                for i in range(0, 50):
                    b = content[4 + i]
                    if b != b"\x00":
                        version += chr(b)
                self.logger.debug(f"Pretending to upgrade to: {version}")
                self.args.fw_version = version

    async def process_isp_settings(self, msg: AVClientRequest) -> AVClientResponse:
        payload = {
            "aeMode": "auto",
            "aeTargetPercent": 50,
            "aggressiveAntiFlicker": 0,
            "brightness": 50,
            "contrast": 50,
            "criticalTmpOfProtect": 40,
            "darkAreaCompensateLevel": 0,
            "denoise": 50,
            "enable3dnr": 1,
            "enableMicroTmpProtect": 1,
            "enablePauseMotion": 0,
            "flip": 0,
            "focusMode": "ztrig",
            "focusPosition": 0,
            "forceFilterIrSwitchEvents": 0,
            "hue": 50,
            "icrLightSensorNightThd": 0,
            "icrSensitivity": 0,
            "irLedLevel": 215,
            "irLedMode": "auto",
            "irOnStsBrightness": 0,
            "irOnStsContrast": 0,
            "irOnStsDenoise": 0,
            "irOnStsHue": 0,
            "irOnStsSaturation": 0,
            "irOnStsSharpness": 0,
            "irOnStsWdr": 0,
            "irOnValBrightness": 50,
            "irOnValContrast": 50,
            "irOnValDenoise": 50,
            "irOnValHue": 50,
            "irOnValSaturation": 50,
            "irOnValSharpness": 50,
            "irOnValWdr": 1,
            "mirror": 0,
            "queryIrLedStatus": 0,
            "saturation": 50,
            "sharpness": 50,
            "touchFocusX": 1001,
            "touchFocusY": 1001,
            "wdr": 1,
            "zoomPosition": 0,
        }
        payload.update(await self.get_video_settings())
        return self.gen_response(
            "ResetIspSettings",
            msg["messageId"],
            payload,
        )

    async def process_video_settings(self, msg: AVClientRequest) -> AVClientResponse:
        vid_dst = {
            "video1": ["file:///dev/null"],
            "video2": ["file:///dev/null"],
            "video3": ["file:///dev/null"],
        }

        if msg["payload"] is not None and "video" in msg["payload"]:
            for k, v in msg["payload"]["video"].items():
                if v:
                    if "avSerializer" in v:
                        vid_dst[k] = v["avSerializer"]["destinations"]
                        if "/dev/null" in vid_dst[k]:
                            self.stop_video_stream(k)
                        elif "parameters" in v["avSerializer"]:
                            self._streams[k] = stream = v["avSerializer"]["parameters"][
                                "streamName"
                            ]
                            try:
                                host, port = urllib.parse.urlparse(
                                    v["avSerializer"]["destinations"][0]
                                ).netloc.split(":")
                                await self.start_video_stream(
                                    k, stream, destination=(host, int(port))
                                )
                            except ValueError:
                                pass

        return self.gen_response(
            "ChangeVideoSettings",
            msg["messageId"],
            {
                "audio": {
                    "bitRate": 32000,
                    "channels": 1,
                    "description": "audio track",
                    "enableTemporalNoiseShaping": False,
                    "enabled": True,
                    "mode": 0,
                    "quality": 0,
                    # Match what ffmpeg actually sends (-ar 32000); volume 0
                    # made Protect store micVolume=0 / mic disabled.
                    "sampleRate": 32000,
                    "type": "aac",
                    "volume": 100,
                },
                "firmwarePath": "/lib/firmware/",
                "video": {
                    "enableHrd": False,
                    "hdrMode": 0,
                    "lowDelay": False,
                    "videoMode": "default",
                    "videoCodec": self.args.hi_codec,
                    "mjpg": {
                        "avSerializer": {
                            "destinations": [
                                "file:///tmp/snap.jpeg",
                                "file:///tmp/snap_av.jpg",
                            ],
                            "parameters": {
                                "audioId": 1000,
                                "enableTimestampsOverlapAvoidance": False,
                                "suppressAudio": True,
                                "suppressVideo": False,
                                "videoId": 1001,
                            },
                            "type": "mjpg",
                        },
                        "bitRateCbrAvg": 500000,
                        "bitRateVbrMax": 500000,
                        "bitRateVbrMin": None,
                        "description": "JPEG pictures",
                        "enabled": True,
                        "fps": 5,
                        "height": 720,
                        "isCbr": False,
                        "maxFps": 5,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": None,
                        "name": "mjpg",
                        "quality": 80,
                        "sourceId": 3,
                        "streamId": 8,
                        "streamOrdinal": 3,
                        "type": "mjpg",
                        "validBitrateRangeMax": 6000000,
                        "validBitrateRangeMin": 32000,
                        "width": 1280,
                    },
                    "video1": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video1"],
                            "parameters": None
                            if "video1" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video1"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 6000000,
                        "bitRateVbrMax": 12000000,
                        "bitRateVbrMin": 48000,
                        "description": "Hi quality video track",
                        "enabled": True,
                        "fps": self.args.hi_fps,
                        "gopModel": 0,
                        "height": self.args.hi_height,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video1",
                        "sourceId": 0,
                        "streamId": 1,
                        "streamOrdinal": 0,
                        "type": self.args.hi_codec,
                        "validBitrateRangeMax": 16000000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1,
                            2,
                            3,
                            4,
                            5,
                            6,
                            8,
                            9,
                            10,
                            12,
                            15,
                            16,
                            18,
                            20,
                            24,
                            25,
                            30,
                        ],
                        "verticalFlip": False,
                        "width": self.args.hi_width,
                    },
                    "video2": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video2"],
                            "parameters": None
                            if "video2" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video2"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 500000,
                        "bitRateVbrMax": 1200000,
                        "bitRateVbrMin": 48000,
                        "currentVbrBitrate": 1200000,
                        "description": "Medium quality video track",
                        "enabled": True,
                        "fps": self.args.lo_fps,
                        "gopModel": 0,
                        "height": self.args.lo_height,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video2",
                        "sourceId": 1,
                        "streamId": 2,
                        "streamOrdinal": 1,
                        "type": self.args.lo_codec,
                        "validBitrateRangeMax": 1500000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1,
                            2,
                            3,
                            4,
                            5,
                            6,
                            8,
                            9,
                            10,
                            12,
                            15,
                            16,
                            18,
                            20,
                            24,
                            25,
                            30,
                        ],
                        "verticalFlip": False,
                        "width": self.args.lo_width,
                    },
                    "video3": {
                        "M": 1,
                        "N": 30,
                        "avSerializer": {
                            "destinations": vid_dst["video3"],
                            "parameters": None
                            if "video3" not in self._streams
                            else {
                                "audioId": None,
                                "streamName": self._streams["video3"],
                                "suppressAudio": None,
                                "suppressVideo": None,
                                "videoId": None,
                            },
                            "type": "extendedFlv",
                        },
                        "bitRateCbrAvg": 300000,
                        "bitRateVbrMax": 200000,
                        "bitRateVbrMin": 48000,
                        "currentVbrBitrate": 200000,
                        "description": "Low quality video track",
                        "enabled": True,
                        "fps": self.args.lo_fps,
                        "gopModel": 0,
                        "height": self.args.lo_height,
                        "horizontalFlip": False,
                        "isCbr": False,
                        "maxFps": 30,
                        "minClientAdaptiveBitRate": 0,
                        "minMotionAdaptiveBitRate": 0,
                        "nMultiplier": 6,
                        "name": "video3",
                        "sourceId": 2,
                        "streamId": 4,
                        "streamOrdinal": 2,
                        "type": self.args.lo_codec,
                        "validBitrateRangeMax": 750000,
                        "validBitrateRangeMin": 32000,
                        "validFpsValues": [
                            1,
                            2,
                            3,
                            4,
                            5,
                            6,
                            8,
                            9,
                            10,
                            12,
                            15,
                            16,
                            18,
                            20,
                            24,
                            25,
                            30,
                        ],
                        "verticalFlip": False,
                        "width": self.args.lo_width,
                    },
                    "vinFps": 30,
                },
            },
        )

    async def process_device_settings(self, msg: AVClientRequest) -> AVClientResponse:
        return self.gen_response(
            "ChangeDeviceSettings",
            msg["messageId"],
            {
                "name": self.args.name,
                "timezone": "PST8PDT,M3.2.0,M11.1.0",
            },
        )

    async def process_osd_settings(self, msg: AVClientRequest) -> AVClientResponse:
        return self.gen_response(
            "ChangeOsdSettings",
            msg["messageId"],
            {
                "_1": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_2": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_3": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "_4": {
                    "enableDate": 1,
                    "enableLogo": 1,
                    "enableReportdStatsLevel": 0,
                    "enableStreamerStatsLevel": 0,
                    "tag": self.args.name,
                },
                "enableOverlay": 1,
                "logoScale": 50,
                "overlayColorId": 0,
                "textScale": 50,
                "useCustomLogo": 0,
            },
        )

    async def process_network_status(self, msg: AVClientRequest) -> AVClientResponse:
        return self.gen_response(
            "NetworkStatus",
            msg["messageId"],
            {
                "connectionState": 2,
                "connectionStateDescription": "CONNECTED",
                "defaultInterface": "eth0",
                "dhcpLeasetime": 86400,
                "dnsServer": "8.8.8.8 4.2.2.2",
                "gateway": "192.168.103.1",
                "ipAddress": self.args.ip,
                "linkDuplex": 1,
                "linkSpeedMbps": 100,
                "mode": "dhcp",
                "networkMask": "255.255.255.0",
            },
        )

    async def process_sound_led_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        return self.gen_response(
            "ChangeSoundLedSettings",
            msg["messageId"],
            {
                "ledFaceAlwaysOnWhenManaged": 1,
                "ledFaceEnabled": 1,
                "speakerEnabled": 1,
                "speakerVolume": 100,
                "systemSoundsEnabled": 1,
                "userLedBlinkPeriodMs": 0,
                "userLedColorFg": "blue",
                "userLedOnNoff": 1,
            },
        )

    async def process_change_isp_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        payload = {
            "aeMode": "auto",
            "aeTargetPercent": 50,
            "aggressiveAntiFlicker": 0,
            "brightness": 50,
            "contrast": 50,
            "criticalTmpOfProtect": 40,
            "dZoomCenterX": 50,
            "dZoomCenterY": 50,
            "dZoomScale": 0,
            "dZoomStreamId": 4,
            "darkAreaCompensateLevel": 0,
            "denoise": 50,
            "enable3dnr": 1,
            "enableExternalIr": 0,
            "enableMicroTmpProtect": 1,
            "enablePauseMotion": 0,
            "flip": 0,
            "focusMode": "ztrig",
            "focusPosition": 0,
            "forceFilterIrSwitchEvents": 0,
            "hue": 50,
            "icrLightSensorNightThd": 0,
            "icrSensitivity": 0,
            "irLedLevel": 215,
            "irLedMode": "auto",
            "irOnStsBrightness": 0,
            "irOnStsContrast": 0,
            "irOnStsDenoise": 0,
            "irOnStsHue": 0,
            "irOnStsSaturation": 0,
            "irOnStsSharpness": 0,
            "irOnStsWdr": 0,
            "irOnValBrightness": 50,
            "irOnValContrast": 50,
            "irOnValDenoise": 50,
            "irOnValHue": 50,
            "irOnValSaturation": 50,
            "irOnValSharpness": 50,
            "irOnValWdr": 1,
            "lensDistortionCorrection": 1,
            "masks": None,
            "mirror": 0,
            "queryIrLedStatus": 0,
            "saturation": 50,
            "sharpness": 50,
            "touchFocusX": 1001,
            "touchFocusY": 1001,
            "wdr": 1,
            "zoomPosition": 0,
        }

        if msg["payload"]:
            await self.change_video_settings(msg["payload"])

        payload.update(await self.get_video_settings())
        return self.gen_response("ChangeIspSettings", msg["messageId"], payload)

    async def process_analytics_settings(
        self, msg: AVClientRequest
    ) -> AVClientResponse:
        return self.gen_response(
            "ChangeAnalyticsSettings", msg["messageId"], msg["payload"]
        )

    async def process_snapshot_request(
        self, msg: AVClientRequest
    ) -> Optional[AVClientResponse]:
        snapshot_type = msg["payload"]["what"]
        if snapshot_type in ["motionSnapshot", "smartDetectZoneSnapshot"]:
            path = self._motion_snapshot
        else:
            path = await self.get_snapshot()

        if path and path.exists():
            async with aiohttp.ClientSession() as session:
                files = {"payload": open(path, "rb")}
                files.update(msg["payload"].get("formFields", {}))
                try:
                    await session.post(
                        msg["payload"]["uri"],
                        data=files,
                        ssl=self._ssl_context,
                    )
                    self.logger.debug(f"Uploaded {snapshot_type} from {path}")
                except aiohttp.ClientError:
                    self.logger.exception("Failed to upload snapshot")
        else:
            self.logger.warning(
                f"Snapshot file {path} is not ready yet, skipping upload"
            )

        if msg["responseExpected"]:
            return self.gen_response("GetRequest", response_to=msg["messageId"])

    async def process_time(self, msg: AVClientRequest) -> AVClientResponse:
        return self.gen_response(
            "ubnt_avclient_paramAgreement",
            msg["messageId"],
            {
                "monotonicMs": self.get_uptime(),
                "wallMs": int(round(time.time() * 1000)),
                "features": {},
            },
        )

    def gen_response(
        self, name: str, response_to: int = 0, payload: Optional[dict[str, Any]] = None
    ) -> AVClientResponse:
        if not payload:
            payload = {}
        return {
            "from": "ubnt_avclient",
            "functionName": name,
            "inResponseTo": response_to,
            "messageId": self.gen_msg_id(),
            "payload": payload,
            "responseExpected": False,
            "to": "UniFiVideo",
        }

    def get_uptime(self) -> float:
        return time.time() - self._init_time

    async def send(self, msg: AVClientRequest) -> None:
        self.logger.debug(f"Sending: {msg}")
        ws = self._session
        if ws:
            await ws.send(json.dumps(msg).encode())

    async def process(self, msg: bytes) -> bool:
        m = json.loads(msg)
        fn = m["functionName"]

        self.logger.info(f"Processing [{fn}] message")
        self.logger.debug(f"Message contents: {m}")

        if (("responseExpected" not in m) or (m["responseExpected"] is False)) and (
            fn
            not in [
                "GetRequest",
                "ChangeVideoSettings",
                "UpdateFirmwareRequest",
                "Reboot",
                "ubnt_avclient_hello",
            ]
        ):
            return False

        res: Optional[AVClientResponse] = None

        if fn == "ubnt_avclient_time":
            res = await self.process_time(m)
        elif fn == "ubnt_avclient_hello":
            await self.process_hello(m)
        elif fn == "ubnt_avclient_paramAgreement":
            res = await self.process_param_agreement(m)
        elif fn == "ResetIspSettings":
            res = await self.process_isp_settings(m)
        elif fn == "ChangeVideoSettings":
            res = await self.process_video_settings(m)
        elif fn == "ChangeDeviceSettings":
            res = await self.process_device_settings(m)
        elif fn == "ChangeOsdSettings":
            res = await self.process_osd_settings(m)
        elif fn == "NetworkStatus":
            res = await self.process_network_status(m)
        elif fn == "AnalyticsTest":
            res = self.gen_response("AnalyticsTest", response_to=m["messageId"])
        elif fn == "ChangeSoundLedSettings":
            res = await self.process_sound_led_settings(m)
        elif fn == "ChangeIspSettings":
            res = await self.process_change_isp_settings(m)
        elif fn == "ChangeAnalyticsSettings":
            res = await self.process_analytics_settings(m)
        elif fn == "GetRequest":
            res = await self.process_snapshot_request(m)
        elif fn == "UpdateUsernamePassword":
            res = self.gen_response(
                "UpdateUsernamePassword", response_to=m["messageId"]
            )
        elif fn == "ChangeSmartDetectSettings":
            res = self.gen_response(
                "ChangeSmartDetectSettings", response_to=m["messageId"]
            )
        elif fn == "UpdateFirmwareRequest":
            await self.process_upgrade(m)
            return True
        elif fn == "Reboot":
            return True

        if res is not None:
            await self.send(res)

        return False

    def get_base_ffmpeg_args(self, stream_index: str = "") -> str:
        base_args = [
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts+discardcorrupt",
            "-use_wallclock_as_timestamps 1",
        ]

        try:
            output = subprocess.check_output(["ffmpeg", "-h", "full"])
            if b"stimeout" in output:
                base_args.append("-stimeout 15000000")
            else:
                base_args.append("-timeout 15000000")
        except subprocess.CalledProcessError:
            self.logger.exception("Could not check for ffmpeg options")

        # Pre-input (decode) slot: hardware-decode HEVC on the iGPU via QSV so
        # frames stay on the GPU for vpp_qsv + h264_qsv (no CPU round-trip).
        prefix = ""
        if self._hw_transcoding(stream_index):
            # NOTE: do NOT pin `-c:v hevc_qsv` here. The hi source is HEVC, but
            # if this stream ever carries H.264 (e.g. a sub-stream fallback),
            # forcing the HEVC decoder fails with "Function not implemented".
            # `-hwaccel qsv` alone lets ffmpeg pick the right QSV decoder.
            prefix = f"-hwaccel qsv -qsv_device {self.args.hw_device} "
        return prefix + " ".join(base_args)

    async def start_video_stream(
        self, stream_index: str, stream_name: str, destination: tuple[str, int]
    ):
        has_spawned = stream_index in self._ffmpeg_handles
        is_dead = has_spawned and self._ffmpeg_handles[stream_index].poll() is not None

        if not has_spawned or is_dead:
            source = await self.get_stream_source(stream_index)
            # ffmpeg muxes HEVC into FLV as Enhanced-RTMP hvc1 extended
            # tags, which Protect ingests but cannot decode; hevc_flv
            # rewrites them into UniFi's codec-id-8 framing (see module).
            hevc_filter = ""
            if (
                stream_index in ("", "video1")
                and getattr(self.args, "hi_codec", "h264") == "h265"
                and not self._hw_transcoding(stream_index)
            ):
                hevc_filter = f"{sys.executable} -m unifi.hevc_flv | "
            cmd = (
                "ffmpeg -nostdin -loglevel error -y"
                f" {self.get_base_ffmpeg_args(stream_index)} -rtsp_transport"
                f' {self.args.rtsp_transport} -i "{source}"'
                f" {self.get_extra_ffmpeg_args(stream_index)} -metadata"
                f" streamName={stream_name} -f flv - | {hevc_filter}{sys.executable} -m"
                " unifi.clock_sync"
                f" {'--write-timestamps' if self._needs_flv_timestamps else ''} | nc"
                f" {destination[0]} {destination[1]}"
            )

            if is_dead:
                self.logger.warn(f"Previous ffmpeg process for {stream_index} died.")

            self.logger.info(
                f"Spawning ffmpeg for {stream_index} ({stream_name}):"
                f" {self.redact_secrets(cmd)}"
            )
            self._ffmpeg_handles[stream_index] = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, shell=True
            )

    def stop_video_stream(self, stream_index: str):
        if stream_index in self._ffmpeg_handles:
            self.logger.info(f"Stopping stream {stream_index}")
            self._ffmpeg_handles[stream_index].kill()

    async def close(self):
        self.logger.info("Cleaning up instance")
        await self.trigger_motion_stop()
        self.close_streams()

    def close_streams(self):
        for stream in self._ffmpeg_handles:
            self.stop_video_stream(stream)
