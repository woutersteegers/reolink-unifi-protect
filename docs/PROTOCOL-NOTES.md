# Protocol notes

Reverse-engineering findings behind this fork, established empirically against
UniFi Protect 7.1.87 (UDM Pro) and a Reolink Elite Floodlight WiFi in August
2026. Everything here was a real blocker at some point; nothing is speculation
unless marked so.

## 1. UniFi's HEVC-in-FLV framing

Protect ingests camera streams as FLV over TCP, but real UniFi cameras do not
use standard FLV for H.265. What actually works:

- **Video tags use FLV codec id 8** (the legacy "AVC" slot re-used), not
  Enhanced-RTMP `hvc1` fourcc tags. ffmpeg ≥ 6.1 emits enhanced-FLV `hvc1`
  extended tags for HEVC; `unifi/hevc_flv.py` translates them to the UniFi
  framing on the fly.
- **The codec-config tag is NOT an hvcC record.** Sending a standard hvcC
  makes Protect log the stream's codec as `VUNK` (unknown) and discard it.
  The working layout is: `byte0 = 0x68`, `byte1 = 0x01`, followed by the bare
  VPS, SPS and PPS NALs, each prefixed with a 2-byte big-endian length —
  no arrays, no NAL-type headers, no profile/level preamble.
- **`onMetaData` must be an AMF0 OBJECT (0x03), not an ECMA array (0x08)**,
  containing the camera vocabulary (nine keys incl. width/height/framerate)
  plus `extendedFormat: true`. ffmpeg's ECMA-array metadata is replaced
  wholesale. ffmpeg's `PacketTypeMetadata` (4) tags are dropped.
- **Every FLV tag carries a 16-byte wall-clock trailer** appended by the
  camera (`unifi/clock_sync.py` re-implements it). The trailer's tick value
  must be expressed in the *stream's own clock*: 90000 ticks/s for video,
  the audio sample rate (e.g. 11025) otherwise. Getting this wrong doesn't
  error — Protect's ingest (`ms`/`msr` services) silently drops FeedData and
  live view spins forever.
- Real cameras additionally prepend an 11-byte magic
  `DE 19 16 15 47 17 DE 19 16 75 50` before the FLV header. Protect 7.1.87
  ingests fine without it; newer versions may start validating it.
- **Host clock accuracy matters.** A proxy host whose clock is ~60 s off NTP
  produces streams Protect ingests but will not play/record. Symptoms look
  identical to framing bugs; check NTP first.

## 2. How Protect decides a camera may do H.265

The codec is **controller-driven, not camera-declared** (confirmed via
bootstrap dumps and decompiled Protect `service.js`):

- Protect keeps a camera-level `videoCodec` field (default `h264`) that
  governs decode/playback. It *commands* the codec via
  `ChangeVideoSettings` per-channel `"type"` and treats the camera's response
  as a mere ack — declaring `h265` in your response changes nothing.
- The UI's "enhanced encoding" (H.265) toggle only appears when the camera's
  `featureFlags.videoCodecs` (plural key!) contains `"h265"`. Real
  H.265-capable models report `["h264", "h265", ...]`; upstream
  unifi-cam-proxy reported nothing.
- **Capabilities are cached at adoption** (model, resolution, codec), but
  `featureFlags` refresh on runtime reconnect. Changing advertised
  capabilities generally means: remove the camera in Protect → fresh adoption
  token → re-adopt.
- **Protect ≥ 3.0 ignores the hello's `model` string.** Model recognition
  keys on `sysid` + `platform` + `firmwareBuild` in the adoption payload
  *and* on WSS connection headers: `camera-model` (the sysid in hex),
  `camera-ip`, `camera-firmware`, a stable `device-id`. E.g. sysid `0xa598`,
  platform `sav837gw` = UVC G5 Pro. Without these you get a generic
  "Camera" with an H.264-only capability profile.
