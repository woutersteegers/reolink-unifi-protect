---
sidebar_position: 1
---

# RTSP

Most generic cameras are supported via the RTSP integration.
Depending on your camera, you might need specific flags to make live-streaming smoother.
Check for your specific camera model in the docs before trying this.

```sh
unifi-cam-proxy -H {NVR IP} -i {Camera IP} -c /client.pem -t {Adoption token} \
  rtsp \
  -s {rtsp stream}
```

## Hardware Acceleration

```sg
unifi-cam-proxy -H {NVR IP} -i {Camera IP} -c /client.pem -t {Adoption token} \
  rtsp \
  -s {rtsp stream} \
  --ffmpeg-args='-hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format yuv420p'
```

## Bind the HTTP listeners

Use `--http-bind-address 127.0.0.1` with `--http-api` or
`--ai-sink-port` when the caller runs on the same host. These endpoints
do not authenticate callers. A bundled sidecar's detection input remains
loopback-only.
