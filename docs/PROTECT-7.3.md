# UniFi Protect 7.3.x: what changed and what the proxy does about it

Protect 7.3.70 moved smart-detect handling out of Protect's Node process into a
native daemon, `ds` ("Unifi Protect Device Service", `/usr/bin/ds`, config
`/usr/share/ds/ds.json`, logs under `/srv/ds/logs/`). It terminates the camera
websocket on port 7442 (the same port this proxy connects to) and relays to
Protect over a local websocket.

## Symptom

Events stop appearing (no chip, no timeline entry, no notification) and Protect's
`cameras.avclient.log` shows, for every event:

```
Received EventSmartDetect over avclient which was not expected to be received; DS should handle this message
```

That line is a **symptom, not a ban**. `ds` parses each event frame with a strict
typed schema; a frame it cannot parse is relayed raw to Protect, which logs that
error and drops it. The real reason is in `ds`'s own log, one offending field per
attempt:

```
grep -E 'Failed to parse|not found' /srv/ds/logs/ds.log | tail
```

`/srv/ds/logs/payloads.jsonl` holds decoded frames from real cameras (plain JSON,
the same shape this proxy sends), which is the easiest way to copy a schema.

## What `ds` requires (all implemented here)

| `ds` message | Requirement | Where |
|---|---|---|
| ``missing field `level` `` | every `zonesStatus` entry needs an integer `level` next to `status` | `SMART_ZONE_LEVEL` in `unifi/cams/base.py` |
| ``expected u32`` | `eventId` and `trackerID` must fit 32 bits, so keep the event counter small (do not seed it from the epoch) | `_motion_event_id` |
| ``missing field `timeStamp` `` / ``expected an RFC 3339 formatted date`` | event envelopes need `timeStamp` as an RFC 3339 **string** | `gen_event()` |
| ``Camera 02:4A:… not found`` | the handshake `camera-mac` header must be the 12-hex form Protect stores (no colons, uppercase) | `Core._headers()` |

## Making events show up under Person / Animal / Vehicle

Protect's filter chips are driven by `metadata.detectedThumbnails`, which `ds`
builds from the **leave** frame's `smartDetectSnapshots`. A frame without them is
stored as a bare event that the filters do not list. This proxy therefore:

- crops the detected object (a centred box when the camera's push channel gives no
  coordinates) and announces it in `smartDetectSnapshots` plus a matching
  `trackerIDAttrMap` of `{objectType, zone}`;
- sizes that crop from the snapshot's real dimensions (`jpeg_size`), because the
  snapshot may be the sub-stream frame rather than the main stream's size;
- announces a full-frame image (`smartDetectSnapshotFullFoV` and its size) so the
  event also gets `thumbnailFullfovId`, like a real camera's.

## PTZ cameras (`--ptz`)

Protect shows the joystick only for a camera that declares a real PTZ model
(`--sysid 0xa604 --platform CV25Z …`, "UVC AI PTZ Industrial"). Notes from testing
on a Reolink E1 Zoom:

- `ContinuousMove` arrives with `responseExpected: false` and must be on the
  dispatcher's allow-list or it is silently dropped; pan/tilt and zoom arrive as
  separate messages.
- Reolink has no speed-only update, so a changed speed re-sends the op.
- Keep one HTTPS connection to the camera. A fresh TLS handshake per command made
  each `PtzCtrl` take ~420 ms against ~100 ms reused, so stops landed late and quick
  jogs overshot.
- Do not adopt the same camera twice (once directly, once through the proxy): Reolink
  Wi-Fi cameras cap concurrent video sessions and drop off.
- **Home View** is Protect's reserved preset slot `-1`. At connect Protect asks for
  `GetCurrentPosition` to build it and needs `steps` in the reply (Reolink has no
  position readback, so a fixed origin is reported). Going home sends a `Preset`
  message with `action: go, index: -1`; the proxy drives the camera to its own
  preset (`--ptz-home-preset`, default 0, the camera's "default") with `PtzCtrl ToPos`.
  Setting the home preset in Protect does not move the camera.

## Spotlight (`--spotlight`)

Protect models a camera's light as `featureFlags.hasFlash` ("internal flash LED") with
`flashRange`; the short keys on the wire are `flash` and `flashRange`. A camera that
advertises it gets Protect's light action (Alarm Manager "turn on light"), which sends
the avclient message `TurnOnColorNightVision` with `spotlightDuration` in seconds and
expects the light to turn itself off afterwards. With `--spotlight` the proxy advertises
the flag and, on that message, switches the Reolink's white LED on (`SetWhiteLed`,
`state` 1, optionally `bright` from `--spotlight-brightness`) and off again after the
duration. A request without one uses the value of Protect's Enhancer slider "Turn off spotlight after no motion for" (`spotlightDuration`, 5 to 900 s, sent in `ChangeIspSettings`), then `--spotlight-default-seconds`. Protect's Color Night Vision toggle (`colorNightVision` in `ChangeIspSettings`) is the on/off for the camera's own automatic spotlight: on writes `WhiteLed.mode` 1 (night mode, the camera's schedule and person/vehicle/pet triggers), off writes mode 0. Protect pushes its settings on every connect, so Protect's value always wins over the Reolink app, and only `mode` is written. The Reolink has no "off after no motion" setting, so the slider cannot time the automatic light. Only `state` and
`bright` are written, so the camera's own mode, schedule and AI triggers stay as set.
A newer request replaces the pending switch-off, and shutdown turns the light off.
Not yet verified end to end from Protect's UI.

## Real detection boxes (`--ai-sidecar`)

Push events only say "a person is present", so by default an event uses a centred
placeholder box and crop. Recent Reolink cameras also send the boxes the app draws,
inside the headers of the video frames on the Baichuan port (9000). `deploy/ai-sidecar`
reads them (nodelink-js for transport, our own TLV decoder) and `--ai-sidecar` runs it
as a subprocess of the proxy, so the standby replica never opens a session.

Measured on Reolink doorbells:

- the boxes are on the **main** stream (every frame); the **sub** stream carries only an
  8-byte counter. `--ai-sidecar-profile` therefore defaults to `main`;
- the main stream costs about 4.5 Mbit/s, ~125 MiB and ~56 m CPU for the Node process,
  so by default the stream is **on-demand**: the sidecar polls the proxy's loopback
  `/state` and opens it only while a detection is in progress (plus a few seconds), then
  closes it. `--ai-sidecar-always-on` keeps it open and lets boxes start/stop events;
- in on-demand mode the push channel starts and ends events and the boxes only *enrich*
  the running event (moving boxes, per-object crops); a missing or failing sidecar leaves
  push-only behaviour unchanged;
- the box setting in the Reolink app made no difference to which stream carries them.

The image bundles Node and the sidecar (`Dockerfile.vaapi`). `--ai-sink-port` defaults to loopback
port 8099 when `--ai-sidecar` is used. The E1 Zoom's boxes were not probed, so it does not use the sidecar.

## Firmware notices

Protect compares the firmware a camera *declares* with the newest version in its own catalog
for that model's platform and shows "update available" when the camera is behind. A proxy has
nothing to flash, so declare exactly the catalog's newest (`--fw-version`; the catalog is the
`updates` table, for example `select platform, version from updates`). Do **not** declare a higher
number: Protect treats a version above its catalog as invalid ("Force updating ... for satisfying the
version requirement"), pushes an `UpdateFirmwareRequest` on every connect, the proxy declines it and
reconnects, and the camera flaps about every 11 seconds. The notice returns when Ubiquiti ships a newer
release for that platform. Cameras that declare no model (`--sysid`) have no catalog entry and show none.
