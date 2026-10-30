# Doorbells, push events and two-way audio

Everything here is opt-in through flags; without them the proxy behaves exactly
as before, so a single image serves doorbells and ordinary cameras.

## Push instead of polling

`--reolink-push-ai` and `--reolink-push-button` keep one long-lived connection
to the camera's Baichuan port (9000) using
[`reolink-aio`](https://github.com/starkillerOG/reolink_aio) in `bc_only` mode,
which never touches the camera's HTTPS API. Small cameras can have their web
server wedged by frequent polling, so this removes the main source of load.

- AI: the `AItype` of Baichuan `AlarmEvent` (cmd 33) drives the same detection
  state machine as the `GetAiState` poller (`people`, `vehicle`, `dog_cat`).
  The library only tracks AI states for keys that already exist (normally
  created by its HTTP startup), so the three keys are pre-seeded on
  `host._ai_detection_states[0]`. This is a private attribute: the dependency
  is pinned (`reolink-aio==0.21.18`) and should be re-checked on upgrade.
- Button: the `visitor` flag of the same event raises a ring once per press.
- A detection that goes quiet for 90 s is cleared, and a dropped connection
  clears an active one.
- Reconnects never stop: the cool-off doubles from 2 s to a 2-minute cap and
  resets after 5 minutes of stable connection.
- `--reolink-visitor-poll` (button) and `--reolink-ai-host` without
  `--reolink-push-ai` (AI) keep the polling behaviour.

## Presenting as a doorbell

Protect decides a camera is a doorbell from the camera's feature flags, which it
renames on ingest with short keys: `doorbell` -> `isDoorbell`, `speaker` ->
`hasSpeaker`, `lcdScreen`, `mic`, ... (`hasSpeaker` as a camera-side key is
ignored). `--doorbell` sends `doorbell`; `--speaker` sends `speaker`. Capability
changes generally need the camera removed and re-adopted with a fresh token;
the speaker flag was picked up on reconnect.

## A real ring

Protect has no camera-to-NVR "ring" message, and a row written straight to its
database skips its in-memory pipeline (no notification, no `lastRing`). A real
ring comes from the Access doorbell route:

    POST /proxy/protect/api/events/access/action
    {"action": "door_bell", "camera_id": "...", "start_time": ms, "end_time": ms}

It creates the event through Protect's store, so notifications, the call screen,
the thumbnail and the ring chip all work. It needs a normal login session
(`POST /api/auth/login` with matching `Origin`/`Referer` headers, then the
returned CSRF token and cookie); an Integration API key is rejected. Use a
dedicated local Protect user (see below). The proxy looks the camera up by its
MAC, so nothing is hard-coded.

### The Protect user

The account is configuration, not code: the proxy only reads
`--protect-ring-user` / `--protect-ring-password`, and without them it logs
"not ringing Protect" and carries on (events and talk still work, only the ring
is skipped). It should not be an admin. What it needs:

- A **local** UniFi OS account (username and password). The proxy performs a
  plain `/api/auth/login`, so cloud/SSO logins and MFA are not supported.
- **Protect access with write permission on the camera it rings for.** Protect's
  handler for the route above requires write on the camera (or the permission to
  edit device settings), and the camera must be adopted. Nothing else is
  needed: a user with write on only the doorbell also sees only that camera,
  which is all the proxy needs to find its own camera by MAC. (Read from
  Protect's code; verified with a user scoped to a single camera.)
- Permissions are granted per camera id, so a camera that is removed and
  re-adopted gets a new id and the user must be granted it again.
- Give each deployment its own credentials (for example from a Kubernetes
  secret). The proxy reads them at start, so restart it after rotating them.

## Two-way audio

With `--speaker`, Protect sends `ChangeTalkbackSettings` and then pushes the
talk audio over UDP to the address it has recorded for the camera (`--ip`), so
that address must reach the proxy (for example a load-balancer address that
selects the active pod). Each datagram is one raw ADTS AAC-LC frame (22.05 kHz
mono as negotiated), not RTP.

`--talkback-bridge` feeds those frames to ffmpeg (`-f aac` to 8 kHz G.711
mu-law) and sends the result over an ONVIF RTSP backchannel:
`DESCRIBE` with `Require: www.onvif.org/ver20/backchannel`, `SETUP` of the
`sendonly` PCMU track over interleaved TCP, `PLAY`, then 20 ms RTP packets. The
client is plain `asyncio` (`unifi/talkback.py`), including Digest and Basic
auth, and needs no separate service. The bridge starts on the first datagram and ends
about 3 s after the last.

Notes: Protect also calls `POST api/1.2/snapshot` on the recorded camera
address; pointing it at the proxy means those calls fail fast instead of
hitting the camera. If the camera's `Snap` endpoint hangs while streaming,
leave `--snapshot-url` unset so snapshots come from the RTSP stream.
