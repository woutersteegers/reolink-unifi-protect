from unifi.cams.rtsp import jpeg_size


def sof(width, height, marker=0xC0):
    body = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\0" * 9
    )
    app0 = b"\xff\xe0\x00\x10" + b"JFIF\0" + b"\0" * 9
    return b"\xff\xd8" + app0 + b"\xff" + bytes([marker]) + b"\x00\x11" + body


def test_reads_dimensions_past_other_segments(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(sof(640, 480))
    assert jpeg_size(p) == (640, 480)


def test_progressive_frames_are_read_too(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(sof(2560, 1920, marker=0xC2))
    assert jpeg_size(p) == (2560, 1920)


def test_garbage_and_missing_files_give_none(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"\0" * 64 + b"\xff\xd9")
    assert jpeg_size(p) is None
    assert jpeg_size(tmp_path / "missing.jpg") is None
