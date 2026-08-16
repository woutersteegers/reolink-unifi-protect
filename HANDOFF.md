# HANDOFF — Reolink Elite Floodlight → UniFi Protect via this fork

**For the next Claude session, working in this repo.** Everything below was
established empirically on 2026-08-15 (see `git log` — every finding is also
in commit messages/comments). The human is Wouter; his infra details follow.

## Goal (current plan, chosen by Wouter)

Feed the camera into UniFi Protect with **NO transcoding at all**:
- **Hi stream (video1): the camera's native H.265 main stream, passed through**
  (`-c:v copy`), honestly declared to Protect as `h265` via `--hi-codec h265`.
  This relies on Protect's **"enhanced" (H.265) codec support — experimental,
  per Wouter**. UNVERIFIED: earlier the live view spun on H.265, but at that
  point the proxy was DECLARING h264 while sending HEVC bytes (upstream
  hardcodes `"type": "h264"` on every channel — that was the bug). Whether
  Protect plays an honestly-declared h265 channel is the open experiment.
- **Medium/Low streams (video2/video3): the camera's native H.264 sub-stream**
  (1920×576@30, `-c:v copy`). Always works; this is also the fallback if the
  H.265 experiment fails (then the camera is simply an HD camera in Protect).

## Infrastructure

- **NAS**: UGREEN, Intel i5-1235U (Iris Xe iGPU), Unraid 7.2.3, `root@192.168.1.136`
  (passwordless SSH from this Mac). Deploy dir was `/mnt/user/appdata/unifi-cam-proxy`
  — **it was DELETED during cleanup** (contained repo copy + `deploy/.env` +
  `client.pem`). Re-rsync + regenerate before running (see Runbook).
- **UniFi Protect NVR**: `192.168.1.1` (UDM-family). The proxied camera
  ("Floodlight") may still exist there as a ghost — remove it in Protect before
  re-adopting.
- **Camera**: Reolink Elite Floodlight WiFi, `192.168.1.129` (static), user
  `admin`, password = ask Wouter (alphanumeric-with-hyphens; also present in
  this session's history). **HTTPS-only** camera API (HTTP returns nginx 302 —
  this crashes `reolinkapi`; that's why we use the `rtsp` source, not the
  `reolink` source). RTSP enabled.
  - Main stream `…:554/h264Preview_01_main`: **HEVC 5120×1552** (~20fps).
    (Camera app offers only 5120×1552 or 4096×1248 — dual-lens 180° panoramic.)
  - Sub stream `…:554/h264Preview_01_sub`: **H.264 1920×576@30**.
- **Scrypted also runs on the NAS** (separate, working: camera native via
  `@apocaliss92/scrypted-reolink-native` → HomeKit; motion works; `/dev/dri`
  added; hw accel = VAAPI there, NOT QSV — legacy MSDK in that image).

## What this fork already has (all committed)

1. **Deps fixed for py3.12**: `pyunifiprotect`→`uiprotect` (main.py import),
   `websockets<13` pin (v14 dropped `extra_headers` used by core.py).
2. **`Dockerfile.vaapi`**: Ubuntu 24.04 + `intel-media-va-driver-non-free` +
   `libvpl2 libmfx-gen1.2` (oneVPL runtime — QSV works only with this).
   Build ON THE NAS: `docker build -f Dockerfile.vaapi -t unifi-cam-proxy:vaapi .`
   (~10 min if apt layer invalidated, <1 min for py-only changes).
3. **QSV hardware transcode in base.py** (`--hw-transcode`, hi-stream-only):
   `-hwaccel qsv` decode → `vpp_qsv=w=2560:h=776` → `h264_qsv`. Proven stable
   on the real stream. NOT needed for the current no-transcode plan, but kept.
4. **Per-channel honest advertisement** (`fb6f1ad`): `--hi-codec h264|h265`,
   `--hi-width/--hi-height`, `--lo-codec/--lo-width/--lo-height` — wired into
   the adoption payload (video1 = hi_*, video2/video3 = lo_*).
5. **`deploy/docker-compose.yml` + `deploy/.env.example`** — note these still
   describe the OLD reolink-source + VAAPI plan; update them to the current
   rtsp-source plan when finalizing.

## Hard-won findings (do not re-learn these)

- **Protect can't play a stream whose declared codec ≠ actual codec.** Snapshot
  works (ffmpeg-side), live spins. Upstream hardcodes h264.
- **Protect caches camera capabilities at ADOPTION.** Changing advertised
  resolution/codec requires REMOVE in Protect + fresh adopt (new token).
- **Adoption tokens live 60 min**, single-ish use:
  `https://192.168.1.1/proxy/protect/api/cameras/manage-payload` (logged in,
  copy `token`). Wouter fetches these on request. Don't start long builds with
  a live token waiting.
