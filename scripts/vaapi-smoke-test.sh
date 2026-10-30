#!/bin/sh
# Synthetic GPU check only: no camera, credentials, or Protect connection.
set -eu

device=${1:-/dev/dri/renderD128}
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM

vainfo --display drm --device "$device"

for input_codec in h264 hevc; do
    if [ "$input_codec" = h264 ]; then
        ffmpeg -nostdin -hide_banner -loglevel error -y \
            -f lavfi -i testsrc2=size=320x240:rate=10 -frames:v 10 \
            -c:v libx264 -threads 1 -pix_fmt yuv420p "$work_dir/input.mp4"
    else
        ffmpeg -nostdin -hide_banner -loglevel error -y \
            -f lavfi -i testsrc2=size=320x240:rate=10 -frames:v 10 \
            -c:v libx265 -x265-params log-level=error:pools=1:frame-threads=1 \
            -pix_fmt yuv420p "$work_dir/input.mp4"
    fi
    for output_codec in h264 hevc; do
        filter=scale_vaapi=format=nv12
        if [ "$output_codec" = h264 ]; then
            filter=scale_vaapi=w=160:h=120:format=nv12
        fi
        ffmpeg -nostdin -hide_banner -loglevel error -y \
            -hwaccel vaapi -hwaccel_device "$device" -hwaccel_output_format vaapi -threads 1 \
            -i "$work_dir/input.mp4" -vf "$filter" \
            -c:v "${output_codec}_vaapi" -b:v 1M -g 10 -bf 0 -an \
            "$work_dir/output.mp4"
        echo "VA-API: $input_codec -> $output_codec"
        ffprobe -v error -select_streams v:0 \
            -show_entries stream=codec_name,width,height,nb_frames \
            -of json "$work_dir/output.mp4"
        # Decode every output frame to catch corrupt/empty output, not just headers.
        ffmpeg -nostdin -hide_banner -loglevel error \
            -xerror -i "$work_dir/output.mp4" -f null -
        frames=$(ffprobe -v error -select_streams v:0 -count_frames \
            -show_entries stream=nb_read_frames -of csv=p=0 "$work_dir/output.mp4")
        if [ "$frames" != 10 ]; then
            echo "Expected 10 decoded frames, got $frames" >&2
            exit 1
        fi
    done
done
