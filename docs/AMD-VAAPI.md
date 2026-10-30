# AMD GPU encoding with VA-API

`--hw-backend vaapi` selects the Mesa VA-API backend for AMD GPUs on Linux.
Enable encoding separately with `--hw-transcode` or `--lo-h265-transcode`.
Without those flags, streams keep their configured passthrough/audio behavior.
The default backend remains Intel `qsv` for existing deployments.

This branch starts from main with the fixes from PRs #1–#3. It does not include
the pending HTTP, Debian runtime, or lower-stream scaling/audio options from
PRs #4–#6.

## Hardware and image

The development host is a Beelink SER5 with an AMD Ryzen 7 5825U, PCI device
`1002:15e7` (Barcelo). SER5 variants differ; inspect the device instead of
assuming every SER5 has the same codec support. The host must provide a working
`amdgpu` kernel driver and a DRM render node such as `/dev/dri/renderD128`.

`Dockerfile.vaapi` installs both Mesa's AMD video driver and the existing Intel
driver. It lets libva select the driver from the device. It no longer forces
`LIBVA_DRIVER_NAME=iHD` on AMD hardware. For AMD, an explicit container setting
of `LIBVA_DRIVER_NAME=radeonsi` is also supported. Remove any inherited `iHD`
override when using AMD. ROCm is not needed for this video path.

The Node build stage uses `node:22-bookworm-slim` so its executable remains
compatible with the Ubuntu 24.04 runtime's glibc. This does not change the
runtime to Debian or bring in PR #5's FFmpeg upgrade.

## High stream: H.264 encoding

Append these flags after the `rtsp` subcommand and the existing source options:

```text
--hw-backend vaapi
--hw-device /dev/dri/renderD128
--hw-transcode
--transcode-width 1280
--transcode-height 720
--transcode-bitrate 4M
--hi-fps 20
```

Set `--hi-fps` to the source's actual frame rate; the scaler does not change
frame rate. The high stream is decoded and scaled on the GPU and encoded with
`h264_vaapi`. Protect is advertised the encoded H.264 codec and output size,
even if the input camera stream has a different codec or dimensions. Lower
channels keep their own source streams and configured passthrough behavior.

The VA-API path uses NV12 (8-bit) surfaces, no B-frames, and approximately
one-second GOPs at the advertised frame rate. Audio is encoded to mono AAC at
16 kHz / 32 kbit/s. Audio encoding runs on the CPU.

