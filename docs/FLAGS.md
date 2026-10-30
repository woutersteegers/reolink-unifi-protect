# Flag reference

Every option of the `rtsp` source, generated from the argument parser. **New** marks options that are not in the upstream project
([woutersteegers/reolink-unifi-protect](https://github.com/woutersteegers/reolink-unifi-protect)). Pass them after the `rtsp` subcommand:
`unifi-cam-proxy --host … --mac … --cert … --token … rtsp -s <url> [options]`. Identity flags (`--sysid`, `--platform`,
`--fw-version`, `--fw-build`, `--name`, `--ip`) are top-level and come before `rtsp`; see the README quickstart.

## Streams, codecs and transcoding

| Flag | Default | New | What it does |
|---|---|---|---|
| `--ffmpeg-args`, `-f` | `-c:v copy -ar 32000 -ac 1 -codec:a aac -b:a 32k` |  | Transcoding args for `ffmpeg -i <src> <args> <dst>` |
| `--rtsp-transport` | `tcp` |  | RTSP transport protocol used by stream |
| `--hw-transcode` | off |  | Hardware-transcode the high stream to H.264 using --hw-backend |
| `--hw-backend` | `qsv` | **New** | `qsv` for Intel, `vaapi` for AMD/Mesa; selecting a backend alone does not enable encoding. See [AMD GPU setup](AMD-VAAPI.md) |
| `--hw-device` | `/dev/dri/renderD128` |  | DRM render node for hardware transcoding |
| `--transcode-width` | `2560` |  | Output width for --hw-transcode (height from --transcode-height) |
| `--transcode-height` | `776` |  | Output height for --hw-transcode |
| `--transcode-bitrate` | `6M` |  | Target bitrate for the hardware H.264 encode |
| `--hi-codec` | `h264` |  | Codec ACTUALLY carried by the high-quality stream (video1) |
| `--hi-width` | `1920` |  |  |
| `--hi-height` | `1080` |  |  |
| `--hi-fps` | `15` |  |  |
| `--lo-codec` | `h264` |  | Codec actually carried by the medium/low streams (video2/3) |
| `--lo-width` | `1280` |  |  |
| `--lo-height` | `720` |  |  |
| `--lo-fps` | `15` |  |  |
| `--lo-h265-transcode` | off |  | Hardware-transcode the medium/low channels to H.265 using --hw-backend |
| `--lo-transcode-bitrate` | `1M` |  | Bitrate for --lo-h265-transcode |
| `--lo-scale` | off | **New** | Scale lower hardware-transcoded channels with Intel QSV or VA-API. Requires `--lo-h265-transcode`; a shared relay provides one upstream connection. |
| `--lo-audio-args` | backend default | **New** | Audio override for lower H.265 transcodes only; QSV defaults to copy, VA-API to AAC 16k mono. See runtime migration guide for FFmpeg 7. |
| `--vaapi-driver` | auto | **New** | Optional libva driver override, e.g. `iHD` or `radeonsi`; does not enable encoding. |
| `--no-audio` | off | **New** | Send video only on every channel (for cameras whose audio timestamps stall the FLV muxer) |
| `--source`, `-s` |  |  | Source(s) for up to three streams in order of descending quality |
| `--http-api` | `0` |  | Specify a port number to enable the HTTP API (default: disabled) |
| `--snapshot-url`, `-i` |  |  | HTTP endpoint to fetch snapshot image from |

## AI detections (Reolink)

| Flag | Default | New | What it does |
|---|---|---|---|
| `--reolink-ai-host` |  |  | Camera IP; enables polling GetAiState over HTTPS |
| `--reolink-ai-user` | `admin` |  |  |
| `--reolink-ai-password` |  |  |  |
| `--reolink-ai-interval` | `1.0` |  | Seconds between GetAiState polls |
| `--reolink-push-ai` | off | **New** | Drive AI detections (person/vehicle/animal) from Baichuan push instead of polling GetAiState; implies the push connection |
| `--ai-min-confidence` | `0.75` |  | Ignore AI detections below this confidence (0-1) |
| `--ai-min-movement` | `10` |  | Suppress boxes until they move this far (0-1000 units; kills stationary false positives like buildings; 0 disables) |
| `--ai-movement-box-ratio` | `0.25` |  | Movement must also exceed this fraction of the box's smaller side, so box-size jitter on large static objects doesn't count as motion (0 = use --ai-min-movement only) |
| `--ai-movement-reports` | `2` |  | Consecutive box reports (~0.4s apart) that must show the movement before a track counts as moving; 1 = no smoothing |
| `--ai-classes` | `person,vehicle,animal` |  | Detection classes to forward, in priority order |
| `--ai-confidence-overrides` |  |  | Per-class minimum confidence, e.g. 'animal=0.9' — for classes the camera misfires on (a person read as dog_cat) |

## Real detection boxes (sidecar)

| Flag | Default | New | What it does |
|---|---|---|---|
| `--ai-sink-port` | `0` |  | Port to receive AI box reports from a standalone sidecar (defaults to loopback 8099 with --ai-sidecar) |
| `--ai-sidecar` | off | **New** | Run the bundled Baichuan box sidecar (Node, in the image) so events carry the camera's real moving detection boxes and per-object crops. Needs --reolink-ai-host; the standby replica never starts it |
| `--ai-sidecar-profile` | `main` | **New** | Baichuan stream the boxes are read from. Doorbells carry them on the main stream only |
| `--ai-sidecar-always-on` | off | **New** | Keep the sidecar's stream open continuously (about 4.5 Mbit/s for a doorbell's main stream) instead of only during detections |
| `--ai-sidecar-path` | `/opt/ai-sidecar/index.mjs` | **New** | Where the sidecar script lives in the image |

## Doorbells, rings and two-way audio

| Flag | Default | New | What it does |
|---|---|---|---|
| `--doorbell` | off | **New** | Advertise the camera as a doorbell (featureFlags.doorbell) |
| `--reolink-push-button` | off | **New** | Detect the doorbell button from Baichuan push (port 9000, no HTTPS) instead of polling GetEvents |
| `--reolink-visitor-poll` | off | **New** | Poll GetEvents on --reolink-ai-host for doorbell button presses |
| `--reolink-visitor-interval` | `1.0` | **New** | Seconds between GetEvents visitor polls |
| `--protect-ring-user` |  | **New** | Local Protect user used to log in and ring on a button press |
| `--protect-ring-password` |  | **New** |  |
| `--speaker` | off | **New** | Advertise hasSpeaker and log Protect's talkback audio (experiment) |
| `--talkback-bridge` | off | **New** | Play Protect's talkback audio on the camera speaker (needs --speaker) |

## PTZ, image settings and reboot

| Flag | Default | New | What it does |
|---|---|---|---|
| `--ptz` | off | **New** | Advertise a pan/tilt/zoom camera so Protect shows its PTZ controls |
| `--ptz-home-preset` | `0` | **New** | Reolink preset id the camera returns to for Protect's Home View (0 is the camera's 'default' preset) |
| `--spotlight` | off | **New** | Advertise a spotlight so Protect's light action switches the Reolink's white LED on for the requested time (needs --reolink-ai-host) |
| `--spotlight-brightness` | none | **New** | Brightness 0-100 when Protect turns the spotlight on (default: keep the camera's own setting) |
| `--spotlight-default-seconds` | `15` | **New** | How long the spotlight stays on when neither the request nor Protect's 'turn off after no motion' slider gives a duration |
| `--reolink-isp` | off | **New** | Write Protect's image settings (brightness, contrast, WDR, ...) to the camera when they change (needs --reolink-ai-host) |
| `--reolink-reboot` | off | **New** | Reboot the Reolink camera when Protect's restart camera is used (needs --reolink-ai-host) |
| `--reolink-scheme` | `https` | **New** | Scheme for the camera's web API on --reolink-ai-host (use `http` when the camera's HTTPS is turned off) |
| `--reolink-snapshot` | off | **New** | Fetch snapshots from --reolink-ai-host with a token login instead of --snapshot-url; works with passwords that break user/password URLs |
