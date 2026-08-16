"""Translate ffmpeg's enhanced-FLV HEVC into UniFi Protect's HEVC framing.

ffmpeg >= 6.1 muxes HEVC into FLV as Enhanced-RTMP extended video tags
(bit 7 of the first body byte set, fourcc "hvc1"). Protect's ingest does
not parse those: real UniFi cameras carry HEVC as legacy-shaped video
tags under FLV codec id 8 — config as a frame-type-6 tag holding the
decoder configuration, frames as key/inter tags with 4-byte
length-prefixed NALs. Format measured against a real G5 PTZ and proven
to record on Protect 7.1.77 by rjmotion/finch + pyunifiwire; this filter
mirrors finch's writer exactly.

Video bytes are passed through untouched (no decode/encode) — only tag
header bytes are rewritten, so CPU cost is negligible. Audio and script
tags pass through as-is (videocodecid in onMetaData is patched to 8).

Sits between ffmpeg and clock_sync (which adds UniFi's 16-byte inter-tag
wall-clock trailers and the 0x07 header flags):

    ffmpeg ... -f flv - | python -m unifi.hevc_flv \
        | python -m unifi.clock_sync | nc <nvr> 7550
"""

import struct
import sys

TAG_HEADER_LEN = 11
PREV_SIZE_LEN = 4

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18

CODEC_H265 = 8  # Ubiquiti's FLV codec id for HEVC

FRAME_KEY = 1
FRAME_INTER = 2
FRAME_SEQUENCE_HEADER = 6

PACKET_SEQUENCE_HEADER = 0
PACKET_NALU = 1
PACKET_END = 2

# Enhanced-RTMP packet types (low nibble when bit 7 of byte 0 is set)
EX_SEQUENCE_START = 0
EX_CODED_FRAMES = 1
EX_SEQUENCE_END = 2
EX_CODED_FRAMES_X = 3


