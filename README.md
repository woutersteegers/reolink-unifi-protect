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

## Architecture

```
Reolink camera                      NAS / Docker host                    UniFi Protect NVR
──────────────                      ─────────────────                    ─────────────────
RTSP main (5K HEVC)  ──►  ffmpeg -c:v copy ─► hevc_flv ─► clock_sync ──►  video1 (HQ, native H.265)
RTSP sub  (H.264)    ──►  ffmpeg (QSV hevc transcode, optional) ───────►  video2/3 (LQ)
HTTPS API            ──►  snapshots, GetAiState fallback poller
Baichuan :9000       ──►  ai-sidecar (Node) ─► HTTP ─► proxy ──────────►  EventSmartDetect
                          decodes per-object class + boxes                (moving bounding boxes)
```

Three moving parts, all in this repo:

| Component | What it does |
|---|---|
| `unifi/` (the proxy) | Speaks `ubnt_avclient` to Protect: adoption, identity, streams, events. New modules: `hevc_flv.py` (UniFi HEVC framing), reworked `clock_sync.py` (wall-clock trailers). |
| `deploy/ai-sidecar/` | Small Node service that subscribes to the camera's detection rectangles over the Baichuan protocol ([nodelink-js](https://github.com/apocaliss92/nodelink-js) for transport, own TLV decoder) and streams them to the proxy. |
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

| Flag | Purpose |
|---|---|
| `--hi-codec h265`, `--hi-width/height/fps` | Honest per-channel advertisement for video1 (upstream hardcodes h264) |
| `--lo-codec`, `--lo-h265-transcode`, `--lo-width/height/fps` | Same for video2/3, with optional QSV HEVC transcode of an H.264 sub stream |
| `--sysid`, `--platform`, `--fw-build` | Full camera identity (e.g. `0xa598` / `sav837gw` = UVC G5 Pro) — required for Protect ≥ 3.x model recognition and the H.265 capability gate |
| `--reolink-ai-host/-user/-password` | HTTPS `GetAiState` poller: typed detections even without the sidecar |
| `--ai-sink-port` | HTTP endpoint where the Baichuan sidecar posts real per-object boxes |
| `--ai-min-confidence`, `--ai-confidence-overrides` | Confidence gates, globally and per class (e.g. `animal=0.6`) |
| `--ai-min-movement` | Suppresses detections that never move (kills "the house across the street is a person") |
| `--ai-classes` | Class priority when picking an event type |
| `--snapshot-url` | Snapshot via the camera's HTTP(S) API instead of a fourth RTSP session (Reolink WiFi cameras limit concurrent RTSP) |

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
modifications © 2026 Wouter Steegers.