GPU decoding uses one submission thread. Automatic decoder threading exhausted
FFmpeg 6.1's hardware-frame pools on the ten-vCPU test worker; limiting the
decoder threads passed the same sustained workload. This keeps decoding on
the GPU. See the [FFmpeg frame-pool discussion](https://ffmpeg.org/pipermail/ffmpeg-devel/2024-February/322169.html).

## Lower streams: H.265 encoding

To encode video2/video3 using the AMD GPU, add:

```text
--hw-backend vaapi
--lo-h265-transcode
--lo-transcode-bitrate 1M
```

The lower streams use `hevc_vaapi`, keep their source dimensions, and pass
through the existing UniFi HEVC FLV rewriter. Set `--lo-width`, `--lo-height`,
and `--lo-fps` to match the lower input streams. Their advertised codec becomes
H.265 automatically. This path also encodes audio to mono AAC at 16 kHz.

These options do not consolidate RTSP sessions or provide the pending
`--lo-scale` feature from #6. A shared RTSP relay is needed if multiple proxy
channels must share one upstream connection to a camera. Enabling H.265 still
requires a compatible Protect camera identity/capability configuration.

## Kubernetes device access

Expose a render device and allow the container user to open it. A standalone
validation pod can use the AMD device plugin:

```yaml
resources:
  limits:
    amd.com/gpu: 1
env:
  - name: LIBVA_DRIVER_NAME
    value: radeonsi
```

Schedule it on a node advertising that resource. A GPU request is reserved
even when a replica is on standby. With one advertised GPU per node, applying
this request to every camera/standby replica can leave pods unschedulable.
Some AMD device-plugin configurations also inject `/dev/kfd`; the pod cannot
start if that device is missing on the node, even though this VA-API path does
not need ROCm.

For a cluster deliberately sharing one video engine across several camera
pods, a [generic device plugin](https://github.com/squat/generic-device-plugin)
can advertise multiple `devic.es/amd-video` slots for one render device. A pod <!-- codespell:ignore devic -->
requests one slot with `resources.limits.devic.es/amd-video: 1`; the plugin <!-- codespell:ignore devic -->
injects the render device without KFD, hostPath, or a privileged application.
These slots limit admission, not GPU time, bandwidth, or memory. Standby
replicas reserve slots too. Configure the resource on every eligible node.

An existing shared-device arrangement can alternatively mount the node's
`/dev/dri`, provided its device policy permits access:

```yaml
volumeMounts:
  - name: dri
    mountPath: /dev/dri
# In the pod's volumes list:
volumes:
  - name: dri
    hostPath:
      path: /dev/dri
      type: Directory
```

The host/container device policy must also permit DRM device access. A
non-root container needs the node's render-group GID in its supplemental
groups. Select a GPU-capable node; a device path alone does not establish codec
support. These are configuration examples, not a deployment change.

## Validation without a camera

The Container check workflow builds the image without publishing it and checks
Python imports, Node and sidecar loading, the Mesa driver file, and FFmpeg's
VA-API encoders. This checks userspace, not physical GPU access.

On a GPU-enabled Linux container, run:

```sh
sh /app/scripts/vaapi-smoke-test.sh /dev/dri/renderD128
```

The script prints `vainfo`, creates short synthetic H.264 and HEVC clips,
hardware-decodes each, and exercises high-stream H.264 scaling/encoding and
lower-stream H.265 encoding. It probes and software-decodes each result. It
does not access cameras or Protect and removes its temporary files afterward.
Frame-rate, resolution and concurrent-session limits still depend on the GPU,
firmware and Mesa driver. This is not a performance benchmark or a validation
of every camera resolution.

Physical-camera/Protect integration and a staged rollout remain separate from
these checks. Keep the currently deployed image available for rollback.

### Development validation (2026-10-24)

- 170 Python tests and the repository's pre-commit checks passed.
- The amd64 runtime image built and passed Python, Node/sidecar, Mesa-file and
  FFmpeg encoder checks in CI.
- Synthetic hardware tests passed on the SER5's Ryzen 7 5825U using Ubuntu
  24.04, FFmpeg 6.1.1 and Mesa 25.2.8. Both H.264 and HEVC inputs produced
  ten valid frames through each output path: H.264 at 160x120 with scaling,
  and HEVC at 320x240. This does not establish 5K or multi-camera throughput.
- The nested worker's plugin allocation failed because `/dev/kfd` was missing;
  a regular `/dev/dri` mount was also blocked by its device policy. The
  successful synthetic check used a short-lived privileged test pod with the
  DRM device mounted. Production GPU access still needs a suitable device
  policy or a working device-plugin allocation. No production pod was changed.

References: [FFmpeg VA-API encoders](https://ffmpeg.org/ffmpeg-codecs.html#VAAPI-encoders),
[Debian Mesa driver files](https://packages.debian.org/trixie/amd64/mesa-va-drivers/filelist).

### Shared-device validation (2026-10-25)

- Four unprivileged video pods ran concurrently on each of three SER5 workers;
  all twelve H.264 output files decoded successfully. Compute allocations also
  opened their KFD/card/render devices on each worker.
- Sustained validation exposed FFmpeg 6.1 frame-pool exhaustion on a ten-vCPU
  worker. The AMD backend now uses one decoder submission thread; the same
  workload then passed on all three workers. Decoding remains on the GPU.
- This synthetic workload was 320x240 input scaled to 160x120 for 20 seconds.
  It does not establish full-resolution multi-camera capacity.
- A separate one-minute test read the actual PTZ camera on the repaired worker:
  H.264 VA-API output preserved 2560x1920 at 20 fps with AAC audio, and the whole
  output file decoded successfully.
- The SHA candidate was deployed to the existing PTZ proxy with unchanged
  identity, dimensions, and replica placement. Its hardware encoder stayed
  alive across a 90-second sample and made output progress. Protect reported
  Online/Excellent at 20 fps and 2560x1920; the owner confirmed that recordings
  from the candidate interval played normally. The previous deployment and
  Flux reconciliation were restored afterward. Protect's restart marker was
  expected; no camera re-adoption was needed.
- The existing API account sees only doorbells, so Protect checks used an
  existing authenticated app session. The CI observation checkpoint timed out
  while coordinating that review; hardware output and recording playback were
  verified separately rather than claiming the automated job passed.
