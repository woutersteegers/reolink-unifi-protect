# Debian 13 / FFmpeg 7 migration

Native installs require Python **3.12.4 or newer** because reolinkapi >=0.4.1 requires it. The Debian 13 image uses Python 3.13 and FFmpeg 7.1. Container CI builds the image and checks Python dependencies, Node/sidecar loading, Mesa and encoder availability; it does not establish hardware compatibility.

Some Reolink firmware has audio timestamps that cause copied AAC to delay video in FFmpeg 7 FLV output until Protect times out. Before upgrading a passthrough deployment, use:

```text
--ffmpeg-args "-c:v copy -ar 16000 -ac 1 -c:a aac -b:a 32k"
```

This copies video and re-encodes audio on the CPU; it does not enable GPU encoding. Test live video, recordings and audio with the camera before wider rollout. Existing custom arguments are not silently rewritten.

For lower-channel hardware transcoding, The proxy provides `--lo-audio-args "-ar 16000 -ac 1 -c:a aac -b:a 32k"`. Supply it with `--lo-h265-transcode`; it does not affect passthrough or the high channel. Use the global arguments above for copied-video channels. `--no-audio` disables audio entirely and is a fallback, not the normal migration setting.

The image includes Intel iHD and AMD Mesa drivers. Driver selection is automatic: neither the image nor proxy forces iHD. Use `--vaapi-driver iHD` for an explicit Intel override or `--vaapi-driver radeonsi` for AMD, only when required. An existing `LIBVA_DRIVER_NAME` environment override is respected; the flag overrides it only in streaming subprocesses. `--hw-backend qsv` selects Intel QSV; `--hw-backend vaapi` supports AMD with Mesa and an accessible render device. Backend/driver selection alone does not enable encoding; the transcode flags do. See [AMD VA-API](AMD-VAAPI.md).

## Shared relay and optional lower-channel scaling

Scaling changes output resolution; each proxy output still has its own FFmpeg input. For one upstream camera RTSP connection, use a shared relay such as go2rtc:

```yaml
streams:
  camera_main:
    - rtsp://USER:PASSWORD@CAMERA:554/h264Preview_01_main
```

Point every proxy source at that same named relay stream:

```text
rtsp --source rtsp://RELAY:8554/camera_main rtsp://RELAY:8554/camera_main rtsp://RELAY:8554/camera_main \
  --lo-h265-transcode --lo-scale --lo-width 640 --lo-height 480 \
  --hw-backend vaapi --ffmpeg-args "-c:v copy -ar 16000 -ac 1 -c:a aac -b:a 32k" \
  --lo-audio-args "-ar 16000 -ac 1 -c:a aac -b:a 32k"
```

Set high-channel codec/dimensions/FPS to the actual main stream and lower FPS to the input cadence. QSV uses Intel `vpp_qsv`/`hevc_qsv`; VA-API uses `scale_vaapi`/`hevc_vaapi` (AMD requires Mesa and device access). Neither scaling nor device passthrough shares inputs. Prefer an HTTP snapshot endpoint if avoiding extra camera RTSP sessions matters: the FFmpeg snapshot fallback can open another input. Protect output pipelines and relay client sessions remain separate even though the relay shares its upstream stream.
