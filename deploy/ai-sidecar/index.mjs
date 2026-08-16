// Reolink AI box sidecar: subscribes to the camera's own detection
// rectangles over the Baichuan protocol (the same data the Reolink app
// overlays) and forwards them to the unifi-cam-proxy sink as a steady
// heartbeat, mapped to Protect's 0-1000 [x, y, w, h] space.
import { ReolinkBaichuanApi } from "@apocaliss92/nodelink-js";
import { decodeCopies } from "./decode.mjs";

const HOST = process.env.CAM_IP;
const USER = process.env.CAM_USER || "admin";
const PASS = process.env.CAM_PASS;
const SINK = process.env.SINK_URL;
const INTERVAL_MS = Number(process.env.POST_INTERVAL_MS || 400);

const LABELS = {
  person: "person",
  people: "person",
  vehicle: "vehicle",
  animal: "animal",
  dog_cat: "animal",
};

// Per-model wire-class correction. nodelink's type1 mapping was
// verified on an E1 Zoom; the Elite Floodlight WiFi delivers PERSON
// boxes on the slot nodelink labels "animal" (a walking human logged
// as animal@94% in a 1:4.5 tall box while the Reolink app said
// person). Format: "from=to,from=to".
const REMAP = Object.fromEntries(
  (process.env.LABEL_REMAP || "")
    .split(",")
    .filter((s) => s.includes("="))
    .map((s) => s.split("=").map((x) => x.trim())),
);

let latest = [];

// Ground truth measured on this firmware (Elite Floodlight, YOLO-World
// generation): a tracked object's box appears under ALL class wrappers
// with IDENTICAL confidence — the wrappers carry no class information.
// What does discriminate is geometry: a standing/walking human is tall
// (h ≳ 2w), a dog is wider than tall, a vehicle is much wider and much
// bigger. Arbitrate ambiguous multi-wrapper ties by shape; boxes that
// appear under a single wrapper keep that specific label.
function arbitrate(labels, w, h) {
  if (labels.size === 1) return [...labels][0];
  if (h >= 1.35 * w) return "people";
  if (w >= 2.0 * h && w >= 150) return "vehicle";
  return "animal";
}
let lastCopyLog = 0;

function mapEvent(event) {
  const { copies, frameWidth, frameHeight } = decodeCopies(event.rawHeader);
  if (copies.length === 0) return mapNodelinkBoxes(event);

  // Group the per-class wrapper copies of each physical box and pick
  // the label by highest per-copy confidence.
  const groups = new Map();
  for (const c of copies) {
    if (c.x2 > frameWidth || c.y2 > frameHeight || c.x2 <= c.x1 || c.y2 <= c.y1)
      continue;
    const key = `${c.x1}_${c.y1}_${c.x2}_${c.y2}`;
    (groups.get(key) ?? groups.set(key, []).get(key)).push(c);
  }

  if (Date.now() - lastCopyLog > 1000 && groups.size) {
    lastCopyLog = Date.now();
    for (const [key, g] of groups) {
      const detail = g
        .map((c) => `${c.label}@${c.conf}(len${c.len}${c.extras ? ",x=" + c.extras : ""})`)
        .join(" ");
      console.log(`copies [${key}]: ${detail}`);
    }
  }

  const boxes = [];
  for (const g of groups.values()) {
    const best = g.reduce((a, b) => (b.conf > a.conf ? b : a));
    const w = best.x2 - best.x1;
    const h = best.y2 - best.y1;
    const label = arbitrate(new Set(g.map((c) => c.label)), w, h);
    let type = LABELS[label];
    if (!type) continue;
    type = REMAP[type] || type;
    boxes.push({
      type,
      confidence: Math.min(best.conf, 100) / 100,
      coord: [
        Math.round((best.x1 / frameWidth) * 1000),
        Math.round((best.y1 / frameHeight) * 1000),
        Math.round((w / frameWidth) * 1000),
        Math.round((h / frameHeight) * 1000),
      ],
    });
  }
  return boxes;
}

// Fallback: nodelink's own (deduped) boxes, if our decoder finds nothing.
function mapNodelinkBoxes(event) {
  const boxes = [];
  for (const b of event.boxes) {
    let type = LABELS[(b.label || "").toLowerCase()];
    if (!type) continue;
    type = REMAP[type] || type;
    boxes.push({
      type,
      confidence: b.confidence ?? 0.8,
      coord: [
        Math.round(b.x * 1000),
        Math.round(b.y * 1000),
        Math.round(b.width * 1000),
        Math.round(b.height * 1000),
      ],
    });
  }
  return boxes;
}

// Steady heartbeat (empty reports included) so the proxy can time out
// an event when boxes disappear without needing its own poller.
setInterval(async () => {
  try {
    await fetch(SINK, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ boxes: latest }),
    });
  } catch (e) {
    console.error("sink post failed:", e.message);
  }
}, INTERVAL_MS);

async function session() {
  const api = new ReolinkBaichuanApi({
    host: HOST,
    username: USER,
    password: PASS,
    logger: console,
  });
  try {
    await api.login();
    console.log("baichuan login OK; subscribing to detection boxes");
    await api.onObjectDetections(
      (event) => {
        latest = mapEvent(event);
      },
      { channel: 0, profile: "sub" },
    );
    // Liveness: detection events only flow while objects are visible,
    // so probe the connection instead of waiting on events.
    for (;;) {
      await new Promise((r) => setTimeout(r, 30000));
      await api.ping();
    }
  } finally {
    try {
      await api.close();
    } catch {}
  }
}

for (;;) {
  try {
    await session();
  } catch (e) {
    console.error("baichuan session ended:", e.message);
  }
  latest = [];
  await new Promise((r) => setTimeout(r, 5000));
}
