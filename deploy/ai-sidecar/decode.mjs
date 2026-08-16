// Copy-preserving decoder for Reolink's AI Mark additionalHeader TLVs.
//
// Ported from nodelink-js's detection.ts walker, with one crucial
// difference: nodelink dedupes the per-class wrapper copies (the camera
// emits the SAME box under people/vehicle/animal wrappers) and picks a
// winner by a fixed "specificity" rank where animal beats people — an
// E1 Zoom heuristic that mislabels humans as animals on the Elite
// Floodlight. We keep ALL copies so the caller can pick by per-copy
// confidence and log the class-specific extra bytes for analysis.
import lz4 from "lz4js";

const LZ4F = [0x04, 0x22, 0x4d, 0x18];
const FRAME_SIZE_TLV = [0x03, 0x04, 0x00];

function type1ToLabel(t) {
  return t === 1 ? "people" : t === 2 ? "vehicle" : t === 3 ? "animal" : "unknown";
}

function hasMarker(buf, off) {
  return (
    buf.length >= off + 8 &&
    buf[off] === 0xff &&
    buf[off + 2] === 0x00 &&
    buf[off + 3] === 0x01 &&
    buf[off + 4] === 0x0b &&
    buf[off + 5] === 0x00 &&
    buf[off + 6] === 0x01 &&
    buf[off + 7] === 0x08
  );
}

function walk(buf, pos, end, type1, type2, out) {
  while (pos + 3 <= end) {
    const t = buf[pos];
    const length = buf.readUInt16LE(pos + 1);
    const recordEnd = Math.min(pos + 3 + length, end);

    const isBox4 = t === 4 && (length === 10 || length === 13 || length === 14);
    const isBox2 = t === 2 && length === 10;
    // Capture leaf-sized TLVs we do NOT recognize as boxes: the SDK's
    // "view1 tracked-with-ID" records may use other type/length combos
    // that nodelink's box filter (and ours) silently skips.
    if (
      !(isBox4 || isBox2) &&
      type1 !== 0 &&
      type2 !== 0 &&
      length >= 8 &&
      length <= 24
    ) {
      out.push({
        odd: true,
        t1: type1,
        t2: type2,
        t,
        len: length,
        hex: buf.subarray(pos + 3, recordEnd).toString("hex"),
      });
    }
    if ((isBox4 || isBox2) && type1 !== 0 && type2 !== 0) {
      if (pos + 13 <= end) {
        out.push({
          label: type1ToLabel(type1),
          t1: type1,
          t2: type2,
          len: length,
          x1: buf.readUInt16LE(pos + 3),
          y1: buf.readUInt16LE(pos + 5),
          x2: buf.readUInt16LE(pos + 7),
          y2: buf.readUInt16LE(pos + 9),
          conf: buf.readUInt16LE(pos + 11),
          extras: buf.subarray(pos + 13, recordEnd).toString("hex"),
        });
      }
      pos = recordEnd;
      continue;
    }

    if (
      type1 === 255 &&
      type2 === 2 &&
      t === 2 &&
      length >= 4 &&
      buf[pos + 3] === LZ4F[0] &&
      buf[pos + 4] === LZ4F[1] &&
      buf[pos + 5] === LZ4F[2] &&
      buf[pos + 6] === LZ4F[3]
    ) {
      try {
        const dec = Buffer.from(
          lz4.decompress(buf.subarray(pos + 3, recordEnd), 256 * 1024),
        );
        walk(dec, 0, dec.length, 0, 0, out);
      } catch {}
      pos = recordEnd;
      continue;
    }

    if (length > 0) {
      let n1 = type1;
      let n2 = type2;
      if (type1 === 0) n1 = t;
      else if (type2 === 0) n2 = t;
      walk(buf, pos + 3, recordEnd, n1, n2, out);
    }
    pos = recordEnd;
  }
}

export function decodeCopies(rawHeader) {
  // I-frames carry an 8-byte prefix before the marker; P-frames don't.
  const off = hasMarker(rawHeader, 8) ? 8 : hasMarker(rawHeader, 0) ? 0 : -1;
  if (off < 0) return { copies: [], frameWidth: 896, frameHeight: 480 };
  const copies = [];
  walk(rawHeader, off, rawHeader.length, 0, 0, copies);

  let frameWidth = 896;
  let frameHeight = 480;
  for (let i = off + 8; i + 7 <= rawHeader.length; i++) {
    if (
      rawHeader[i] === FRAME_SIZE_TLV[0] &&
      rawHeader[i + 1] === FRAME_SIZE_TLV[1] &&
      rawHeader[i + 2] === FRAME_SIZE_TLV[2]
    ) {
      const w = rawHeader.readUInt16LE(i + 3);
      const h = rawHeader.readUInt16LE(i + 5);
      if (w >= 64 && w <= 8192 && h >= 64 && h <= 8192) {
        frameWidth = w;
        frameHeight = h;
        break;
      }
    }
  }
  return { copies, frameWidth, frameHeight };
}
