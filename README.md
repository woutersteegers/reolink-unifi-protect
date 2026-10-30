# Reolink → UniFi Protect Bridge

A fork of [keshavdv/unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy)
that gets a non-Ubiquiti camera into UniFi Protect as a *first-class* citizen:

- **Native H.265 passthrough** — the camera's 5K HEVC main stream is fed to
  Protect untouched (`-c:v copy`), rewritten on the fly into UniFi's
  proprietary HEVC-in-FLV framing. To our knowledge this is the first working
  H.265 passthrough over the `ubnt_avclient` protocol — every previous
  fork/issue ends in transcode-to-H.264.
- **Typed smart detections without an AI Port** — Person / Vehicle / Animal
  events with *moving, accurately-placed bounding boxes*, sourced from the
  camera's own on-board AI over Reolink's Baichuan protocol (the same boxes
  the Reolink app draws).
- **Full camera identity emulation** — Protect ≥ 3.x recognizes the proxy as
  a real camera model (sysid/platform/firmware-build, WSS identity headers),
  which is what unlocks the H.265 "enhanced encoding" capability gate.
- **Clean audio passthrough** (AAC-LC), working microphone, ~1–2 s live-view
  startup, snapshot support without an extra RTSP session.

Developed and verified against a **Reolink Elite Floodlight WiFi**
(dual-lens 180°, 5120×1552 HEVC) and **UniFi Protect 7.1.87** on a UDM Pro.
Most of it is general: the H.265 framing, identity emulation, and smart-detect
event pipeline apply to any RTSP camera; the AI sidecar applies to any recent
Reolink with on-board detection.

> **Disclaimer**: not affiliated with Ubiquiti or Reolink. The proxy emulates
> a UniFi camera (UVC G5 Pro by default in the example config) toward your own
> NVR. Protect updates may change the ingest protocol at any time.

## Contributors

This fork is maintained and developed by:

