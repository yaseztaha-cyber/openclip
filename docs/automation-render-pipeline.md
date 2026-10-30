# Automation Render Pipeline (v1.0)

Canonical order (enforced by call order in `_render_editor_core`):

```
SOURCE → TRIM → REFRAME/CROP → SOCIAL COMPOSITION → PEOPLE MASK →
CAPTIONS → GRAPHICS → AUDIO → FINAL ENCODE
```

This matches the pre-existing architecture (people-mask was inserted
before captions; no stage was reordered).

- Preview and render coordinates share one space: render-time people
  tracks are measured on final output pixels in normalized 0..1 units; the
  browser overlay positions the same boxes with the same units. Pre-render
  "pre-check" overlays are advisory (source pixels) and labeled as such.
- Render identity (`render_identity.compute_render_hash`) covers source id,
  trim, keyframes, composition, captions, people policy + masks, title,
  subtitle, output settings — no timestamps. Identical inputs reuse;
  any change yields a new identity and invalidates bound approvals.
- Every artifact passes `verify_render_artifact` (ffprobe: streams, dims,
  aspect, duration, size) plus the existing `_verify_clip_integrity`.
- People masking: generic modes `OFF | BLUR_ALL | MANUAL | REVIEW`. No
  appearance/gender/identity classification exists. Tracks: person_id,
  bbox, confidence, interval, blur flag, source (automatic/manual),
  review_status. Crowded/low-confidence/unreviewed tracks → NEEDS_REVIEW.
