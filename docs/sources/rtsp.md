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

## Basic motion

Install the optional motion dependencies (`pip install '.[motion]'` from the
repository), then add `--detect-motion` after `rtsp`. Enable ordinary motion
and draw motion zones for the virtual camera in Protect. The detector reads
the last configured RTSP source at 4 fps and measures foreground movement
inside each enabled polygon. Each zone has independent start and stop timing.
Its sensitivity value controls the minimum foreground area (50 uses 90 pixels
at 320×180). This is an approximate mapping, not UniFi's detector algorithm.

Basic motion uses native `EventSmartMotion` start/stop messages independently
of Person, Vehicle and Animal events. Heatmaps are not currently generated.