- [Andy Drag (@andy83-projects)](https://github.com/andy83-projects) — fork
  maintainer, lab integration and validation, and AMD VA-API support.
- [@jikdoc](https://github.com/jikdoc) — snapshot, push-event and credential
  fixes, the Debian runtime upgrade, and audio/scaling controls
  ([PRs #1–#6](https://github.com/andy83-projects/reolink-unifi-protect/pulls?q=is%3Apr+author%3Ajikdoc+is%3Amerged)).

The upstream projects and their contributors are acknowledged in [Credits](#credits).
The full commit-based contributor list is available in
[GitHub Contributors](https://github.com/andy83-projects/reolink-unifi-protect/graphs/contributors).

## What the andy83-projects fork adds

Everything below was added on top of the upstream project and is covered by the unit tests in `tests/`.
Each feature is opt-in by flag, so a camera that does not use it behaves exactly as before. The complete
list of options is in [docs/FLAGS.md](docs/FLAGS.md).

| Feature | Flags | Details |
|---|---|---|
| **Doorbells**: real rings, call screen and thumbnail from the button; the camera is advertised to Protect as a doorbell | `--doorbell`, `--reolink-push-button`, `--protect-ring-user/-password`, `--reolink-visitor-poll` | [docs/DOORBELL.md](docs/DOORBELL.md) |
| **Two-way audio**: Protect's talk audio is converted to G.711 and played on the camera speaker over an ONVIF backchannel | `--speaker`, `--talkback-bridge` | [docs/DOORBELL.md](docs/DOORBELL.md) |
| **Baichuan push events**: person / vehicle / animal and the doorbell button arrive over the camera's push channel (port 9000) instead of polling its web server, with endless reconnect and backoff | `--reolink-push-ai`, `--reolink-push-button` | [docs/DOORBELL.md](docs/DOORBELL.md) |
| **UniFi Protect 7.3.x compatibility**: event frames match the strict schema of Protect's new `ds` daemon, so events are recorded again; how to debug `ds` rejections | (always on) | [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| **Events that show up under the Person / Animal / Vehicle filters**: a per-object crop and a full-frame image are announced with every event | (always on) | [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| **Real detection boxes** from the camera's own AI via the bundled Baichuan sidecar, run and supervised by the proxy, opened only while a detection is in progress | `--ai-sidecar`, `--ai-sidecar-profile`, `--ai-sidecar-always-on` | [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| **PTZ**: Protect's joystick and zoom drive the camera, **Home View** returns to a camera preset | `--ptz`, `--ptz-home-preset` | [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| **Spotlight**: Protect's light action switches the Reolink's white LED on for the requested time | `--spotlight`, `--spotlight-brightness`, `--spotlight-default-seconds` | [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| **Image settings sync**: brightness, contrast, saturation, hue, sharpness, denoise, mirror/flip and WDR changed in Protect are written to the camera | `--reolink-isp` | `unifi/reolink_isp.py` |
| **Remote reboot**: Protect's "restart camera" also reboots the Reolink (5 minute cooldown) | `--reolink-reboot` | `unifi/cams/rtsp.py` |
| **One image for everything** (doorbells and plain cameras differ only in flags), Node and the sidecar included | | `Dockerfile.vaapi` |

## Tested with

| Camera | Role | Verified on real hardware |
|---|---|---|
| **Reolink Video Doorbell** ×2 (firmware v3.0.0.6460_2605211897) | front and rear doorbells | rings from the button, call screen, two-way audio, push AI events, per-object crops and full-frame thumbnails, real detection boxes through the sidecar (main stream), active/standby failover |
| **Reolink RLC-510A** | plain camera | push AI events, thumbnails, H.264 passthrough |
| **Reolink E1 Zoom** (pan/tilt + optical zoom) | PTZ camera | joystick and zoom, Home View to preset 0, push AI events, thumbnails, image-settings writes, adoption as "UVC AI PTZ Industrial" (`--sysid 0xa604 --platform CV25Z`). Its detection boxes were not probed, so the sidecar is not used there |
| **Reolink Elite Floodlight WiFi** | upstream author's development camera | H.265 passthrough, boxes via the sidecar's sub-stream mode |

Controller: **UniFi Protect 7.1.87** (upstream author), **7.2.105** and **7.3.70**, all on a UDM Pro.

**`--reolink-reboot` confirmed on hardware (2026-10-14):** triggering "restart camera" in Protect's
UI does reboot the real physical Reolink, as expected. Previously only unit-tested.

Known gaps, so nobody assumes more than was checked:

- `--reolink-isp`: the camera API accepted the values we send (HDR/WDR, brightness and friends were written and
  restored), but Protect's slider-to-camera mapping has not been verified setting by setting, and the
  mirror/flip direction is unconfirmed.
- Boxes are on the **main** stream on the doorbells and the **sub** stream on the Floodlight; other models are
  unverified, and the sidecar costs about 4.5 Mbit/s while a doorbell's main stream is open.
- Only the `rtsp` source has the smart-detection bridge.

## Architecture

```
Reolink camera                      NAS / Docker host                    UniFi Protect NVR
──────────────                      ─────────────────                    ─────────────────
RTSP main (5K HEVC)  ──►  ffmpeg -c:v copy ─► hevc_flv ─► clock_sync ──►  video1 (HQ, native H.265)
RTSP sub  (H.264)    ──►  ffmpeg (QSV hevc transcode, optional) ───────►  video2/3 (LQ)
HTTPS API            ──►  snapshots, GetAiState fallback poller
Baichuan :9000 push  ──►  proxy (reolink-aio) ─ detection start/stop, doorbell button
Baichuan :9000 video ──►  ai-sidecar (Node) ─► HTTP ─► proxy ─────────►  EventSmartDetect
                          decodes per-object class + boxes                (moving bounding boxes)
```

The sidecar can run as its own container (the reference compose file, always on, upstream's
original setup) or **bundled in the proxy image and started by the proxy** with `--ai-sidecar`, which is
the mode used on Reolink doorbells: it opens the camera's video stream only while a detection is in
progress. See [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md#real-detection-boxes---ai-sidecar).

Three moving parts, all in this repo:

| Component | What it does |
|---|---|
| `unifi/` (the proxy) | Speaks `ubnt_avclient` to Protect: adoption, identity, streams, events. New modules: `hevc_flv.py` (UniFi HEVC framing), reworked `clock_sync.py` (wall-clock trailers). |
| `deploy/ai-sidecar/` | Small Node service that subscribes to the camera's detection rectangles over the Baichuan protocol ([nodelink-js](https://github.com/apocaliss92/nodelink-js) for transport, own TLV decoder) and streams them to the proxy. Runs standalone (always on) or, with `--ai-sidecar`, inside the proxy image under the proxy's supervision (on demand). |
| `deploy/docker-compose.yml` | The reference deployment wiring it all together. |

## Quickstart

Requirements:

- Docker host on the same network as camera and NVR. For the LQ H.265
  transcode you need a VA-API/QSV-capable Intel iGPU (`/dev/dri`); once
  Protect's enhanced encoding is on it expects *every* channel in HEVC.
- Accurate NTP on the host — Protect silently drops frames if your clock
  drifts by more than ~1 s.
- Reolink camera with RTSP enabled.

```bash
git clone https://github.com/woutersteegers/reolink-unifi-protect.git
cd reolink-unifi-protect/deploy

# 1. Client certificate (the proxy authenticates to Protect with it)
openssl ecparam -out /tmp/pk.key -name prime256v1 -genkey -noout
openssl req -new -sha256 -key /tmp/pk.key -out /tmp/s.csr \
  -subj "/C=TW/L=Taipei/O=Ubiquiti Networks Inc./OU=devint/CN=camera.ubnt.dev/emailAddress=support@ubnt.com"
openssl x509 -req -sha256 -days 36500 -in /tmp/s.csr -signkey /tmp/pk.key -out /tmp/pub.key
cat /tmp/pk.key /tmp/pub.key > client.pem && rm /tmp/pk.key /tmp/pub.key /tmp/s.csr

# 2. Secrets — create deploy/.env (gitignored):
#    NVR_IP=            your Protect NVR
#    ADOPTION_TOKEN=    from https://<NVR_IP>/proxy/protect/api/cameras/manage-payload
#                       (log in first; tokens expire after ~60 minutes)
#    CAM_MAC=           a MAC for the virtual camera (e.g. AA:BB:CC:00:11:22)
#    CAM_IP=            the Reolink's IP
#    CAM_NAME=          display name in Protect
#    CAM_USER=          camera username (usually admin)
#    CAM_PASS=          camera password

# 3. Build and run
docker compose up -d --build
```

Then adopt the camera when it appears in Protect and enable **enhanced
encoding** (H.265) in its settings. In Protect, also give the camera a smart
detection zone and enable the detection types you want.

**Important:** Protect caches model/codec/resolution at *adoption*. If you
change any of those flags later, remove the camera in Protect, put a fresh
adoption token in `.env`, and `docker compose up -d --force-recreate`.

## Notable flags added by this fork

The full, generated list is in [docs/FLAGS.md](docs/FLAGS.md); the main ones are below.

| Flag | Purpose |
|---|---|
| `--hi-codec h265`, `--hi-width/height/fps` | Honest per-channel advertisement for video1 (upstream hardcodes h264) |
| `--lo-codec`, `--lo-h265-transcode`, `--lo-width/height/fps` | Same for video2/3, with optional hardware HEVC transcode of a sub stream |
| `--hw-backend vaapi`, `--hw-device`, `--hw-transcode` | Opt-in AMD/Mesa VA-API encoding; Intel QSV remains the default backend. See [AMD GPU setup](docs/AMD-VAAPI.md) for container, codec, and Kubernetes requirements |
| `--sysid`, `--platform`, `--fw-build` | Full camera identity (e.g. `0xa598` / `sav837gw` = UVC G5 Pro) — required for Protect ≥ 3.x model recognition and the H.265 capability gate |
| `--reolink-ai-host/-user/-password` | HTTPS `GetAiState` poller: typed detections even without the sidecar |
| `--ai-sink-port` | HTTP endpoint where a standalone Baichuan sidecar posts real per-object boxes (with `--ai-sidecar` it defaults to loopback port 8099) |
| `--ai-min-confidence`, `--ai-confidence-overrides` | Confidence gates for *starting* an event from boxes, globally and per class (e.g. `animal=0.6`). Not applied in on-demand `--ai-sidecar` mode, where the camera's own push detection starts the event and boxes only decorate it (20% floor) |
| `--ai-min-movement` | Suppresses boxes that never move (kills "the house across the street is a person"). Not applied in on-demand `--ai-sidecar` mode |
| `--ai-classes` | Class priority when picking an event type |
| `--reolink-push-ai` | Drive person/vehicle/animal detections from the camera's own Baichuan push channel (port 9000) instead of polling `GetAiState`; no HTTPS calls to the camera |
| `--reolink-push-button` | Doorbell button press from the same push connection (see [docs/DOORBELL.md](docs/DOORBELL.md)) |
| `--doorbell` | Advertise the camera to Protect as a doorbell |
| `--protect-ring-user/-password` | A local Protect user the proxy logs in as to raise a real ring on a button press |
| `--speaker`, `--talkback-bridge` | Two-way audio: Protect's talk audio is converted to G.711 and played on the camera speaker over an ONVIF RTSP backchannel |
| `--reolink-visitor-poll` | Fallback: poll `GetEvents` for the button instead of using push |
| `--ai-sidecar`, `--ai-sidecar-profile`, `--ai-sidecar-always-on` | Run the bundled Baichuan box sidecar: events carry the camera's real moving detection boxes and per-object crops. On-demand by default (stream open only during detections). See [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md) |
| `--snapshot-url` | Snapshot via the camera's HTTP(S) API instead of a fourth RTSP session (Reolink WiFi cameras limit concurrent RTSP) |
| `--spotlight`, `--spotlight-brightness`, `--spotlight-default-seconds` | Advertise a spotlight so Protect's light action turns the Reolink's white LED on for the requested time (see [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md)) |
| `--ptz`, `--ptz-home-preset` | Advertise a pan/tilt/zoom camera so Protect shows its joystick; drive the Reolink from Protect's controls and map Home View to a camera preset (see [docs/PROTECT-7.3.md](docs/PROTECT-7.3.md)) |
| `--reolink-isp` | Write Protect's image settings (brightness, contrast, WDR, ...) to the camera when they change |
| `--reolink-reboot` | Reboot the Reolink camera when Protect's restart camera is used |
| `--reolink-scheme http` | For cameras with HTTPS turned off: the image-settings, spotlight, PTZ, reboot and snapshot calls use plain HTTP |
| `--reolink-snapshot` | Snapshots through a token login (password in the request body) instead of `--snapshot-url`, whose `password=` query parameter fails on recent firmware when the password has special characters |

## Doorbells, push events and two-way audio

Reolink doorbells work as first-party Protect doorbells: a press raises a real
ring (notification, call screen, thumbnail), talk audio plays on the doorbell,
and AI events arrive over the camera's push channel rather than by polling its
web server. One image serves both doorbells and plain cameras; the behaviour is
chosen with flags. Details, and the Protect internals they rest on, are in
[`docs/DOORBELL.md`](docs/DOORBELL.md).

## Protect 7.3.x compatibility

Protect 7.3.70 moved smart-detect handling into a native `ds` daemon with a strict
event schema. Frames that do not match are dropped with a misleading
"not expected over avclient" error. The proxy is compatible with it; what changed,
how to debug it from `ds`'s own log, and the PTZ findings are in
[`docs/PROTECT-7.3.md`](docs/PROTECT-7.3.md).

## Protocol notes

The genuinely novel reverse-engineering is written up in
[`docs/PROTOCOL-NOTES.md`](docs/PROTOCOL-NOTES.md):

- UniFi's proprietary HEVC-in-FLV framing (codec id 8, the config-tag layout
  that avoids `VUNK`, wall-clock trailer ticks);
- how Protect gates codecs by *controller-side* state and emulated model, not
  by what the camera declares;
- Protect 7.x's smart-detect payload schema (descriptors, zonesStatus objects);
- a Baichuan finding worth knowing if you touch Reolink AI: on
  YOLO-World-generation firmware the widely-assumed `type1`/`type2` TLV
  semantics are **reversed** — `type2` is the per-object class.

## Other camera sources

Upstream's other sources (Amcrest, Dahua, Hikvision, Frigate, Reolink NVR,
generic RTSP) are still present and benefit from the identity/codec work —
see [`docs/sources/`](docs/sources) for their per-source options. The
smart-detection bridge is currently wired into the `rtsp` source.

## Credits

This repository is a fork of
[woutersteegers/reolink-unifi-protect](https://github.com/woutersteegers/reolink-unifi-protect)
(itself a fork of keshavdv/unifi-cam-proxy). All of the camera emulation, codec
and Baichuan work that this builds on is theirs; please star and credit the
originals. The doorbell, push-event and talkback additions were written by
andy83-projects together with Claude (Anthropic).

- [keshavdv/unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) —
  the foundation this fork builds on (MIT).
- [apocaliss92/nodelink-js](https://github.com/apocaliss92/nodelink-js) —
  Baichuan protocol transport, and the detection-TLV walker our decoder
  is derived from.
- The redalert unifi-cam-proxy fork's wire-format captures and
  [unifi-rtsp-converter](https://github.com/MicahZoltu/unifi-rtsp-converter)'s
  decompiled-Protect research, which informed the ingest analysis.

## License

MIT — see [LICENSE](LICENSE). Original work © 2023 Keshav Varma;
modifications © 2026 Wouter Steegers;
further modifications © 2026 andy83-projects.

For Debian 13 / FFmpeg 7 images and native Python requirements, follow the [runtime migration guide](docs/RUNTIME-MIGRATION.md) before upgrading.
