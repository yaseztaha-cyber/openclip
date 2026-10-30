import { readFileSync } from "fs";
import { join } from "path";

// Import the frontend's computeCrop and interpolateKeyframes
// We need to extract them from the editor page — for test, we duplicate the logic here
// and also test via importing the actual module if possible.
// For now, we test the logic directly by copying the canonical functions.

function clamp(v: number, lo: number, hi: number) { return Math.max(lo, Math.min(v, hi)); }

function computeCrop(
  vidW: number, vidH: number, zoom: number, cx: number, cy: number, arW: number, arH: number
) {
  let cropH = vidH / Math.max(zoom, 1e-6);
  let cropW = (cropH * arW) / arH;
  if (cropW > vidW) {
    cropW = vidW;
    cropH = (cropW * arH) / arW;
  }
  if (cropH > vidH) {
    cropH = vidH;
    cropW = (cropH * arW) / arH;
  }
  const even = (n: number) => Math.max(2, Math.round(n)) & ~1;
  let cw = even(cropW), ch = even(cropH);
  cw = Math.min(cw, Math.floor(vidW) & ~1 || 2);
  ch = Math.min(ch, Math.floor(vidH) & ~1 || 2);
  const slackX = vidW - cw;
  const slackY = vidH - ch;
  const fx = clamp(cx - cw / 2, 0, Math.max(slackX, 0));
  const fy = clamp(cy - ch / 2, 0, Math.max(slackY, 0));
  let ox = Math.round(fx) & ~1, oy = Math.round(fy) & ~1;
  ox = clamp(ox, 0, Math.max(Math.floor(vidW) - cw, 0));
  oy = clamp(oy, 0, Math.max(Math.floor(vidH) - ch, 0));
  if (ox + cw > vidW) ox = (Math.floor(vidW) - cw) & ~1;
  if (oy + ch > vidH) oy = (Math.floor(vidH) - ch) & ~1;
  return { w: cw, h: ch, x: Math.max(0, ox), y: Math.max(0, oy) };
}

function validateKeyframes(kfs: any[], duration: number) {
  const seen = new Map<number, any>();
  for (const kf of kfs) {
    const t = Math.max(0, Math.min(Number(kf.t) || 0, duration));
    const x = Math.max(0, Math.min(Number(kf.x) || 0.5, 1));
    const y = Math.max(0, Math.min(Number(kf.y) || 0.5, 1));
    const zoom = Math.max(0.5, Math.min(Number(kf.zoom) || 1, 3));
    seen.set(Math.round(t * 1000) / 1000, { t, x, y, zoom });
  }
  return Array.from(seen.values()).sort((a, b) => a.t - b.t);
}
function interpolateKeyframes(kfs: any[], t: number, duration: number) {
  const v = validateKeyframes(kfs, duration);
  if (v.length === 0) return { x: 0.5, y: 0.5, zoom: 1 };
  if (v.length === 1) return v[0];
  if (t <= v[0].t) return v[0];
  if (t >= v[v.length - 1].t) return v[v.length - 1];
  for (let i = 0; i < v.length - 1; i++) {
    const a = v[i], b = v[i + 1];
    if (t >= a.t && t <= b.t) {
      const span = b.t - a.t;
      if (span < 1e-6) return b;
      const alpha = Math.max(0, Math.min(1, (t - a.t) / span));
      return { x: a.x + alpha * (b.x - a.x), y: a.y + alpha * (b.y - a.y), zoom: a.zoom + alpha * (b.zoom - a.zoom), t };
    }
  }
  return v[v.length - 1];
}

const fixturePath = join(__dirname, "../../tests/fixtures/transform_vectors.json");
const data = JSON.parse(readFileSync(fixturePath, "utf-8"));
const vectors = data.vectors;

let failed = 0;
for (const v of vectors) {
  const c = computeCrop(v.source_width, v.source_height, v.zoom, v.x * v.source_width, v.y * v.source_height, v.output_width, v.output_height);
  if (c.w !== v.crop_width || c.h !== v.crop_height || c.x !== v.crop_x || c.y !== v.crop_y) {
    failed++;
    if (failed < 5) console.log(`FAIL ${JSON.stringify(v)} -> got ${JSON.stringify(c)}`);
  }
}
if (failed > 0) {
  console.error(`TypeScript vectors ${vectors.length - failed}/${vectors.length} passed, ${failed} failed`);
  process.exit(1);
}
console.log(`TypeScript vectors ${vectors.length}/${vectors.length} passed`);

// Interpolation test
const kfs = [{t:0,x:0.5,y:0.5,zoom:1.0},{t:5,x:0.3,y:0.45,zoom:1.5},{t:10,x:0.7,y:0.55,zoom:1.2}];
for (const iv of data.interpolation) {
  const r = interpolateKeyframes(kfs, iv.t, 10);
  if (Math.abs(r.x - iv.x) > 0.01 || Math.abs(r.y - iv.y) > 0.01 || Math.abs(r.zoom - iv.zoom) > 0.01) {
    console.error(`Interp fail t=${iv.t} got ${JSON.stringify(r)} exp ${JSON.stringify(iv)}`);
    process.exit(1);
  }
}
console.log(`Interpolation ${data.interpolation.length}/${data.interpolation.length} passed`);
console.log("ALL TYPESCRIPT VECTOR TESTS PASSED");