- Once enhanced encoding is on, Protect expects **every** channel in HEVC —
  including the low-quality ones. If your camera's sub stream is H.264,
  transcode it (this fork's `--lo-h265-transcode` uses Intel QSV).
- Advertising a current firmware version (`--fw-version`/`--fw-build`)
  prevents Protect from nagging about (or scheduling) an update the virtual
  camera cannot take.

## 3. Protect 7.x smart-detection payload schema

`EventSmartDetect` payloads are validated against
`smartDetectObjectsTransformMessage` (AJV/zod, in the decompiled service).
Violations are silently fatal — the event degrades to generic Motion or is
dropped with `AJV_PARSE_ERROR` in the service logs. Requirements:

- `displayTimeoutMSec` is **required**. It also controls how long a drawn box
  persists; large values (5000) paint a "trail" of stale boxes behind a
  moving subject. ~600 ms tracks smoothly.
- `descriptors[]` per object, each with: `trackerID` (**number**; keep it
  stable across updates of the same physical object or Protect draws each
  update as a new box), `name`, `objectType`, `confidenceLevel`,
  `coord` (`[x, y, w, h]` normalized to **0–1000** ints), `zones[]`,
  `lines[]`, `stationary: false`, `coord3d[]`. Event types are filled only
  from non-stationary descriptors.
- `zonesStatus` values are **objects** `{"status": "enter"|"leave"|"moving"}`
  — not bare numbers/strings.
- Use `edgeType: "moving"` updates between start and stop so the box follows
  the subject.
- The camera must advertise `smartDetectTypes` in featureFlags
  (`smartDetect` alone is ignored) and have at least one smart-detection
  zone configured.
- **Answer every `responseExpected` request**, including ones you don't
  implement (`ChangeSmartMotionSettings`, `SmartMotionTest`, …). Protect 7.x
  blocks ~5 s per unanswered settings request — it's a major source of slow
  live-view startup. A generic ack is sufficient.
- Camera-side, a short GOP (this deployment uses gop = 1 via Reolink's
  `SetEnc`) minimizes viewer-join latency.

## 4. Reolink Baichuan AI boxes: the type1/type2 swap

Recent Reolink cameras stream their on-board AI detection rectangles (the
boxes the Reolink app draws) in the `additionalHeader` of BcMedia frames on
port 9000, as nested TLVs (sometimes LZ4-frame-compressed). nodelink-js can
decode these — but its class mapping was derived from an E1 Zoom, and on
**YOLO-World-generation firmware (2025+) the semantics are reversed**:

- nodelink assumes `type1` = object class, `type2` = view. On this firmware
  **`type2` IS the per-object class** — `1` = people, `2` = vehicle,
  `3` = animal (matching Reolink SDK enums) — and `type1` is the **view
  index** of the SDK's 3-view layout.
- Each physical box is emitted as *multiple copies*, one per view wrapper.
  All copies of one box share the same `type2` (class). Per-view box-record
  lengths are 10 / 14 / 13 bytes — which is what created nodelink's
  "record length varies by class" illusion.
- Views 2/3 carry extra bytes beginning with a **u16 LE stable track id** —
  useful for cross-frame object identity (this fork uses it for Protect's
  `trackerID` and for its static-object movement gate).
- Box leaves are TLV type 4 (lengths 10/13/14) or type 2 (length 10):
  `x1, y1, x2, y2, confidence` as u16 LE, in a coordinate space given by a
  frame-size TLV (`03 04 00`, default 896×480).
- Deduping copies by "class specificity" (nodelink's approach, where animal
  outranks people) mislabels humans as animals here. Correct approach: group
  copies by identical rectangle, take the class from `type2`.

Verified live with a dog, a person, and a static distant building
simultaneously in frame. This is worth upstreaming to
[nodelink-js](https://github.com/apocaliss92/nodelink-js) as a
firmware-generation switch.

`deploy/ai-sidecar/decode.mjs` implements the copy-preserving decoder;
`deploy/ai-sidecar/index.mjs` maps groups to typed, normalized boxes and
heartbeats them to the proxy (including empty reports, so the proxy can time
events out without polling).

## 5. Assorted operational findings

- **Reolink WiFi cameras limit concurrent RTSP sessions.** Passthrough needs
  three (main + sub ×2). Use the HTTP(S) API for snapshots
  (`--snapshot-url`) instead of a fourth session; if streams respawn-loop
  right after adoption, suspect the session cap first.
- The Reolink HTTP API on some models is **HTTPS-only** (HTTP 302s), which
  breaks libraries that assume HTTP — hence this fork's `rtsp` source +
  HTTPS snapshot URL with TLS verification off (self-signed cert).
- `ffmpeg -use_wallclock_as_timestamps` on an RTSP input stamps *arrival*
  times as PTS — audible as crackle on audio passthrough. Trust the RTP
  clock instead. `aresample=async=1` makes it worse (hard fill/trim).
- Intel iGPU (tested: i5-1235U / Iris Xe): use **QSV, not VAAPI** in
  containers (VAAPI hit `Cannot allocate memory`); the scaler must be
  `vpp_qsv` (`scale_qsv` fails to configure its input pad); do **not** pin
  `-c:v hevc_qsv` as decoder (breaks H.264 inputs — let `-hwaccel qsv`
  auto-pick). H.264 encode caps at 4096 px width; one 2560-px transcode runs
  ~1.0× realtime, two don't fit — gate transcoding to the channels that
  need it.