def read_exact(source, count):
    buf = b""
    while len(buf) < count:
        chunk = source.read(count - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def _hvcc_parameter_sets(record: bytes):
    """Extract (VPS, SPS, PPS) from an HEVCDecoderConfigurationRecord.

    ISO/IEC 14496-15: 22 fixed bytes, then numOfArrays of
    [nal_type, count, [len16, NAL]...]. Returns None when incomplete.
    """
    if len(record) <= 22:
        return None
    found = {}
    cursor = 22
    arrays = record[cursor]
    cursor += 1
    for _ in range(arrays):
        if cursor + 3 > len(record):
            break
        kind = record[cursor] & 0x3F
        count = int.from_bytes(record[cursor + 1: cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            if cursor + 2 > len(record):
                break
            size = int.from_bytes(record[cursor: cursor + 2], "big")
            cursor += 2
            unit = record[cursor: cursor + size]
            cursor += size
            if len(unit) == size and kind not in found:
                found[kind] = unit
    if all(k in found for k in (32, 33, 34)):  # VPS, SPS, PPS
        return found[32], found[33], found[34]
    return None


def _build_config(sets) -> bytes:
    # Real-camera config shape: byte0=0x68, byte1=1, then bare 2-byte
    # length-prefixed VPS/SPS/PPS, no composition field (pyunifiwire
    # hevc.parameter_sets, measured on a real UVC). An hvcC record here
    # makes Protect sniff the codec as VUNK.
    out = bytearray([(FRAME_SEQUENCE_HEADER << 4) | CODEC_H265, 1])
    for unit in sets:
        out += len(unit).to_bytes(2, "big") + unit
    return bytes(out)


def _inband_parameter_sets(payload: bytes):
    """Harvest (VPS, SPS, PPS) from 4-byte length-prefixed NAL units.

    After an encoder reconfigure the RTSP SDP can lack the parameter
    sets, leaving ffmpeg's hvcC empty — but HEVC keyframes carry them
    in band, so the config can be rebuilt from the first keyframe.
    """
    found = {}
    cur = 0
    while cur + 4 <= len(payload):
        size = int.from_bytes(payload[cur: cur + 4], "big")
        cur += 4
        if size <= 0 or cur + size > len(payload):
            break
        unit = payload[cur: cur + size]
        cur += size
        kind = (unit[0] >> 1) & 0x3F
        if kind in (32, 33, 34) and kind not in found:
            found[kind] = unit
    if all(k in found for k in (32, 33, 34)):
        return found[32], found[33], found[34]
    return None


def transform_video(body: bytes, state: dict):
    """Rewrite an Enhanced-RTMP hvc1 tag body to UniFi framing.

    Returns a LIST of bodies to emit in place of the tag (empty list =
    drop). A config tag can be injected ahead of a frame when the
    parameter sets had to be harvested in band.
    """
    if not body or not (body[0] & 0x80):
        return [body]  # legacy tag (e.g. h264) — not ours to touch
    frame_type = (body[0] >> 4) & 0x07
    packet_type = body[0] & 0x0F
    if body[1:5] != b"hvc1":
        return [body]
    rest = body[5:]
    if packet_type == EX_SEQUENCE_START:
        sets = _hvcc_parameter_sets(rest)
        if sets is None:
            # Empty/incomplete hvcC — drop it and rebuild the config
            # from the first keyframe's in-band NALs instead.
            return []
        state["config_done"] = True
        return [_build_config(sets)]
    ft = FRAME_KEY if frame_type == 1 else FRAME_INTER
    if packet_type == EX_CODED_FRAMES:
        ct, payload = rest[:3], rest[3:]
    elif packet_type == EX_CODED_FRAMES_X:
        ct, payload = b"\x00\x00\x00", rest
    elif packet_type == EX_SEQUENCE_END:
        return [bytes([(FRAME_KEY << 4) | CODEC_H265, PACKET_END, 0, 0, 0])]
    else:
        # Metadata (4), MPEG2-TS (5), multitrack (6): no UniFi equivalent.
        return []
    frame = bytes([(ft << 4) | CODEC_H265, PACKET_NALU]) + ct + payload
    if not state.get("config_done"):
        sets = _inband_parameter_sets(payload)
        if sets is not None:
            state["config_done"] = True
            return [_build_config(sets), frame]
    return [frame]


def _amf_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _amf_value(item) -> bytes:
    if isinstance(item, bool):
        return bytes([0x01, 1 if item else 0])
    if isinstance(item, (int, float)):
        return bytes([0x00]) + struct.pack(">d", float(item))
    return bytes([0x02]) + _amf_string(item)


def _extract_stream_name(body: bytes):
    marker = b"streamName"
    at = body.find(marker)
    if at < 0:
        return None
    cursor = at + len(marker)
    if cursor + 3 > len(body) or body[cursor] != 0x02:
        return None
    length = int.from_bytes(body[cursor + 1: cursor + 3], "big")
    value = body[cursor + 3: cursor + 3 + length]
    return value.decode("utf-8", errors="replace") if len(value) == length else None


def replace_metadata(body: bytes) -> bytes:
    """Swap ffmpeg's onMetaData for the vocabulary a real camera announces.

    Protect reads the camera's nine-key AMF0 OBJECT (0x03) — notably
    `extendedFormat: true`, which flags the codec-id-8 extended framing —
    while ffmpeg emits an ECMA array (0x08) full of keys the camera never
    sends. streamName is carried over; channelId/streamId are video1's
    (this filter only runs on the hi channel).
    """
    if b"onMetaData" not in body:
        return body
    name = _extract_stream_name(body)
    if name is None:
        return body
    fields = {
        "audioBandwidth": 32000.0,
        "audioChannels": 1.0,
        "audioFrequency": 32000.0,
        "channelId": 0.0,
        "extendedFormat": True,
        "hasAudio": True,
        "hasVideo": True,
        "streamId": 1.0,
        "streamName": name,
    }
    obj = bytearray([0x03])
    for key, item in fields.items():
        obj += _amf_string(key) + _amf_value(item)
    obj += _amf_string("") + bytes([0x09])
    return bytes([0x02]) + _amf_string("onMetaData") + bytes(obj)


def main() -> None:
    source = sys.stdin.buffer
    sink = sys.stdout.buffer
    state: dict = {"config_done": False}

    # FLV header (9 bytes) + first previous-tag-size (4): pass through.
    head = read_exact(source, 9 + PREV_SIZE_LEN)
    sink.write(head)
    if len(head) < 9 + PREV_SIZE_LEN or head[:3] != b"FLV":
        sink.flush()
        return

    while True:
        header = read_exact(source, TAG_HEADER_LEN)
        if len(header) < TAG_HEADER_LEN:
            sink.write(header)
            break
        size = int.from_bytes(header[1:4], "big")
        body = read_exact(source, size)
        prev = read_exact(source, PREV_SIZE_LEN)
        if len(body) < size:
            sink.write(header + body + prev)
            break

        kind = header[0]
        if kind == TAG_VIDEO:
            bodies = transform_video(body, state)
        elif kind == TAG_SCRIPT:
            bodies = [replace_metadata(body)]
        else:
            bodies = [body]

        for out_body in bodies:
            out = bytearray(header)
            out[1:4] = len(out_body).to_bytes(3, "big")
            sink.write(bytes(out))
            sink.write(out_body)
            sink.write(
                (TAG_HEADER_LEN + len(out_body)).to_bytes(PREV_SIZE_LEN, "big")
            )
        sink.flush()


if __name__ == "__main__":
    main()
