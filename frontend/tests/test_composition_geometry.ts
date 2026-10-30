import { readFileSync } from "fs";
import { join } from "path";

// Mirrors backend/composition.py geometry (bg_cover_box / fg_fit_box / caption_y).
// Must stay exact: preview positions derive from these numbers.

function clampC(v: number, lo: number, hi: number) { return Math.max(lo, Math.min(Number(v) || 0, hi)); }
const even = (n: number) => Math.max(2, Math.round(n)) & ~1;

function bgCoverBox(sw: number, sh: number, cw: number, ch: number, scale: number, bx: number, by: number) {
  const s0 = Math.max(cw / sw, ch / sh);
  const s = s0 * clampC(scale, 1, 1.5);
  const scw = Math.max(even(sw * s), cw), sch = Math.max(even(sh * s), ch);
  const ox = Math.round(clampC(bx, 0, 1) * Math.max(scw - cw, 0)) & ~1;
  const oy = Math.round(clampC(by, 0, 1) * Math.max(sch - ch, 0)) & ~1;
  return { scaled_w: scw, scaled_h: sch, crop_w: cw, crop_h: ch,
           crop_x: Math.max(0, Math.min(ox, Math.max(scw - cw, 0))),
           crop_y: Math.max(0, Math.min(oy, Math.max(sch - ch, 0))) };
}
function fgFitBox(sw: number, sh: number, cw: number, ch: number, scale: number, fx: number, fy: number) {
  const base = Math.min(cw / sw, ch / sh);
  let bw = even(sw * base * clampC(scale, 0.5, 1.5));
  let bh = even((bw * sh) / sw);
  if (bw > cw || bh > ch) {
    bh = Math.min(bh, ch); bw = Math.min(even((bh * sw) / sh), cw);
    bh = even((bw * sh) / sw);
    if (bh > ch) { bh = ch & ~1; bw = even((bh * sw) / sh); }
  }
  return { box_w: Math.max(2, bw), box_h: Math.max(2, bh),
           x: Math.round(clampC(fx, 0, 1) * Math.max(cw - bw, 0)),
           y: Math.round(clampC(fy, 0, 1) * Math.max(ch - bh, 0)) };
}
function captionY(ch: number, pos: string, customY = 0.78) {
  if (pos === "top") return Math.floor(ch * 0.16);
  if (pos === "center") return Math.floor(ch * 0.5);
  if (pos === "custom") return Math.floor(clampC(customY, 0, 1) * ch);
  return Math.floor(ch * 0.78);
}

const data = JSON.parse(readFileSync(join(__dirname, "../../tests/fixtures/composition_vectors.json"), "utf-8"));
let failed = 0;
for (const v of data.bg) {
  const r = bgCoverBox(v.sw, v.sh, v.cw, v.ch, v.scale, v.bx, v.by);
  for (const k of ["scaled_w", "scaled_h", "crop_w", "crop_h", "crop_x", "crop_y"] as const) {
    if ((r as any)[k] !== (v as any)[k]) { failed++; if (failed < 5) console.log(`BG FAIL ${JSON.stringify(v)} got ${JSON.stringify(r)}`); break; }
  }
  // even + inside-source invariants
  if (r.scaled_w % 2 || r.scaled_h % 2 || r.crop_x % 2 || r.crop_y % 2) { failed++; console.log(`BG ODD ${JSON.stringify(r)}`); }
}
for (const v of data.fg) {
  const r = fgFitBox(v.sw, v.sh, v.cw, v.ch, v.scale, v.fx, v.fy);
  for (const k of ["box_w", "box_h", "x", "y"] as const) {
    if ((r as any)[k] !== (v as any)[k]) { failed++; if (failed < 5) console.log(`FG FAIL ${JSON.stringify(v)} got ${JSON.stringify(r)}`); break; }
  }
  if (r.box_w % 2 || r.box_h % 2) { failed++; console.log(`FG ODD ${JSON.stringify(r)}`); }
  if (r.x + r.box_w > v.cw || r.y + r.box_h > v.ch) { failed++; console.log(`FG OVERFLOW ${JSON.stringify(v)} got ${JSON.stringify(r)}`); }
}
for (const v of data.caption) {
  if (captionY(v.ch, v.pos) !== v.y) { failed++; console.log(`CAP FAIL ${JSON.stringify(v)}`); }
}
if (failed > 0) { console.error(`${failed} composition geometry mismatches`); process.exit(1); }
console.log(`Composition geometry ${data.bg.length + data.fg.length + data.caption.length}/${data.bg.length + data.fg.length + data.caption.length} passed`);
console.log("ALL COMPOSITION GEOMETRY TESTS PASSED");
