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
// Which Baichuan stream to read the boxes from. Measured on Reolink doorbells: the MAIN
// stream carries them in every frame's extended header and the SUB stream carries none
// (an 8-byte counter only), so doorbells need PROFILE=main. The older floodlight used sub.
const PROFILE = process.env.PROFILE || "sub";
// On-demand mode: when STATE_URL is set, the stream (about 4.5 Mbit/s for a doorbell's
// main stream) is only open while the proxy reports a detection in progress, plus a short
// linger so the proxy sees the boxes disappear. Without it the stream is always on.
const STATE_URL = process.env.STATE_URL || "";
const STATE_POLL_MS = Number(process.env.STATE_POLL_MS || 400);
const LINGER_MS = Number(process.env.LINGER_MS || 4000);

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

// MEASURED on this firmware (Elite Floodlight, YOLO-World generation):
// nodelink's (type1=class, type2=view) model is REVERSED here — every
// copy of one physical box shares one type2 and that type2 IS the
// class (1=people, 2=vehicle, 3=animal, matching the SDK enum), while
// type1 is the view index (the SDK's "3 views x 4 class arrays"; view
// lengths 10/14/13 explain the length-by-wrapper illusion). Verified
// live: dog box t2=3, its human t2=1, the static far house t2=2.
const T2_CLASS = { 1: "people", 2: "vehicle", 3: "animal" };
let lastCopyLog = 0;

function mapEvent(event) {
  const { copies, frameWidth, frameHeight } = decodeCopies(event.rawHeader);
  if (copies.length === 0) return mapNodelinkBoxes(event);

  // Group the per-class wrapper copies of each physical box and pick
  // the label by highest per-copy confidence.
  const groups = new Map();
  const odd = [];
  for (const c of copies) {
    if (c.odd) {
      odd.push(c);
      continue;
    }
    if (c.x2 > frameWidth || c.y2 > frameHeight || c.x2 <= c.x1 || c.y2 <= c.y1)
      continue;
    const key = `${c.x1}_${c.y1}_${c.x2}_${c.y2}`;
    (groups.get(key) ?? groups.set(key, []).get(key)).push(c);
  }

  if (Date.now() - lastCopyLog > 1000 && groups.size) {
    lastCopyLog = Date.now();
    for (const [key, g] of groups) {
      const detail = g
        .map(
          (c) =>
            `${c.label}/t2=${c.t2}@${c.conf}(len${c.len}${c.extras ? ",x=" + c.extras : ""})`,
        )
        .join(" ");
      console.log(`copies [${key}]: ${detail}`);
    }
    for (const o of odd.slice(0, 8)) {
      console.log(`odd-tlv t1=${o.t1} t2=${o.t2} t=${o.t} len=${o.len} ${o.hex}`);
    }
  }

  const boxes = [];
  for (const g of groups.values()) {
    const best = g.reduce((a, b) => (b.conf > a.conf ? b : a));
    const w = best.x2 - best.x1;
    const h = best.y2 - best.y1;
    let type = LABELS[T2_CLASS[best.t2]];
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
let posting = !STATE_URL;
async function postOnce() {
  try {
    await fetch(SINK, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ boxes: latest }),
    });
  } catch (e) {
    console.error("sink post failed:", e.message);
  }
}
setInterval(() => {
  if (posting) void postOnce();
}, INTERVAL_MS);

async function detectionActive() {
  try {
    const r = await fetch(STATE_URL);
    return (await r.json()).active === true;
  } catch {
    return false;
  }
}

async function session() {
  const api = new ReolinkBaichuanApi({
    host: HOST,
    username: USER,
    password: PASS,
    logger: console,
  });
  const handler = (event) => {
    latest = mapEvent(event);
  };
  const opts = { channel: 0, profile: PROFILE };
  let streaming = false;
  try {
    await api.login();
    console.log(`baichuan login OK; profile=${PROFILE} on-demand=${Boolean(STATE_URL)}`);
    if (!STATE_URL) {
      await api.onObjectDetections(handler, opts);
      streaming = true;
    }
    let lastActive = 0;
    let lastPing = Date.now();
    for (;;) {
      if (STATE_URL) {
        const now = Date.now();
        const active = await detectionActive();
        if (active) lastActive = now;
        if (active && !streaming) {
          await api.onObjectDetections(handler, opts);
          streaming = true;
          posting = true;
          console.log("stream on");
        } else if (!active && streaming && now - lastActive > LINGER_MS) {
          await api.offObjectDetections(handler, opts);
          streaming = false;
          latest = [];
          await postOnce(); // final empty report so the proxy can end the event
          posting = false;
          console.log("stream off");
        }
      }
      // Liveness: probe the connection instead of waiting on events.
      if (Date.now() - lastPing > 30000) {
        await api.ping();
        lastPing = Date.now();
      }
      await new Promise((r) => setTimeout(r, STATE_URL ? STATE_POLL_MS : 1000));
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
