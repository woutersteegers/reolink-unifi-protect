// Reolink AI box sidecar: subscribes to the camera's own detection
// rectangles over the Baichuan protocol (the same data the Reolink app
// overlays) and forwards them to the unifi-cam-proxy sink as a steady
// heartbeat, mapped to Protect's 0-1000 [x, y, w, h] space.
import { ReolinkBaichuanApi } from "@apocaliss92/nodelink-js";

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

function mapEvent(event) {
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