- **iGPU limits (i5-1235U)**: H.264 encode max width **4096** (5120 impossible);
  HEVC decode 1.12×; 3840-encode 0.66× (too slow); 2560-encode 1.02× (ok);
  1920 1.09×. VAAPI here fails `Cannot allocate memory` — **use QSV**; scaler
  must be `vpp_qsv` (`scale_qsv` fails to configure input pad). Do NOT pin
  `-c:v hevc_qsv` as decoder — breaks on H.264 inputs (`Function not
  implemented`); plain `-hwaccel qsv` auto-picks.
- **Protect opens MULTIPLE channels simultaneously** — the iGPU fits exactly
  one 2560 transcode; two → respawn loop. Hence hi-only transcode gating
  (`_hw_transcoding`).
- **The `rtsp` source maps `-s URL1 URL2` → video1=URL1, video2/3=URL2.**
- The proxy's emulated profile also caps **audio/mic** (no mic in Protect) and
  smart detections; resolution/codec are now configurable, mic is not.

## Runbook (fresh deploy)

```bash
# 1. Sync repo to NAS (never sync .env/.pem/.git)
rsync -az --delete --exclude '.git' --exclude 'run' --exclude 'deploy/.env' \
  --exclude '*.pem' --exclude 'build.log' \
  ~/code/unifi-cam-proxy/ root@192.168.1.136:/mnt/user/appdata/unifi-cam-proxy/

# 2. Cert (regenerate — old one was deleted; camera must re-adopt anyway)
ssh root@192.168.1.136 'cd /mnt/user/appdata/unifi-cam-proxy/deploy && \
  openssl ecparam -out /tmp/pk.key -name prime256v1 -genkey -noout && \
  openssl req -new -sha256 -key /tmp/pk.key -out /tmp/s.csr -subj "/C=TW/L=Taipei/O=Ubiquiti Networks Inc./OU=devint/CN=camera.ubnt.dev/emailAddress=support@ubnt.com" && \
  openssl x509 -req -sha256 -days 36500 -in /tmp/s.csr -signkey /tmp/pk.key -out /tmp/pub.key && \
  cat /tmp/pk.key /tmp/pub.key > client.pem && rm -f /tmp/pk.key /tmp/pub.key /tmp/s.csr'

# 3. Build on the NAS
ssh root@192.168.1.136 'cd /mnt/user/appdata/unifi-cam-proxy && \
  docker build -f Dockerfile.vaapi -t unifi-cam-proxy:vaapi . > build.log 2>&1 && echo OK'

# 4. Remove ghost camera in Protect UI, get fresh token from Wouter, then:
ssh root@192.168.1.136 bash -s <<'EOF'
docker rm -f reolink-floodlight-proxy 2>/dev/null
docker run -d --name reolink-floodlight-proxy --restart unless-stopped \
  -v /mnt/user/appdata/unifi-cam-proxy/deploy/client.pem:/client.pem:ro \
  unifi-cam-proxy:vaapi \
  unifi-cam-proxy --host 192.168.1.1 --cert /client.pem \
    --token <FRESH_TOKEN> --mac AABBCC00C129 \
    --ip 192.168.1.129 --name Floodlight \
    rtsp \
      -s "rtsp://admin:<CAMERA_PASSWORD>@192.168.1.129:554/h264Preview_01_main" \
         "rtsp://admin:<CAMERA_PASSWORD>@192.168.1.129:554/h264Preview_01_sub" \
    --hi-codec h265 --hi-width 5120 --hi-height 1552 \
    --lo-width 1920 --lo-height 576
EOF
# (no --hw-transcode: pure passthrough plan; ffmpeg-args default is -c:v copy)
```

**Verify**: `docker logs` — adoption OK = many `Processing [...]` messages and
zero `no close frame`. Streams: `Spawning ffmpeg for video1` should carry the
`_main` URL + `-c:v copy`; video2/3 the `_sub` URL. Then Wouter checks Protect:
does live view play on the h265 channel? (He may need to enable the
experimental "enhanced codec" option in Protect settings if it exists there.)

**If H.265 hi fails** → fallback: re-adopt with `--hi-codec h264` and BOTH
sources set to the sub URL (or main-transcoded via `--hw-transcode`,
`--transcode-width 2560 --transcode-height 776`, add
`--device /dev/dri:/dev/dri --group-add video` to docker run).

## Open items

- The `deploy/` compose still reflects the old plan — refresh it once the
  final configuration is settled, so the container is compose-managed
  (`restart: unless-stopped` survives NAS reboots either way).
- Protect-side: ghost "Floodlight" may need removal; "enhanced codec"
  (H.265) toggle location/behavior in Wouter's Protect version is unverified.
- Never commit: `client.pem`, `deploy/.env`, camera password, adoption tokens.
