"""
Helper program to inject absolute wall clock time into FLV stream for recordings
"""

import argparse
import sys
import time

from flvlib3.astypes import FLVObject
from flvlib3.primitives import make_ui8, make_ui32
from flvlib3.tags import create_script_tag


def read_bytes(source, num_bytes):
    read_bytes = 0
    buf = b""
    while read_bytes < num_bytes:
        d_in = source.read(num_bytes - read_bytes)
        if d_in:
            read_bytes += len(d_in)
            buf += d_in
        else:
            return buf
    return buf


def write(data):
    sys.stdout.buffer.write(data)


def write_log(data):
    sys.stderr.buffer.write(f"{data}\n".encode())


VIDEO_RATE = 90_000
AUDIO_AND_METADATA_RATE = 11_025


def media_ticks(timestamp_ms, rate):
    return timestamp_ms * rate // 1000


def write_timestamp_trailer(rate, ticks):
    write(b"\0" + rate.to_bytes(3, "big") + bytes(8))
    write(make_ui32(ticks & 0xFFFFFFFF))


def main(args):
    source = sys.stdin.buffer

    header = read_bytes(source, 3)

    if header != b"FLV":
        print("Not a valid FLV file")
        return
    write(header)

    # Skip rest of FLV header
    write(read_bytes(source, 1))
    read_bytes(source, 1)
    # Write custom bitmask for FLV type
    write(make_ui8(7))
    write(read_bytes(source, 4))

    # Tag 0 previous size
    write(read_bytes(source, 4))

    last_sync = None
    origin_ms = None
    while True:
        header = read_bytes(source, 11)
        if not header:
            return
        if len(header) != 11:
            raise EOFError("Incomplete FLV tag header")

        packet_type = header[0]
        payload_size = int.from_bytes(header[1:4], "big")
        payload = read_bytes(source, payload_size)
        previous_size = read_bytes(source, 4)
        if len(payload) != payload_size or len(previous_size) != 4:
            raise EOFError("Incomplete FLV tag")

        timestamp = int.from_bytes(header[7:8] + header[4:7], "big")
        if origin_ms is None:
            origin_ms = time.time_ns() // 1_000_000 - timestamp

        rate = VIDEO_RATE if packet_type == 9 else AUDIO_AND_METADATA_RATE
        is_video_picture = (packet_type == 9 and len(payload) >= 5
                          and (payload[0] & 15) in (7, 8) and payload[1] == 1)
        presentation_ms = timestamp
        if is_video_picture:
            presentation_ms += int.from_bytes(payload[2:5], "big", signed=True)
        ticks = media_ticks(presentation_ms, rate)

        should_sync = packet_type == 9 if last_sync is None else timestamp - last_sync >= 5000
        if should_sync:
            last_sync = timestamp
            metadata = FLVObject()
            metadata["streamClock"] = timestamp
            metadata["streamClockBase"] = 0
            metadata["wallClock"] = origin_ms + timestamp
            write(create_script_tag("onClockSync", metadata, timestamp))
            metadata_ticks = media_ticks(timestamp, AUDIO_AND_METADATA_RATE)
            write_timestamp_trailer(AUDIO_AND_METADATA_RATE, metadata_ticks)

            # Write mpma tag
            # {'cs': {'cur': 1500000.0,
            #         'max': 1500000.0,
            #         'min': 32000.0},
            #  'm': {'cur': 750000.0,
            #        'max': 1500000.0,
            #        'min': 750000.0},
            #  'r': 0.0,
            #  'sp': {'cur': 1500000.0,
            #         'max': 1500000.0,
            #         'min': 150000.0},
            #  't': 750000.0}

            data = FLVObject()
            data["cs"] = FLVObject()
            data["cs"]["cur"] = 1500000
            data["cs"]["max"] = 1500000
            data["cs"]["min"] = 1500000

            data["m"] = FLVObject()
            data["m"]["cur"] = 1500000
            data["m"]["max"] = 1500000
            data["m"]["min"] = 1500000
            data["r"] = 0

            data["sp"] = FLVObject()
            data["sp"]["cur"] = 1500000
            data["sp"]["max"] = 1500000
            data["sp"]["min"] = 1500000
            data["t"] = 75000.0
            packet_to_inject = create_script_tag("onMpma", data, 0)

            write(packet_to_inject)

            write_timestamp_trailer(AUDIO_AND_METADATA_RATE, metadata_ticks)

        write(header)
        write(payload)
        write(previous_size)
        write_timestamp_trailer(rate, ticks)



def parse_args():
    parser = argparse.ArgumentParser(description="Modify Protect FLV stream")
    parser.add_argument(
        "--write-timestamps",
        action="store_true",
        help="Indicates we should write timestamp in between packets",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())
