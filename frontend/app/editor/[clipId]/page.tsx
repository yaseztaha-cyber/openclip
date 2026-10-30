"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api";
import PeoplePolicyPanel, { PeopleCfg } from "@/components/PeoplePolicyPanel";

type EditorState = {
  source_start: number;
  source_end: number;
  output_aspect: string;
  crop_mode: string;
  zoom: number;
  x: number;
  y: number;
  caption_style: string | null;
  caption_enabled: boolean;
  caption_scale: number;
  crop_keyframes?: { t: number; x: number; y: number; zoom: number }[];
  caption_position?: string;
  people_policy?: PeopleCfg | null;
  composition?: Composition;
  canvas_preset?: string;
  export_preset?: string | null;
  manual_cuts?: { start: number; end: number; enabled: boolean }[];
};

type Composition = {
  mode: "reframe" | "social";
  canvas: string;
  background: {
    mode: string; blur: string; blur_value: number; darkness: string;
    brightness: number; contrast: number; opacity: number; scale: number;
    x: number; y: number; tint: string; tint_color: string; tint_opacity: number;
    gradient: boolean; gradient_top: string; gradient_bottom: string;
    gradient_opacity: number; follow_foreground: boolean; motion: string;
    visible: boolean; solid_color: string;
  };
  foreground: {
    fit: string; scale: number; x: number; y: number; position_preset: string;
    radius: number; shadow: string;
    border: { enabled: boolean; width: number; color: string; opacity: number };
  };
  captions: { position: string; y: number; scale: number; safe_area: boolean };
  overlays: {
    progress: { enabled: boolean; position: string; color: string; height: number };
    watermark: { enabled: boolean; kind: string; text: string; image: any; position: string; scale: number; opacity: number };
    title: { enabled: boolean; text: string; subtitle: string; position: string; scale: number };
  };
};

// ---- Social composition helpers (mirror backend/composition.py) ----
const CANVAS_SIZES: Record<string, [number, number]> = {
  "9:16": [1080, 1920], "16:9": [1920, 1080], "1:1": [1080, 1080], "4:5": [1080, 1350],
};
const BLUR_SIGMA: Record<string, number> = { none: 0, light: 10, medium: 18, heavy: 30 };
const DARKNESS_OFF: Record<string, number> = { natural: 0, subtle: -0.06, dark: -0.14, very_dark: -0.25 };
const clampC = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(Number(v) || 0, hi));

function defaultComp(canvas = "9:16"): Composition {
  return {
    mode: "reframe", canvas,
    background: { mode: "blurred_video", blur: "medium", blur_value: 18, darkness: "subtle", brightness: 0, contrast: 1, opacity: 1, scale: 1.05, x: 0.5, y: 0.5, tint: "none", tint_color: "#000000", tint_opacity: 0, gradient: false, gradient_top: "#000000", gradient_bottom: "#000000", gradient_opacity: 0.35, follow_foreground: false, motion: "none", visible: true, solid_color: "#0a0a0f" },
    foreground: { fit: "fit", scale: 1, x: 0.5, y: 0.5, position_preset: "center", radius: 0, shadow: "soft", border: { enabled: false, width: 4, color: "#ffffff", opacity: 1 } },
    captions: { position: "bottom", y: 0.78, scale: 1, safe_area: true },
    overlays: {
      progress: { enabled: false, position: "bottom", color: "#ffffff", height: 8 },
      watermark: { enabled: false, kind: "text", text: "", image: null, position: "top-right", scale: 1, opacity: 0.6 },
      title: { enabled: false, text: "", subtitle: "", position: "top", scale: 1 },
    },
  };
}
function mergeComp(partial: any, canvas = "9:16"): Composition {
  const d = defaultComp(canvas);
  if (!partial || typeof partial !== "object") return d;
  return {
    mode: partial.mode === "social" ? "social" : "reframe",
    canvas: CANVAS_SIZES[partial.canvas] ? partial.canvas : canvas,
    background: { ...d.background, ...(partial.background || {}) },
    foreground: {
      ...d.foreground, ...(partial.foreground || {}),
      border: { ...d.foreground.border, ...((partial.foreground || {}).border || {}) },
    },
    captions: { ...d.captions, ...(partial.captions || {}) },
    overlays: {
      progress: { ...d.overlays.progress, ...((partial.overlays || {}).progress || {}) },
      watermark: { ...d.overlays.watermark, ...((partial.overlays || {}).watermark || {}) },
      title: { ...d.overlays.title, ...((partial.overlays || {}).title || {}) },
    },
  };
}
// Geometry mirrors backend bg_cover_box / fg_fit_box (output-pixel math).
function bgCoverBox(sw: number, sh: number, cw: number, ch: number, scale: number, bx: number, by: number) {
  const s0 = Math.max(cw / sw, ch / sh);
  const s = s0 * clampC(scale, 1, 1.5);
  const even = (n: number) => Math.max(2, Math.round(n)) & ~1;
  const scw = Math.max(even(sw * s), cw), sch = Math.max(even(sh * s), ch);
  const ox = (Math.round(clampC(bx, 0, 1) * Math.max(scw - cw, 0)) & ~1);
  const oy = (Math.round(clampC(by, 0, 1) * Math.max(sch - ch, 0)) & ~1);
  return { scaledW: scw, scaledH: sch, x: Math.max(0, Math.min(ox, Math.max(scw - cw, 0))), y: Math.max(0, Math.min(oy, Math.max(sch - ch, 0))) };
}
function fgFitBox(sw: number, sh: number, cw: number, ch: number, scale: number, fx: number, fy: number) {
  const even = (n: number) => Math.max(2, Math.round(n)) & ~1;
  const base = Math.min(cw / sw, ch / sh);
  let bw = even(sw * base * clampC(scale, 0.5, 1.5));
  let bh = even((bw * sh) / sw);
  if (bw > cw || bh > ch) {
    bh = Math.min(bh, ch); bw = Math.min(even((bh * sw) / sh), cw);
    bh = even((bw * sh) / sw);
    if (bh > ch) { bh = ch & ~1; bw = even((bh * sw) / sh); }
  }
  return { w: Math.max(2, bw), h: Math.max(2, bh), x: Math.round(clampC(fx, 0, 1) * Math.max(cw - bw, 0)), y: Math.round(clampC(fy, 0, 1) * Math.max(ch - bh, 0)) };
}
function captionYFrac(position: string, customY = 0.78) {
  if (position === "top") return 0.16;
  if (position === "center") return 0.5;
  if (position === "custom") return clampC(customY, 0, 1);
  return 0.78;
}
// Client-side preset mirror (backend normalizes on save; same values).
const SOCIAL_PRESET_CLIENT: Record<string, any> = {
  classic_vertical: { background: { mode: "blurred_video", blur: "medium", darkness: "subtle", scale: 1.05 }, foreground: { scale: 1, y: 0.5, position_preset: "center", radius: 0, shadow: "soft" } },
  blur_16x9: { background: { mode: "blurred_video", blur: "medium", darkness: "subtle", scale: 1.08 }, foreground: { scale: 1, y: 0.5, position_preset: "center", radius: 0, shadow: "soft" } },
  dark_blur: { background: { mode: "blurred_video", blur: "heavy", darkness: "dark", scale: 1.08 }, foreground: { scale: 0.95, y: 0.45, position_preset: "custom", radius: 16, shadow: "strong" } },
  gradient: { background: { mode: "blurred_video", blur: "light", darkness: "subtle", scale: 1.05, gradient: true, gradient_opacity: 0.45 }, foreground: { scale: 0.92, y: 0.42, position_preset: "custom", radius: 20, shadow: "soft" } },
  floating: { background: { mode: "darkened_video", blur: "none", darkness: "dark", scale: 1 }, foreground: { scale: 0.88, y: 0.5, position_preset: "center", radius: 28, shadow: "strong", border: { enabled: true, width: 3, color: "#ffffff", opacity: 0.85 } } },
};
function applyClientPreset(comp: Composition, name: string): Composition {
  const p = SOCIAL_PRESET_CLIENT[name];
  if (!p) return comp;
  const bg = { ...comp.background, ...p.background };
  if (typeof p.background.blur === "string") bg.blur_value = BLUR_SIGMA[p.background.blur] ?? 18;
  const fg = { ...comp.foreground, ...p.foreground, border: { ...comp.foreground.border, ...(p.foreground.border || {}) } };
  return mergeComp({ ...comp, mode: "social", background: bg, foreground: fg }, comp.canvas);
}
function autoSocialComp(sw: number, sh: number): Composition {
  const vertical = Math.abs(sw / Math.max(sh, 1) - 1080 / 1920) < 0.01;
  if (vertical) {
    const c = defaultComp("9:16");
    c.mode = "social";
    c.background = { ...c.background, mode: "blurred_video", blur: "none", blur_value: 0, scale: 1 };
    c.foreground = { ...c.foreground, shadow: "none" };
    return c;
  }
  const c = defaultComp("9:16");
  return applyClientPreset(c, "blur_16x9");
}

// Shared interpolation — must match backend _interpolate_keyframes (linear, clamped, sorted)
function validateKeyframes(kfs: { t: number; x: number; y: number; zoom: number }[], duration: number) {
  const seen = new Map<number, { t: number; x: number; y: number; zoom: number }>();
  for (const kf of kfs) {
    const t = Math.max(0, Math.min(Number(kf.t) || 0, duration));
    const x = Math.max(0, Math.min(Number(kf.x) || 0.5, 1));
    const y = Math.max(0, Math.min(Number(kf.y) || 0.5, 1));
    const zoom = Math.max(0.5, Math.min(Number(kf.zoom) || 1, 3));
    seen.set(Math.round(t * 1000) / 1000, { t, x, y, zoom });
  }
  return Array.from(seen.values()).sort((a, b) => a.t - b.t);
}
function computeCrop(
  vidW: number,
  vidH: number,
  zoom: number,
  cx: number,
  cy: number,
  arW: number,
  arH: number
) {
  // Mirrors Python _compute_crop exactly: round + even dims/offsets for yuv420p.
  // For manual mode: cx = x * vidW, cy = y * vidH
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
  const fx = Math.max(0, Math.min(cx - cw / 2, Math.max(slackX, 0)));
  const fy = Math.max(0, Math.min(cy - ch / 2, Math.max(slackY, 0)));
  let ox = Math.round(fx) & ~1, oy = Math.round(fy) & ~1;
  ox = Math.max(0, Math.min(ox, Math.max(0, Math.floor(vidW) - cw)));
  oy = Math.max(0, Math.min(oy, Math.max(0, Math.floor(vidH) - ch)));
  if (ox + cw > vidW) ox = (Math.floor(vidW) - cw) & ~1;
  if (oy + ch > vidH) oy = (Math.floor(vidH) - ch) & ~1;
  return { x: Math.max(0, ox), y: Math.max(0, oy), w: cw, h: ch };
}
function aspectToRatio(aspect: string): [number, number, number, number] {
  const map: Record<string, [number, number, number, number]> = {
    "9:16": [9, 16, 1080, 1920],
    "1:1": [1, 1, 1080, 1080],
    "16:9": [16, 9, 1920, 1080],
    "4:3": [4, 3, 1440, 1080],
  };
  return map[aspect] || map["9:16"];
}
function interpolateKeyframes(kfs: { t: number; x: number; y: number; zoom: number }[], t: number, duration: number) {
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
      return {
        x: a.x + alpha * (b.x - a.x),
        y: a.y + alpha * (b.y - a.y),
        zoom: a.zoom + alpha * (b.zoom - a.zoom),
        t,
      } as any;
    }
  }
  return v[v.length - 1];
}

export default function EditorPage() {
  const params = useParams();
  const router = useRouter();
  const clipId = params.clipId as string;
  const [clip, setClip] = useState<any>(null);
  const [project, setProject] = useState<any>(null);
  const [state, setState] = useState<EditorState | null>(null);
  const [history, setHistory] = useState<EditorState[]>([]);
  const [future, setFuture] = useState<EditorState[]>([]);
  const [saving, setSaving] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [source, setSource] = useState<any>(null);
  const [thumbs, setThumbs] = useState<any[]>([]);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [playUrl, setPlayUrl] = useState<string | null>(null);
  const [videoError, setVideoError] = useState<string | null>(null);
  const [compareOn, setCompareOn] = useState(false);
  const [renderJob, setRenderJob] = useState<any>(null);
  const [dirty, setDirty] = useState(false);
  const [saveStatus, setSaveStatus] = useState<string>("");
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [peopleMasks, setPeopleMasks] = useState<{ track_id: number; box: { x: number; y: number; w: number; h: number }; policy_status?: string }[]>([]);
  const onPeopleMasks = useCallback((m: { track_id: number; box: { x: number; y: number; w: number; h: number }; policy_status?: string }[]) => {
    setPeopleMasks(m);
  }, []);
  const videoRef = useRef<HTMLVideoElement>(null);
  const fgVideoRef = useRef<HTMLVideoElement>(null);
  const srcVideoRef = useRef<HTMLVideoElement>(null);
  const compBoxRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef<EditorState | null>(null);
  const dragStartRef = useRef<EditorState | null>(null);
  const savedSnapshotRef = useRef<string | null>(null);

  const api = getApiBaseUrl();

  useEffect(() => {
    const load = async () => {
      // Find clip first (direct lookup, falling back to a project scan)
      let foundClip: any = null;
      let foundProject: any = null;
      try {
        const direct = await fetch(`${api}/api/clips/${clipId}`).then((x) => (x.ok ? x.json() : null));
        if (direct?.clip) {
          foundClip = direct.clip;
          foundProject = direct.project;
        }
      } catch {}
      if (!foundClip) {
        const pr = await fetch(`${api}/api/projects`).then((x) => x.json());
        for (const p of pr) {
          const det = await fetch(`${api}/api/projects/${p.id}`).then((x) => x.json());
          const found = det.clips?.find((c: any) => c.id === clipId);
          if (found) {
            foundClip = found;
            foundProject = det;
            break;
          }
        }
      }
      if (!foundClip) {
        setError("Clip not found");
        return;
      }
      setClip(foundClip);
      setProject(foundProject);

      // Fetch source metadata (real duration, fps, etc.)
      let srcData: any = null;
      try {
        const src = await fetch(`${api}/api/projects/${foundProject.id}/source`).then((r) => r.json());
        setSource(src);
        srcData = src;
        const th = await fetch(`${api}/api/projects/${foundProject.id}/thumbnails?count=12`).then((r) => r.json());
        setThumbs(th.thumbnails || []);
      } catch (e) {
        console.warn("Failed to load source/thumbs", e);
      }
      // Resolve a playable URL: prefer full source (trim context), fall back to clip file.
      // Prevents cryptic "no supported sources" on play when a file is missing.
      try {
        const headOk = async (u: string) => {
          try {
            const r = await fetch(u, { method: "HEAD" });
            return r.ok;
          } catch {
            return false;
          }
        };
        const srcUrl = srcData?.path ? `${api}${srcData.path}` : null;
        const clipUrl = `${api}${foundClip.file_path}`;
        if (srcUrl && (await headOk(srcUrl))) {
          setPlayUrl(srcUrl);
        } else if (await headOk(clipUrl)) {
          setPlayUrl(clipUrl);
          setVideoError("Source video unavailable — previewing the rendered clip file instead (trim range limited).");
        } else {
          // Fall back to the newest editor render if the source clip file is gone.
          let renderOk = false;
          try {
            const rr = await fetch(`${api}/api/clips/${clipId}/renders`).then((x) => (x.ok ? x.json() : []));
            if (Array.isArray(rr) && rr.length > 0) {
              const rel = rr[rr.length - 1].file_path;
              if (rel && (await headOk(`${api}${rel}`))) {
                setPlayUrl(`${api}${rel}`);
                setVideoError("Source clip file was missing — previewing the latest render. Use the timeline only if this is the original range.");
                renderOk = true;
              }
            }
          } catch {}
          if (!renderOk) {
            setVideoError(`No playable video found (clip file missing: ${clipUrl}). Re-run the project or re-render the clip.`);
          }
        }
      } catch {
        setPlayUrl(`${api}${foundClip.file_path}`);
      }

      // Load editor state or init from clip
      try {
        const data = await fetch(`${api}/api/clips/${clipId}/editor-state`).then((r) => (r.ok ? r.json() : null));
        if (data && data.source_start !== undefined) {
          const st = {
            source_start: data.source_start,
            source_end: data.source_end,
            output_aspect: data.output_aspect || "9:16",
            crop_mode: data.crop_mode || "auto",
            zoom: data.zoom ?? 1.0,
            x: data.x ?? 0.5,
            y: data.y ?? 0.5,
            caption_style: data.caption_style,
            caption_enabled: data.caption_enabled ?? true,
            caption_scale: data.caption_scale ?? 1.0,
            crop_keyframes: data.crop_keyframes,
            caption_position: data.caption_position,
            composition: mergeComp(data.composition, data.canvas_preset || data.output_aspect || "9:16"),
            canvas_preset: data.canvas_preset || data.output_aspect || "9:16",
            export_preset: data.export_preset ?? null,
          };
          savedSnapshotRef.current = JSON.stringify(st);
          setState(st);
          return;
        }
      } catch {}
      // Init from clip
      const initAspect = foundClip.aspect || "9:16";
      const initState: EditorState = {
        source_start: foundClip.start_time,
        source_end: foundClip.end_time,
        output_aspect: initAspect,
        crop_mode: "auto",
        zoom: 1.0,
        x: 0.5,
        y: 0.5,
        caption_style: foundClip.caption_style || "viral_word",
        caption_enabled: true,
        caption_scale: 1.0,
        composition: defaultComp(initAspect),
        canvas_preset: initAspect,
        export_preset: null,
      };
      savedSnapshotRef.current = JSON.stringify(initState);
      setState(initState);
    };
    load();
  }, [clipId, api]);

  // Editorial renders list → newest file download target
  useEffect(() => {
    if (!clipId) return;
    let on = true;
    (async () => {
      try {
        const r = await fetch(`${api}/api/clips/${clipId}/renders`).then((x) => (x.ok ? x.json() : []));
        if (!on || !Array.isArray(r) || r.length === 0) return;
        const latest = r[r.length - 1];
        if (latest?.file_path) setDownloadUrl(`${api}${latest.file_path}`);
      } catch {}
    })();
    return () => { on = false; };
  }, [clipId, api, renderJob]);

  // Trim-aware playback: loop within render range when not showing source
  useEffect(() => {
    const v = videoRef.current;
    if (!v || showSource || !state) return;
    const onTimeUpdate = () => {
      if (v.currentTime > state.source_end) {
        v.currentTime = state.source_start;
        v.play();
      }
      if (v.currentTime < state.source_start) {
        v.currentTime = state.source_start;
      }
    };
    v.addEventListener("timeupdate", onTimeUpdate);
    return () => v.removeEventListener("timeupdate", onTimeUpdate);
  }, [showSource, state?.source_start, state?.source_end]);

  const dragLayerRef = useRef<"fg" | "bg" | null>(null);
  const trimDragRef = useRef<"start" | "end" | null>(null);
  const trimBarRef = useRef<HTMLDivElement | null>(null);
  const pushHistory = useCallback((next: EditorState) => {
    if (state) setHistory((h) => [...h.slice(-19), state]);
    setFuture([]);
    setState(next);
  }, [state]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // ---- Render job: WebSocket progress + polling fallback ----
  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: number | null = null;
    let closed = false;
    const wsUrl = api.replace(/^http/, "ws").replace(/\/$/, "");
    const poll = async () => {
      if (closed) return;
      try {
        const r = await fetch(`${api}/api/clips/${clipId}/render-job`).then((x) => (x.ok ? x.json() : null));
        if (r && r.status && r.status !== "idle") setRenderJob((prev: any) => (!prev || prev.job_id === r.job_id ? r : prev));
        else if (r && r.status === "idle") setRenderJob((prev: any) => (prev && (prev.status === "queued" || prev.status === "running" || prev.job_id === "pending") ? null : prev));
      } catch {}
    };
    try {
      ws = new WebSocket(`${wsUrl}/ws/render/${clipId}`);
      ws.onmessage = (ev) => {
        try { setRenderJob(JSON.parse(ev.data as string)); } catch {}
      };
      ws.onclose = () => { if (!closed) timer = window.setInterval(poll, 1200); };
    } catch {
      timer = window.setInterval(poll, 1200);
    }
    return () => { closed = true; if (timer) clearInterval(timer); try { ws?.close(); } catch {} };
  }, [clipId, api]);

  // Reset rendering state on job status transitions
  useEffect(() => {
    if (!renderJob) return;
    if (renderJob.status === "done") {
      setRendering(false);
      if (renderJob.output) {
        setVideoError(null);
        setDownloadUrl(`${api}${renderJob.output}`);
      }
    } else if (renderJob.status === "error") {
      setRendering(false);
      setError(renderJob.error || renderJob.message || "Render failed");
    } else if (renderJob.status === "cancelled") {
      setRendering(false);
    } else if (renderJob.status === "running" || renderJob.status === "queued") {
      setRendering(true);
    }
  }, [renderJob, api]);

  // ---- Autosave + dirty tracking ----
  const postSave = useCallback(async (st: EditorState, silent = false): Promise<boolean> => {
    try {
      const r = await fetch(`${api}/api/clips/${clipId}/editor-state`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(st),
      });
      if (!r.ok) throw new Error(await r.text());
      savedSnapshotRef.current = JSON.stringify(st);
      setDirty(false);
      if (!silent) setSaveStatus(new Date().toLocaleTimeString());
      return true;
    } catch (e: any) {
      if (!silent) setError(e.message || "Save failed");
      return false;
    }
  }, [clipId, api]);

  useEffect(() => {
    if (!state) return;
    if (JSON.stringify(state) === savedSnapshotRef.current) {
      setDirty(false);
      return;
    }
    setDirty(true);
    if (saving) return;
    const t = window.setTimeout(() => {
      setSaving(true);
      postSave(state, true).finally(() => setSaving(false));
    }, 1200);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  // Warn when leaving with unsaved changes
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // ---- A/B compare: source master, edited follows (like social fg/bg sync) ----
  useEffect(() => {
    const src = srcVideoRef.current;
    const master = videoRef.current;
    if (!src || !master || !compareOn) return;
    const sync = () => {
      if (Math.abs(src.currentTime - master.currentTime) > 0.15) {
        try { src.currentTime = master.currentTime; } catch {}
      }
      if (playing) src.play().catch(() => {});
      else src.pause();
    };
    sync();
    const iv = window.setInterval(sync, 150);
    return () => clearInterval(iv);
  }, [compareOn, playing, time]);

  // Grouped slider history: live-update without history spam, commit once on release.
  const beginCompDrag = useCallback(() => {
    dragStartRef.current = stateRef.current;
  }, []);
  const endCompDrag = useCallback(() => {
    const started = dragStartRef.current;
    dragStartRef.current = null;
    // Skip no-op entries (click without change) so undo stays meaningful.
    if (started && JSON.stringify(started) !== JSON.stringify(stateRef.current)) {
      setHistory((h) => [...h.slice(-19), started]);
      setFuture([]);
    }
  }, []);
  const patchCompLive = useCallback((fn: (c: Composition) => Composition) => {
    const cur = stateRef.current;
    if (!cur) return;
    const comp = mergeComp(cur.composition, cur.canvas_preset || "9:16");
    setState({ ...cur, composition: fn(comp) });
  }, []);
  const pushComp = useCallback((fn: (c: Composition) => Composition) => {
    const cur = stateRef.current;
    if (!cur) return;
    const comp = mergeComp(cur.composition, cur.canvas_preset || "9:16");
    if (state) setHistory((h) => [...h.slice(-19), state]);
    setFuture([]);
    setState({ ...cur, composition: fn(comp) });
  }, [state]);

  // Social dual-video sync: bg video is master (videoRef), fg follows.
  const socialOn = !!state?.composition && state.composition.mode === "social" && !showSource;
  useEffect(() => {
    const fg = fgVideoRef.current;
    const bg = videoRef.current;
    if (!fg || !bg || !socialOn) return;
    if (Math.abs(fg.currentTime - bg.currentTime) > 0.15) {
      try { fg.currentTime = bg.currentTime; } catch {}
    }
    if (playing) fg.play().catch(() => {});
    else fg.pause();
  }, [time, playing, socialOn]);
  const seekBoth = (v: number) => {
    setTime(v);
    if (videoRef.current) videoRef.current.currentTime = v;
    if (fgVideoRef.current) { try { fgVideoRef.current.currentTime = v; } catch {} }
  };
  const togglePlayBoth = () => {
    const bg = videoRef.current;
    if (!bg) return;
    if (bg.paused) {
      bg.play();
      fgVideoRef.current?.play().catch(() => {});
    } else {
      bg.pause();
      fgVideoRef.current?.pause();
    }
  };
  // Direct drag on the 9:16 canvas: fg layer moves foreground, bg moves background.
  const moveComp = (clientX: number, clientY: number) => {
    const el = compBoxRef.current;
    const cur = stateRef.current;
    const layer = dragLayerRef.current;
    if (!el || !cur || !layer) return;
    const r = el.getBoundingClientRect();
    const nx = clampC((clientX - r.left) / Math.max(r.width, 1), 0, 1);
    const ny = clampC((clientY - r.top) / Math.max(r.height, 1), 0, 1);
    const comp = mergeComp(cur.composition, cur.canvas_preset || "9:16");
    if (layer === "fg") {
      const fg = { ...comp.foreground, x: Math.round(nx * 200) / 200, y: Math.round(ny * 200) / 200, position_preset: "custom" };
      setState({ ...cur, composition: { ...comp, foreground: fg } });
    } else {
      const bg = { ...comp.background, x: Math.round(nx * 200) / 200, y: Math.round(ny * 200) / 200 };
      setState({ ...cur, composition: { ...comp, background: bg } });
    }
  };
  const beginCompPointer = (layer: "fg" | "bg") => (e: React.PointerEvent) => {
    dragLayerRef.current = layer;
    beginCompDrag();
    moveComp(e.clientX, e.clientY);
  };
  const endCompPointer = () => {
    if (dragLayerRef.current) {
      dragLayerRef.current = null;
      endCompDrag();
    }
  };
  const applyPreset = (name: string) => {
    pushComp((c) => applyClientPreset(c, name));
  };
  const autoSocial = () => {
    const sw = source?.width || 1920;
    const sh = source?.height || 1080;
    const c = autoSocialComp(sw, sh);
    if (!state) return;
    pushHistory({ ...state, composition: c, canvas_preset: "9:16", output_aspect: "9:16" });
  };
  const resetCompPart = (part: "background" | "foreground" | "all") => {
    pushComp((c) => {
      const d = defaultComp(c.canvas);
      if (part === "all") return { ...d, mode: "social", canvas: c.canvas };
      return { ...c, mode: "social", [part]: (d as any)[part] };
    });
  };

  const undo = () => {
    if (history.length === 0) return;
    const prev = history[history.length - 1];
    setFuture((f) => [state!, ...f]);
    setHistory((h) => h.slice(0, -1));
    setState(prev);
  };
  const redo = () => {
    if (future.length === 0) return;
    const nxt = future[0];
    setHistory((h) => [...h, state!]);
    setFuture((f) => f.slice(1));
    setState(nxt);
  };

  // ---- Interactive trim handles (grouped undo like comp sliders) ----
  const totalDur = source?.duration || duration || 0;
  const beginTrim = (which: "start" | "end") => (e: React.PointerEvent) => {
    e.preventDefault();
    trimDragRef.current = which;
    beginCompDrag();
  };
  const onTrimMove = (e: React.PointerEvent) => {
    const which = trimDragRef.current;
    const bar = trimBarRef.current;
    const cur = stateRef.current;
    if (!which || !bar || !cur || !totalDur) return;
    const rect = bar.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(rect.width, 1)));
    const t = Math.round(frac * totalDur * 100) / 100;
    const minDur = 0.5;
    if (which === "start") {
      const next = Math.max(0, Math.min(t, cur.source_end - minDur));
      if (Math.abs(next - cur.source_start) > 0.01) {
        setState({ ...cur, source_start: next });
        seekBoth(next);
      }
    } else {
      const next = Math.min(totalDur, Math.max(t, cur.source_start + minDur));
      if (Math.abs(next - cur.source_end) > 0.01) {
        setState({ ...cur, source_end: next });
        seekBoth(next);
      }
    }
  };
  const endTrim = () => {
    if (trimDragRef.current) {
      trimDragRef.current = null;
      endCompDrag();
    }
  };
  const setTrimFromPlayhead = (which: "start" | "end") => {
    if (!state) return;
    const t = Math.round(Math.max(0, Math.min(time, totalDur || time)) * 100) / 100;
    const minDur = 0.5;
    if (which === "start") pushHistory({ ...state, source_start: Math.min(t, state.source_end - minDur) });
    else pushHistory({ ...state, source_end: Math.max(t, state.source_start + minDur) });
  };

  const save = async () => {
    if (!state) return;
    setSaving(true);
    setError(null);
    await postSave(state);
    setSaving(false);
  };

  const render = async () => {
    if (!state) return;
    setError(null);
    if (renderJob?.status === "running" || renderJob?.status === "queued") return;
    // Autosave pending edits first so the render uses the latest state
    if (dirty) await postSave(state, true);
    setRendering(true);
    setRenderJob({ job_id: "pending", status: "queued", percent: 0, message: "Queued…", stage: "queued" });
    try {
      const r = await fetch(`${api}/api/clips/${clipId}/editor-render`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      const j = await r.json();
      setRenderJob((prev: any) => (prev?.job_id === "pending" ? { ...prev, job_id: j.job_id } : j));
    } catch (e: any) {
      setRendering(false);
      setRenderJob(null);
      setError(e.message || "Render start failed");
    }
  };

  const cancelRender = async () => {
    if (!(renderJob?.status === "running" || renderJob?.status === "queued")) return;
    try {
      await fetch(`${api}/api/clips/${clipId}/render-job/cancel`, { method: "POST" });
    } catch {}
  };

  const downloadClip = async () => {
    try {
      const res = await fetch(`${api}/api/clips/${clipId}/download`);
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const cd = (res.headers.get("content-disposition") || "");
      const m = cd.match(/filename="?([^";]+)"?/i);
      a.href = url;
      a.download = m ? m[1] : `${(clip.title || "clip").replace(/[^a-zA-Z0-9 _-]/g, "")}.mp4`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (e: any) {
      setError(e.message || "Download failed");
    }
  };

  const backClick = () => {
    if (dirty && !window.confirm("You have unsaved changes. Leave anyway?")) return;
    router.push(`/project/${clip.project_id}`);
  };

  if (error && !clip) return <div className="p-8 text-red-400">{error} <Link href="/" className="underline">Back</Link></div>;
  if (!clip || !state) return <div className="p-8 text-slate-400">Loading editor…</div>;

  const clipDuration = state.source_end - state.source_start;
  const aspectStyle = state.output_aspect === "9:16" ? "aspect-[9/16]" : state.output_aspect === "1:1" ? "aspect-square" : state.output_aspect === "16:9" ? "aspect-video" : "aspect-[4/3]";
  // For manual keyframes, preview uses interpolated values at current playhead time
  const kfs = state.crop_keyframes && state.crop_keyframes.length > 0 ? validateKeyframes(state.crop_keyframes, clipDuration) : [];
  const interp = state.crop_mode === "manual" && kfs.length > 0 ? interpolateKeyframes(kfs, time, clipDuration) : null;
  const previewX = interp ? interp.x : state.x;
  const previewY = interp ? interp.y : state.y;
  const previewZoom = interp ? interp.zoom : state.zoom;
  const isInterpolated = !!(interp && kfs.length > 1 && time > kfs[0].t && time < kfs[kfs.length - 1].t && !kfs.some((k) => Math.abs(k.t - time) < 0.05));
  // Exact crop for preview (mirrors Python _compute_crop)
  const sourceW = source?.width || 1920;
  const sourceH = source?.height || 1080;
  const [arW, arH] = aspectToRatio(state.output_aspect);
  const previewCrop = (() => {
    if (state.crop_mode !== "manual") return null;
    const cx = previewX * sourceW;
    const cy = previewY * sourceH;
    return computeCrop(sourceW, sourceH, previewZoom, cx, cy, arW, arH);
  })();

  // ---- Social composition preview math (mirrors backend/composition.py) ----
  const comp = mergeComp(state.composition, state.canvas_preset || "9:16");
  const isSocialComp = comp.mode === "social" && !showSource;
  const [ccW, ccH] = CANVAS_SIZES[comp.canvas] || CANVAS_SIZES["9:16"];
  const canvasAspectCls = comp.canvas === "9:16" ? "aspect-[9/16]" : comp.canvas === "1:1" ? "aspect-square" : comp.canvas === "16:9" ? "aspect-video" : "aspect-[1080/1350]";
  const socialKf = state.crop_mode === "manual" && kfs.length > 0 ? interpolateKeyframes(kfs, time, clipDuration) : null;
  const fgUseReframe = comp.foreground.fit === "reframe" || !!socialKf;
  const fgBox = fgFitBox(sourceW, sourceH, ccW, ccH, comp.foreground.scale, comp.foreground.x, comp.foreground.y);
  const fgContentZoom = socialKf ? socialKf.zoom : 1;
  const fgContentX = socialKf ? socialKf.x : 0.5;
  const fgContentY = socialKf ? socialKf.y : 0.5;
  const bgBlurPx = comp.background.blur === "none" ? 0 : comp.background.blur_value || 18;
  const bgBright = clampC(1 + (DARKNESS_OFF[comp.background.darkness] ?? -0.06) + comp.background.brightness, 0.35, 1.25);
  const bgContrast = clampC(comp.background.contrast, 0.8, 1.2);
  const capFrac = captionYFrac(comp.captions.position, comp.captions.y);
  const radPct = comp.foreground.radius > 0 ? (comp.foreground.radius / Math.max(Math.min(fgBox.w, fgBox.h), 1)) * 100 : 0;
  const fgShadowCss = comp.foreground.shadow === "soft" ? "0 10px 40px rgba(0,0,0,0.45)" : comp.foreground.shadow === "strong" ? "0 18px 70px rgba(0,0,0,0.65)" : "none";
  const showCapOverlay = state.caption_enabled && (state.caption_style || "viral_word") !== "none" && comp.captions.position !== "off";
  const videoSrc = playUrl;
  const onVideoError = () => {
    setVideoError((v) => v || `Video failed to load (${playUrl || "no URL resolved"}). The file may be missing — re-run the project or re-render the clip.`);
  };

  // Composition controls: sliders update live, commit once on release (grouped undo).
  const compSlider = (label: string, value: number, min: number, max: number, step: number, apply: (c: Composition, v: number) => Composition, display?: string) => (
    <label className="block text-xs">
      <span className="text-slate-500">{label} {display ?? value}</span>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onPointerDown={beginCompDrag}
        onChange={(e) => patchCompLive((c) => apply(c, parseFloat(e.target.value)))}
        onPointerUp={endCompDrag} onBlur={endCompDrag} onKeyUp={endCompDrag}
        className="w-full accent-violet-500"
      />
    </label>
  );
  const compSelect = (label: string, value: string, options: string[], apply: (c: Composition, v: string) => Composition) => (
    <label className="block text-xs">
      <span className="text-slate-500">{label}</span>
      <select value={value} onChange={(e) => pushComp((c) => apply(c, e.target.value))} className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm">
        {options.map((o) => (<option key={o} value={o}>{o}</option>))}
      </select>
    </label>
  );
  const compCheck = (label: string, checked: boolean, apply: (c: Composition, v: boolean) => Composition) => (
    <label className="flex items-center gap-2 text-xs">
      <input type="checkbox" checked={checked} onChange={(e) => pushComp((c) => apply(c, e.target.checked))} />
      {label}
    </label>
  );

  return (
    <div className="min-h-screen bg-[#0a0a0f] text-white flex flex-col">
      {/* Top bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-white/10 bg-black/40">
        <div className="flex items-center gap-2">
          <button onClick={backClick} className="text-sm text-slate-400 hover:text-white">← Back</button>
          <span className="text-sm font-bold">{clip.title || "Clip"}</span>
          <span className="text-xs text-slate-500">{clipDuration.toFixed(2)}s</span>
          {(renderJob?.status === "running" || renderJob?.status === "queued") && (
            <span className="text-[10px] px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-300 animate-pulse">{renderJob.message || "Rendering…"} {Math.round(renderJob.percent || 0)}%</span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <span className="text-[10px] text-slate-500 hidden md:block">
            {saving ? "Saving…" : saveStatus ? `Saved ${saveStatus}` : dirty ? "● Unsaved" : ""}
          </span>
          <button onClick={undo} disabled={history.length===0} className="px-2 py-1 text-xs rounded bg-white/10 disabled:opacity-30">Undo</button>
          <button onClick={redo} disabled={future.length===0} className="px-2 py-1 text-xs rounded bg-white/10 disabled:opacity-30">Redo</button>
          <button onClick={() => pushHistory({ ...state, crop_mode: "auto", zoom: 1, x: 0.5, y: 0.5 })} className="px-2 py-1 text-xs rounded bg-white/10">Reset to Auto</button>
          <button onClick={save} disabled={saving || !dirty} className="px-3 py-1 text-xs rounded bg-violet-600 hover:bg-violet-700 disabled:opacity-40">{saving ? "Saving…" : "Save"}</button>
          <button onClick={downloadClip} disabled={rendering || !downloadUrl} title={downloadUrl ? "Download latest render" : "Render first to enable download"} className="px-3 py-1 text-xs rounded bg-sky-600 hover:bg-sky-700 disabled:opacity-40">⬇ Download</button>
          <button
            onClick={render}
            disabled={renderJob?.status === "running" || renderJob?.status === "queued"}
            className="px-3 py-1 text-xs rounded bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50"
          >
            {rendering ? "Rendering…" : isSocialComp ? `Render Social ${comp.canvas}` : "Render"}
          </button>
          {rendering && (
            <button onClick={cancelRender} className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-700">Cancel</button>
          )}
        </div>
      </div>
      {rendering && renderJob && (
        <div className="px-4 py-1.5 bg-cyan-500/10 border-b border-cyan-500/20">
          <div className="w-full max-w-[560px] h-1.5 bg-white/10 rounded overflow-hidden">
            <div className="h-full bg-cyan-400 transition-all" style={{ width: `${Math.round(renderJob.percent || 0)}%` }} />
          </div>
          <div className="text-[10px] text-cyan-300 mt-1">{renderJob.stage} — {renderJob.message || "Rendering…"}</div>
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        {/* Left: preview */}
        <div className="flex-1 flex flex-col items-center justify-center p-4 bg-black gap-3">
          <div className="flex gap-1 mb-2">
            <button onClick={() => { setShowSource(false); setCompareOn(false); }} className={`flex-1 py-1 text-xs rounded ${!showSource && !compareOn ? "bg-violet-600" : "bg-white/10"}`}>OUTPUT {isSocialComp ? `SOCIAL ${comp.canvas}` : state.output_aspect}</button>
            <button onClick={() => { setShowSource(true); setCompareOn(false); }} className={`flex-1 py-1 text-xs rounded ${showSource ? "bg-violet-600" : "bg-white/10"}`}>SOURCE</button>
            <button onClick={() => setCompareOn((v) => !v)} className={`flex-1 py-1 text-xs rounded ${compareOn ? "bg-emerald-600" : "bg-white/10"}`}>A/B</button>
          </div>
          {videoError && <div className="w-full max-w-[560px] text-xs text-amber-200 p-2 rounded bg-amber-500/10 border border-amber-500/30">{videoError}</div>}
          {compareOn ? (
            /* A/B split: SOURCE full frame (left) vs EDITED OUTPUT (right), synced */
            <div className="w-full max-w-[560px] grid grid-cols-2 gap-2 items-start">
              <div className="relative bg-black rounded-xl overflow-hidden border border-white/10 aspect-video">
                <video
                  ref={srcVideoRef}
                  src={videoSrc ?? undefined}
                  onError={onVideoError}
                  className="absolute inset-0 w-full h-full object-contain bg-black"
                  muted
                  controls={false}
                  playsInline
                />
                <span className="absolute top-1 left-1 text-[9px] px-1.5 py-0.5 rounded bg-black/60">SOURCE</span>
              </div>
              <div className={`relative bg-black rounded-xl overflow-hidden border border-white/10 ${isSocialComp ? canvasAspectCls : aspectStyle}`}>
                <video
                  ref={videoRef}
                  src={videoSrc ?? undefined}
                  onError={onVideoError}
                  className="absolute inset-0 w-full h-full object-cover"
                  muted
                  controls={false}
                  playsInline
                  style={!showSource && state.crop_mode === "manual" ? { transform: `scale(${previewZoom}) translate(${(0.5 - previewX) * 100}%, ${(0.5 - previewY) * 100}%)`, transformOrigin: "center" } : undefined}
                  onTimeUpdate={(e) => { const v = e.target as HTMLVideoElement; setTime(v.currentTime); if (srcVideoRef.current) { try { srcVideoRef.current.currentTime = v.currentTime; } catch {} } }}
                  onLoadedMetadata={(e) => setDuration((e.target as HTMLVideoElement).duration)}
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                />
                <span className="absolute top-1 left-1 text-[9px] px-1.5 py-0.5 rounded bg-black/60">EDITED</span>
              </div>
              {isSocialComp && <div className="col-span-2 text-[10px] text-slate-500">Social canvas layers preview in OUTPUT mode — switch to OUTPUT for the full composition.</div>}
            </div>
          ) : (
          <div className={`relative bg-black rounded-xl overflow-hidden border border-white/10 ${showSource ? "aspect-video" : isSocialComp ? canvasAspectCls : aspectStyle} w-full max-w-[360px]`} style={{ maxHeight: "65vh" }}>
            {isSocialComp ? (
              /* SOCIAL CANVAS preview: real source on both layers, synced */
              <div
                ref={compBoxRef}
                className="absolute inset-0 overflow-hidden bg-black select-none cursor-crosshair"
                onPointerDown={beginCompPointer("bg")}
                onPointerMove={(e) => { if (dragLayerRef.current === "bg") moveComp(e.clientX, e.clientY); }}
                onPointerUp={endCompPointer}
                onPointerLeave={endCompPointer}
              >
                {comp.background.mode === "solid_color" && (
                  <div className="absolute inset-0" style={{ background: comp.background.solid_color }} />
                )}
                <video
                  ref={videoRef}
                  src={videoSrc ?? undefined}
                  onError={onVideoError}
                  className="absolute inset-0 w-full h-full"
                  style={{
                    objectFit: "cover",
                    transform: `scale(${comp.background.scale})`,
                    transformOrigin: `${comp.background.x * 100}% ${comp.background.y * 100}%`,
                    filter: `blur(${bgBlurPx}px) brightness(${bgBright.toFixed(2)}) contrast(${bgContrast.toFixed(2)})`,
                    opacity: comp.background.visible && comp.background.mode !== "solid_color" ? comp.background.opacity : 0,
                  }}
                  muted={muted}
                  controls={false}
                  playsInline
                  onTimeUpdate={(e) => setTime((e.target as HTMLVideoElement).currentTime)}
                  onLoadedMetadata={(e) => setDuration((e.target as HTMLVideoElement).duration)}
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                />
                {/* Foreground (drag to move) */}
                <div
                  className="absolute overflow-hidden cursor-move"
                  style={{
                    left: `${(fgBox.x / ccW) * 100}%`,
                    top: `${(fgBox.y / ccH) * 100}%`,
                    width: `${(fgBox.w / ccW) * 100}%`,
                    height: `${(fgBox.h / ccH) * 100}%`,
                    borderRadius: `${radPct.toFixed(1)}%`,
                    boxShadow: fgShadowCss,
                    border: comp.foreground.border.enabled ? `${comp.foreground.border.width}px solid ${comp.foreground.border.color}` : "none",
                  }}
                  onPointerDown={(e) => { e.stopPropagation(); beginCompPointer("fg")(e); }}
                  onPointerMove={(e) => { e.stopPropagation(); if (dragLayerRef.current === "fg") moveComp(e.clientX, e.clientY); }}
                  onPointerUp={(e) => { e.stopPropagation(); endCompPointer(); }}
                >
                  <video
                    ref={fgVideoRef}
                    src={videoSrc ?? undefined}
                    onError={onVideoError}
                    className="w-full h-full"
                    style={{
                      objectFit: "fill",
                      transform: fgUseReframe ? `scale(${fgContentZoom}) translate(${(0.5 - fgContentX) * 100}%, ${(0.5 - fgContentY) * 100}%)` : undefined,
                      transformOrigin: "center",
                    }}
                    muted
                    controls={false}
                    playsInline
                  />
                </div>
                {/* Tint + gradient veils */}
                {(comp.background.tint !== "none" || comp.background.tint_opacity > 0) && comp.background.tint !== "none" && (
                  <div className="absolute inset-0 pointer-events-none" style={{ background: comp.background.tint === "black" ? "#000" : comp.background.tint_color, opacity: comp.background.tint === "black" ? 0.35 : comp.background.tint_opacity }} />
                )}
                {comp.background.gradient && (
                  <div className="absolute inset-0 pointer-events-none" style={{ background: `linear-gradient(to bottom, ${comp.background.gradient_top}, ${comp.background.gradient_bottom})`, opacity: comp.background.gradient_opacity }} />
                )}
                {/* Safe-area guide (preview only, never rendered) */}
                {comp.captions.safe_area && (
                  <div className="absolute inset-x-[6%] top-[4%] bottom-[4%] border border-dashed border-white/25 rounded pointer-events-none">
                    <div className="absolute inset-x-0 text-center text-[9px] text-white/40 top-1">SAFE ZONE</div>
                  </div>
                )}
                {/* Caption placeholder at canvas position */}
                {showCapOverlay && (
                  <div className="absolute inset-x-0 text-center pointer-events-none" style={{ top: `${capFrac * 100}%`, transform: "translateY(-50%)" }}>
                    <span className="inline-block px-2 py-1 text-sm font-extrabold bg-black/55 rounded" style={{ fontSize: `${Math.max(11, 15 * (state.caption_scale || 1) * (comp.captions.scale || 1))}px` }}>
                      {state.caption_style || "viral_word"}
                    </span>
                  </div>
                )}
                {/* Progress / title / watermark overlays */}
                {comp.overlays.progress.enabled && (
                  <div className="absolute inset-x-0 pointer-events-none" style={{ top: comp.overlays.progress.position === "top" ? 0 : undefined, bottom: comp.overlays.progress.position === "bottom" ? 0 : undefined, height: comp.overlays.progress.height }}>
                    <div className="h-full" style={{ width: `${clipDuration > 0 ? Math.min(100, (time / clipDuration) * 100) : 0}%`, background: comp.overlays.progress.color }} />
                  </div>
                )}
                {comp.overlays.title.enabled && !!comp.overlays.title.text && (
                  <div className="absolute inset-x-0 text-center pointer-events-none font-bold" style={{ top: comp.overlays.title.position === "top" ? "8%" : comp.overlays.title.position === "center" ? "45%" : "85%", fontSize: `${15 * comp.overlays.title.scale}px` }}>
                    <span className="bg-black/55 px-2 py-1 rounded">{comp.overlays.title.text}</span>
                    {!!comp.overlays.title.subtitle && <div className="text-xs text-slate-200 mt-1"><span className="bg-black/55 px-2 py-0.5 rounded">{comp.overlays.title.subtitle}</span></div>}
                  </div>
                )}
                {comp.overlays.watermark.enabled && comp.overlays.watermark.kind === "text" && !!comp.overlays.watermark.text && (
                  <div className="absolute text-xs pointer-events-none" style={{ opacity: comp.overlays.watermark.opacity, top: comp.overlays.watermark.position.includes("top") ? 8 : undefined, bottom: comp.overlays.watermark.position.includes("bottom") ? 8 : undefined, left: comp.overlays.watermark.position.includes("left") ? 8 : undefined, right: comp.overlays.watermark.position.includes("right") ? 8 : undefined }}>
                    {comp.overlays.watermark.text}
                  </div>
                )}
                {fgUseReframe && <div className="absolute top-1 left-1 text-[9px] px-1.5 py-0.5 rounded bg-violet-600/80 pointer-events-none">REFRAME FG</div>}
              </div>
            ) : (
            <>
            {/* Live preview — SOURCE shows full frame, OUTPUT shows crop */}
            <div className="absolute inset-0 overflow-hidden bg-black">
              <video
                ref={videoRef}
                src={videoSrc ?? undefined}
                onError={onVideoError}
                className="w-full h-full object-cover"
                muted={muted}
                style={
                  !showSource && state.crop_mode === "manual"
                    ? {
                        transform: `scale(${previewZoom}) translate(${(0.5 - previewX) * 100}%, ${(0.5 - previewY) * 100}%)`,
                        transformOrigin: "center",
                      }
                    : undefined
                }
                controls={false}
                onTimeUpdate={(e) => setTime((e.target as HTMLVideoElement).currentTime)}
                onLoadedMetadata={(e) => setDuration((e.target as HTMLVideoElement).duration)}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
              />
            </div>
            {/* People-policy blur overlay — same normalized tracks as the FFmpeg render */}
            {peopleMasks.map((m) => (
              <div
                key={`ppl-${m.track_id}`}
                className="absolute pointer-events-none"
                style={{
                  left: `${m.box.x * 100}%`,
                  top: `${m.box.y * 100}%`,
                  width: `${m.box.w * 100}%`,
                  height: `${m.box.h * 100}%`,
                  backdropFilter: "blur(14px)",
                  background: "rgba(0,0,0,0.25)",
                  border: "1px dashed rgba(255,255,255,0.5)",
                }}
                title={`Person #${m.track_id} — BLUR`}
              />
            ))}
            {/* Exact crop guide — uses same math as renderer */}
            {state.crop_mode === "manual" && previewCrop && (
              <div
                className="absolute border-2 border-violet-500/80 bg-violet-500/10 pointer-events-none"
                style={{
                  left: `${(previewCrop.x / sourceW) * 100}%`,
                  top: `${(previewCrop.y / sourceH) * 100}%`,
                  width: `${(previewCrop.w / sourceW) * 100}%`,
                  height: `${(previewCrop.h / sourceH) * 100}%`,
                }}
              >
                <div className="absolute -top-1 -left-1 w-2 h-2 bg-violet-500 rounded-full" />
                <div className="absolute -bottom-1 -right-1 w-2 h-2 bg-violet-500 rounded-full" />
              </div>
            )}
            {state.crop_mode === "manual" && !previewCrop && (
              <div className="absolute inset-0 border-2 border-violet-500/60 pointer-events-none" />
            )}
            </>
            )}
          </div>
          )}

          {/* Timeline */}
          <div className="w-full max-w-[560px] space-y-2">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span>{time.toFixed(3)}s / {(source?.duration || duration || clipDuration).toFixed(1)}s</span>
              <div className="flex-1" />
              <button onClick={() => { const v = videoRef.current; if (v) seekBoth(Math.max(0, v.currentTime - 1/30)); }} className="px-2 py-1 rounded bg-white/10 text-[10px]">← Frame</button>
              <button onClick={togglePlayBoth} className="px-2 py-1 rounded bg-white/10">
                {playing ? "Pause" : "Play"}
              </button>
              <button onClick={() => { const v = videoRef.current; if (v) seekBoth(Math.min(source?.duration || duration || clipDuration, v.currentTime + 1/30)); }} className="px-2 py-1 rounded bg-white/10 text-[10px]">Frame →</button>
              <button onClick={() => setMuted(!muted)} className="px-2 py-1 rounded bg-white/10 text-[10px]">{muted ? "Unmute" : "Mute"}</button>
              <input type="range" min={0} max={1} step={0.05} value={volume} onChange={(e) => { const v = parseFloat(e.target.value); setVolume(v); if (videoRef.current) videoRef.current.volume = v; }} className="w-16 accent-violet-500" />
              <select value={playbackRate} onChange={(e) => { const v = parseFloat(e.target.value); setPlaybackRate(v); if (videoRef.current) videoRef.current.playbackRate = v; if (fgVideoRef.current) fgVideoRef.current.playbackRate = v; }} className="bg-white/10 rounded px-1 py-0.5 text-xs">
                <option value={0.5}>0.5x</option>
                <option value={1}>1x</option>
                <option value={1.5}>1.5x</option>
                <option value={2}>2x</option>
              </select>
              <button onClick={() => videoRef.current && videoRef.current.requestFullscreen()} className="px-2 py-1 rounded bg-white/10 text-[10px]">Fullscreen</button>
            </div>
            <div className="relative">
              {/* Filmstrip thumbnails */}
              {thumbs.length > 0 && (
                <div className="flex gap-px mb-1 h-10 overflow-hidden rounded bg-black/50 p-1">
                  {thumbs.map((th: any) => (
                    <img key={th.index} src={`${api}${th.url}`} alt="" className="h-full flex-1 object-cover rounded-sm opacity-80" />
                  ))}
                </div>
              )}
              {/* Trim range bar — draggable handles */}
              <div
                ref={trimBarRef}
                className="relative h-6 bg-white/5 rounded mb-1 touch-none select-none"
                onPointerMove={onTrimMove}
                onPointerUp={endTrim}
                onPointerLeave={endTrim}
              >
                <div
                  className="absolute top-0 bottom-0 bg-violet-600/30"
                  style={{
                    left: `${(state.source_start / (source?.duration || duration || clipDuration)) * 100}%`,
                    width: `${(clipDuration / (source?.duration || duration || clipDuration)) * 100}%`,
                  }}
                />
                <div className="absolute top-0 bottom-0 w-0.5 bg-white" style={{ left: `${(time / (source?.duration || duration || clipDuration)) * 100}%` }} />
                <div
                  className="absolute top-0 bottom-0 w-2.5 -ml-[5px] cursor-ew-resize bg-violet-500 rounded-sm border border-black/40 hover:bg-violet-400 transition-colors"
                  style={{ left: `${(state.source_start / (source?.duration || duration || clipDuration)) * 100}%` }}
                  onPointerDown={beginTrim("start")}
                  title="Drag to trim start"
                />
                <div
                  className="absolute top-0 bottom-0 w-2.5 -ml-[5px] cursor-ew-resize bg-violet-500 rounded-sm border border-black/40 hover:bg-violet-400 transition-colors"
                  style={{ left: `${(state.source_end / (source?.duration || duration || clipDuration)) * 100}%` }}
                  onPointerDown={beginTrim("end")}
                  title="Drag to trim end"
                />
              </div>
              <input
                type="range"
                min={0}
                max={source?.duration || duration || clipDuration}
                step={0.05}
                value={time}
                onChange={(e) => {
                  seekBoth(parseFloat(e.target.value));
                }}
                className="w-full accent-violet-500"
              />
              {/* Keyframe + cuts timeline */}
              <div className="absolute -top-1 left-0 right-0 h-2 pointer-events-none">
                {kfs.map((kf, i) => (
                  <div
                    key={`kf-${i}`}
                    className="absolute w-2 h-2 rounded-full bg-amber-400 border border-black -translate-x-1"
                    style={{ left: `${(kf.t / (source?.duration || duration || clipDuration)) * 100}%` }}
                    title={`Keyframe ${kf.t.toFixed(1)}s`}
                  />
                ))}
                {isSocialComp && comp.background.motion !== "none" && (
                  <div
                    className="absolute top-0 h-2 rounded bg-cyan-400/50"
                    style={{
                      left: `${(state.source_start / (source?.duration || duration || clipDuration)) * 100}%`,
                      width: `${(clipDuration / (source?.duration || duration || clipDuration)) * 100}%`,
                    }}
                    title={`BG motion: ${comp.background.motion}`}
                  />
                )}
                {(state.manual_cuts || []).map((cut: any, i: number) => (
                  <div
                    key={`cut-${i}`}
                    className="absolute top-0 w-0.5 h-2 bg-red-500"
                    style={{ left: `${(cut.start / (source?.duration || duration || clipDuration)) * 100}%` }}
                    title={`Cut ${cut.start.toFixed(1)}s`}
                  />
                ))}
              </div>
              {/* Caption blocks */}
              <div className="absolute top-3 left-0 right-0 h-1.5 flex gap-px opacity-60 pointer-events-none">
                {Array.from({ length: Math.ceil(source?.duration || duration || clipDuration) }).map((_, i) => (
                  <div key={i} className="flex-1 bg-violet-500/40 rounded-sm" style={{ opacity: 0.3 + (i % 2) * 0.4 }} />
                ))}
              </div>
            </div>
            <div className="flex items-center gap-2 text-[10px] text-slate-500">
              <span>Source: 0:00 → {(source?.duration || 0).toFixed(1)}s</span>
              <span className="text-violet-400">Render: {state.source_start.toFixed(1)}s → {state.source_end.toFixed(1)}s ({clipDuration.toFixed(1)}s)</span>
            </div>
            <div className="flex gap-1">
              <button
                onClick={() => {
                  const t = Math.round(time * 10) / 10;
                  const existing = (state.manual_cuts || []).find((c: any) => Math.abs(c.start - t) < 0.3);
                  if (existing) return;
                  const next = [...(state.manual_cuts || []), { start: t, end: t + 0.5, enabled: true }];
                  pushHistory({ ...state, manual_cuts: next });
                }}
                className="flex-1 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
              >
                + Cut @ {time.toFixed(1)}s
              </button>
              <button
                onClick={() => {
                  const t = time;
                  const idx = (state.manual_cuts || []).findIndex((c: any) => Math.abs(c.start - t) < 0.5);
                  if (idx >= 0) {
                    const next = (state.manual_cuts || []).filter((_: any, i: number) => i !== idx);
                    pushHistory({ ...state, manual_cuts: next });
                  }
                }}
                disabled={!(state.manual_cuts || []).some((c: any) => Math.abs(c.start - time) < 0.5)}
                className="px-2 py-1 rounded bg-white/10 text-xs disabled:opacity-30"
              >
                DEL Cut
              </button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => setTrimFromPlayhead("start")}
                onContextMenu={(e) => { e.preventDefault(); setTrimFromPlayhead("start"); }}
                className="py-1 rounded bg-violet-600/70 hover:bg-violet-600 text-xs font-bold"
              >
                ⟦ SET IN @ {time.toFixed(1)}s
              </button>
              <button
                onClick={() => setTrimFromPlayhead("end")}
                onContextMenu={(e) => { e.preventDefault(); setTrimFromPlayhead("end"); }}
                className="py-1 rounded bg-violet-600/70 hover:bg-violet-600 text-xs font-bold"
              >
                SET OUT @ {time.toFixed(1)}s ⟧
              </button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-xs">
                <span className="text-slate-500">START</span>
                <input
                  type="number"
                  step={0.1}
                  value={state.source_start.toFixed(2)}
                  onChange={(e) => pushHistory({ ...state, source_start: Math.min(parseFloat(e.target.value) || 0, state.source_end - 0.5) })}
                  className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-white"
                />
              </label>
              <label className="text-xs">
                <span className="text-slate-500">END</span>
                <input
                  type="number"
                  step={0.1}
                  value={state.source_end.toFixed(2)}
                  onChange={(e) => pushHistory({ ...state, source_end: Math.max(parseFloat(e.target.value) || 0, state.source_start + 0.5) })}
                  className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-white"
                />
              </label>
            </div>
            <div className="text-[10px] text-slate-500">Duration {(state.source_end - state.source_start).toFixed(2)}s • Source {clip.start_time.toFixed(1)}→{clip.end_time.toFixed(1)}s</div>
          </div>
        </div>

        {/* Right: controls */}
        <div className="w-[320px] border-l border-white/10 bg-[#111117] p-3 space-y-4 overflow-y-auto">
          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Framing</h3>
            <div className="grid grid-cols-2 gap-1 mb-2">
              <button onClick={() => pushHistory({ ...state, crop_mode: "auto" })} className={`py-1.5 rounded text-xs font-bold ${state.crop_mode === "auto" ? "bg-violet-600" : "bg-white/10"}`}>Auto</button>
              <button onClick={() => pushHistory({ ...state, crop_mode: "manual" })} className={`py-1.5 rounded text-xs font-bold ${state.crop_mode === "manual" ? "bg-violet-600" : "bg-white/10"}`}>Manual</button>
            </div>
            <div className="space-y-2">
              <label className="block text-xs">
                <span className="text-slate-500">Zoom {previewZoom.toFixed(2)}x {isInterpolated ? "(interp)" : ""}</span>
                <input
                  type="range"
                  min={0.5}
                  max={3}
                  step={0.05}
                  value={previewZoom}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    if (state.crop_mode === "manual" && kfs.length > 0) {
                      const t = Math.round(time * 10) / 10;
                      const idx = kfs.findIndex((k) => Math.abs(k.t - t) < 0.3);
                      if (idx >= 0) {
                        const nextKfs = kfs.map((k, i) => (i === idx ? { ...k, zoom: v } : k));
                        pushHistory({ ...state, crop_keyframes: nextKfs });
                      } else {
                        pushHistory({ ...state, zoom: v, crop_keyframes: [...kfs, { t, x: previewX, y: previewY, zoom: v }].sort((a, b) => a.t - b.t) });
                      }
                    } else {
                      pushHistory({ ...state, zoom: v });
                    }
                  }}
                  className="w-full accent-violet-500"
                />
              </label>
              <label className="block text-xs">
                <span className="text-slate-500">X {(previewX - 0.5).toFixed(2)}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={previewX}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    if (state.crop_mode === "manual" && kfs.length > 0) {
                      const t = Math.round(time * 10) / 10;
                      const idx = kfs.findIndex((k) => Math.abs(k.t - t) < 0.3);
                      if (idx >= 0) {
                        const nextKfs = kfs.map((k, i) => (i === idx ? { ...k, x: v } : k));
                        pushHistory({ ...state, crop_keyframes: nextKfs });
                      } else {
                        pushHistory({ ...state, x: v, crop_keyframes: [...kfs, { t, x: v, y: previewY, zoom: previewZoom }].sort((a, b) => a.t - b.t) });
                      }
                    } else {
                      pushHistory({ ...state, x: v });
                    }
                  }}
                  className="w-full accent-violet-500"
                />
              </label>
              <label className="block text-xs">
                <span className="text-slate-500">Y {(previewY - 0.5).toFixed(2)}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={previewY}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    if (state.crop_mode === "manual" && kfs.length > 0) {
                      const t = Math.round(time * 10) / 10;
                      const idx = kfs.findIndex((k) => Math.abs(k.t - t) < 0.3);
                      if (idx >= 0) {
                        const nextKfs = kfs.map((k, i) => (i === idx ? { ...k, y: v } : k));
                        pushHistory({ ...state, crop_keyframes: nextKfs });
                      } else {
                        pushHistory({ ...state, y: v, crop_keyframes: [...kfs, { t, x: previewX, y: v, zoom: previewZoom }].sort((a, b) => a.t - b.t) });
                      }
                    } else {
                      pushHistory({ ...state, y: v });
                    }
                  }}
                  className="w-full accent-violet-500"
                />
              </label>
              <div className="grid grid-cols-3 gap-1">
                <button onClick={() => pushHistory({ ...state, x: 0.5, y: 0.5, zoom: 1 })} className="py-1 rounded bg-white/10 text-xs">Center</button>
                <button onClick={() => pushHistory({ ...state, zoom: 1 })} className="py-1 rounded bg-white/10 text-xs">Fit</button>
                <button onClick={() => pushHistory({ ...state, x: 0.5, y: 0.5 })} className="py-1 rounded bg-white/10 text-xs">Reset</button>
              </div>
              <p className="text-[10px] text-slate-500">Drag: use X/Y sliders (live preview, no re-encode). Final render uses FFmpeg.</p>
              {state.crop_mode === "manual" && (
                <div className="pt-2 border-t border-white/5 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold text-slate-400">Keyframes {kfs.length > 0 ? `(${kfs.length})` : ""}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${isInterpolated ? "bg-amber-500/20 text-amber-300" : "bg-emerald-500/20 text-emerald-300"}`}>
                      {isInterpolated ? "INTERPOLATED" : kfs.some((k) => Math.abs(k.t - time) < 0.15) ? "KEYFRAME" : "—"}
                    </span>
                  </div>
                  <div className="flex gap-1">
                    <button
                      onClick={() => {
                        const t = Math.round(time * 10) / 10;
                        const existingIdx = kfs.findIndex((k) => Math.abs(k.t - t) < 0.2);
                        const newKf = { t, x: previewX, y: previewY, zoom: previewZoom };
                        let nextKfs: typeof kfs;
                        if (existingIdx >= 0) {
                          nextKfs = kfs.map((k, i) => (i === existingIdx ? newKf : k));
                        } else {
                          nextKfs = [...kfs, newKf].sort((a, b) => a.t - b.t);
                        }
                        pushHistory({ ...state, crop_keyframes: nextKfs });
                      }}
                      className="flex-1 py-1 rounded bg-violet-600 hover:bg-violet-700 text-xs font-bold"
                    >
                      ADD KEYFRAME @ {time.toFixed(1)}s
                    </button>
                    <button
                      onClick={() => {
                        const idx = kfs.findIndex((k) => Math.abs(k.t - time) < 0.3);
                        if (idx >= 0) {
                          const nextKfs = kfs.filter((_, i) => i !== idx);
                          pushHistory({ ...state, crop_keyframes: nextKfs });
                        }
                      }}
                      disabled={!kfs.some((k) => Math.abs(k.t - time) < 0.3)}
                      className="px-2 py-1 rounded bg-white/10 text-xs disabled:opacity-30"
                    >
                      DEL
                    </button>
                  </div>
                  {kfs.length > 0 && (
                    <div className="flex gap-1 flex-wrap">
                      {kfs.map((kf, i) => (
                        <button
                          key={i}
                          onClick={() => seekBoth(kf.t)}
                          className={`text-[10px] px-1.5 py-0.5 rounded border ${Math.abs(kf.t - time) < 0.15 ? "bg-violet-600 border-violet-500 text-white" : "bg-white/5 border-white/10 text-slate-400"}`}
                        >
                          {kf.t.toFixed(1)}s
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="text-[10px] text-slate-500">
                    Preview: x {previewX.toFixed(2)} y {previewY.toFixed(2)} zoom {previewZoom.toFixed(2)} {isInterpolated ? "(interp)" : ""}
                  </div>
                </div>
              )}
            </div>
          </div>

          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Composition</h3>
            <div className="grid grid-cols-2 gap-1 mb-2">
              <button onClick={() => pushComp((c) => ({ ...c, mode: "reframe" }))} className={`py-1.5 rounded text-xs font-bold ${comp.mode === "reframe" ? "bg-violet-600" : "bg-white/10"}`}>Reframe</button>
              <button onClick={() => pushComp((c) => ({ ...c, mode: "social" }))} className={`py-1.5 rounded text-xs font-bold ${comp.mode === "social" ? "bg-violet-600" : "bg-white/10"}`}>Social</button>
            </div>
            {comp.mode === "social" && (
              <div className="space-y-2">
                <div>
                  <div className="text-slate-500 text-xs mb-1">Canvas {comp.canvas}</div>
                  <div className="grid grid-cols-4 gap-1">
                    {["9:16", "1:1", "4:5", "16:9"].map((a) => (
                      <button key={a} onClick={() => { if (state) pushHistory({ ...state, canvas_preset: a, output_aspect: a, composition: { ...comp, canvas: a } }); }} className={`py-1 rounded text-xs ${comp.canvas === a ? "bg-violet-600" : "bg-white/10"}`}>{a}</button>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="text-slate-500 text-xs mb-1">Presets</div>
                  <div className="grid grid-cols-2 gap-1">
                    {["classic_vertical", "blur_16x9", "dark_blur", "gradient", "floating"].map((p) => (
                      <button key={p} onClick={() => applyPreset(p)} className="py-1 rounded text-[11px] bg-white/10 hover:bg-white/20">{p.replace(/_/g, " ")}</button>
                    ))}
                    <button onClick={autoSocial} className="py-1 rounded text-[11px] font-bold bg-emerald-600 hover:bg-emerald-700 col-span-2">AUTO SOCIAL</button>
                  </div>
                </div>
                <div className="pt-2 border-t border-white/5 space-y-2">
                  <div className="text-slate-500 text-xs font-bold">BACKGROUND {comp.background.follow_foreground ? "(follows FG)" : ""}</div>
                  {compSelect("Mode", comp.background.mode, ["blurred_video", "darkened_video", "solid_color", "gradient"], (c, v) => ({ ...c, background: { ...c.background, mode: v } }))}
                  {compSelect("Blur", comp.background.blur, ["none", "light", "medium", "heavy"], (c, v) => ({ ...c, background: { ...c.background, blur: v, blur_value: BLUR_SIGMA[v] ?? 18 } }))}
                  {compSelect("Darkness", comp.background.darkness, ["natural", "subtle", "dark", "very_dark"], (c, v) => ({ ...c, background: { ...c.background, darkness: v } }))}
                  {compSlider("Brightness", comp.background.brightness, -0.3, 0.3, 0.01, (c, v) => ({ ...c, background: { ...c.background, brightness: v } }), comp.background.brightness.toFixed(2))}
                  {compSlider("Contrast", comp.background.contrast, 0.8, 1.2, 0.01, (c, v) => ({ ...c, background: { ...c.background, contrast: v } }), comp.background.contrast.toFixed(2))}
                  {compSlider("Zoom", comp.background.scale, 1.0, 1.5, 0.01, (c, v) => ({ ...c, background: { ...c.background, scale: v } }), comp.background.scale.toFixed(2))}
                  {compSlider("BG X", comp.background.x, 0, 1, 0.01, (c, v) => ({ ...c, background: { ...c.background, x: v } }))}
                  {compSlider("BG Y", comp.background.y, 0, 1, 0.01, (c, v) => ({ ...c, background: { ...c.background, y: v } }))}
                  {compSelect("Motion", comp.background.motion, ["none", "slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right"], (c, v) => ({ ...c, background: { ...c.background, motion: v } }))}
                  {compCheck("Background follows foreground", comp.background.follow_foreground, (c, v) => ({ ...c, background: { ...c.background, follow_foreground: v } }))}
                  {compCheck("Gradient veil", comp.background.gradient, (c, v) => ({ ...c, background: { ...c.background, gradient: v } }))}
                  <button onClick={() => resetCompPart("background")} className="py-1 rounded bg-white/10 text-xs w-full">Reset background</button>
                </div>
                <div className="pt-2 border-t border-white/5 space-y-2">
                  <div className="text-slate-500 text-xs font-bold">FOREGROUND</div>
                  {compSelect("Content", comp.foreground.fit, ["fit", "reframe"], (c, v) => ({ ...c, foreground: { ...c.foreground, fit: v } }))}
                  <div className="grid grid-cols-4 gap-1">
                    {["top", "center", "bottom", "custom"].map((p) => (
                      <button key={p} onClick={() => pushComp((c) => ({ ...c, foreground: { ...c.foreground, position_preset: p, y: p === "top" ? 0 : p === "bottom" ? 1 : p === "center" ? 0.5 : c.foreground.y } } as Composition))} className={`py-1 rounded text-[11px] ${comp.foreground.position_preset === p ? "bg-violet-600" : "bg-white/10"}`}>{p}</button>
                    ))}
                  </div>
                  {compSlider("Scale", comp.foreground.scale, 0.75, 1.25, 0.01, (c, v) => ({ ...c, foreground: { ...c.foreground, scale: v } }), comp.foreground.scale.toFixed(2))}
                  {compSlider("FG X", comp.foreground.x, 0, 1, 0.01, (c, v) => ({ ...c, foreground: { ...c.foreground, x: v, position_preset: "custom" } }))}
                  {compSlider("FG Y", comp.foreground.y, 0, 1, 0.01, (c, v) => ({ ...c, foreground: { ...c.foreground, y: v, position_preset: "custom" } }))}
                  {compSlider("Corners", comp.foreground.radius, 0, 48, 1, (c, v) => ({ ...c, foreground: { ...c.foreground, radius: v } }), comp.foreground.radius === 0 ? "square" : `${comp.foreground.radius}px`)}
                  {compSelect("Shadow", comp.foreground.shadow, ["none", "soft", "strong"], (c, v) => ({ ...c, foreground: { ...c.foreground, shadow: v } }))}
                  {compCheck("Border", comp.foreground.border.enabled, (c, v) => ({ ...c, foreground: { ...c.foreground, border: { ...c.foreground.border, enabled: v } } }))}
                  <button onClick={() => resetCompPart("foreground")} className="py-1 rounded bg-white/10 text-xs w-full">Reset foreground</button>
                </div>
                <div className="pt-2 border-t border-white/5 space-y-2">
                  <div className="text-slate-500 text-xs font-bold">LAYERS</div>
                  {compCheck("Background", comp.background.visible, (c, v) => ({ ...c, background: { ...c.background, visible: v } }))}
                  {compCheck("Progress bar", comp.overlays.progress.enabled, (c, v) => ({ ...c, overlays: { ...c.overlays, progress: { ...c.overlays.progress, enabled: v } } }))}
                  {compCheck("Watermark", comp.overlays.watermark.enabled, (c, v) => ({ ...c, overlays: { ...c.overlays, watermark: { ...c.overlays.watermark, enabled: v } } }))}
                  {compCheck("Title", comp.overlays.title.enabled, (c, v) => ({ ...c, overlays: { ...c.overlays, title: { ...c.overlays.title, enabled: v } } }))}
                </div>
                <div className="pt-2 border-t border-white/5 space-y-2">
                  <div className="text-slate-500 text-xs font-bold">OVERLAYS</div>
                  {compSelect("Progress position", comp.overlays.progress.position, ["top", "bottom"], (c, v) => ({ ...c, overlays: { ...c.overlays, progress: { ...c.overlays.progress, position: v } } }))}
                  <label className="block text-xs">
                    <span className="text-slate-500">Watermark text</span>
                    <input type="text" value={comp.overlays.watermark.text} onChange={(e) => pushComp((c) => ({ ...c, overlays: { ...c.overlays, watermark: { ...c.overlays.watermark, enabled: true, kind: "text", text: e.target.value } } }))} placeholder="optional" className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm" />
                  </label>
                  {compSelect("Watermark position", comp.overlays.watermark.position, ["top-left", "top-right", "bottom-left", "bottom-right", "center"], (c, v) => ({ ...c, overlays: { ...c.overlays, watermark: { ...c.overlays.watermark, position: v } } }))}
                  <label className="block text-xs">
                    <span className="text-slate-500">Title</span>
                    <input type="text" value={comp.overlays.title.text} onChange={(e) => pushComp((c) => ({ ...c, overlays: { ...c.overlays, title: { ...c.overlays.title, enabled: true, text: e.target.value } } }))} placeholder="optional" className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm" />
                  </label>
                  <label className="block text-xs">
                    <span className="text-slate-500">Subtitle</span>
                    <input type="text" value={comp.overlays.title.subtitle} onChange={(e) => pushComp((c) => ({ ...c, overlays: { ...c.overlays, title: { ...c.overlays.title, enabled: true, subtitle: e.target.value } } }))} placeholder="optional" className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm" />
                  </label>
                  <button onClick={() => resetCompPart("all")} className="py-1 rounded bg-white/10 text-xs w-full">Reset composition</button>
                </div>
              </div>
            )}
          </div>

          <div>
            <PeoplePolicyPanel
              clipId={clipId}
              cfg={state.people_policy ?? null}
              onChange={(cfg) => pushHistory({ ...state, people_policy: cfg })}
              sourceStart={state.source_start}
              sourceEnd={state.source_end}
              sourcePath={source?.path ?? null}
              time={time}
              onMasks={onPeopleMasks}
            />
          </div>

          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Captions</h3>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={state.caption_enabled} onChange={(e) => pushHistory({ ...state, caption_enabled: e.target.checked })} />
              Enabled
            </label>
            <select value={state.caption_style || "viral_word"} onChange={(e) => pushHistory({ ...state, caption_style: e.target.value })} className="w-full mt-2 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm">
              <option value="viral_word">Viral Word</option>
              <option value="classic_white">Classic White</option>
              <option value="highlight_yellow">Highlight Yellow</option>
              <option value="tiktok_style">TikTok</option>
              <option value="neon_green">Neon Green</option>
              <option value="minimal_clean">Minimal</option>
              <option value="none">None</option>
            </select>
            <label className="block text-xs mt-2">
              <span className="text-slate-500">Scale {state.caption_scale.toFixed(2)}x</span>
              <input type="range" min={0.7} max={1.5} step={0.05} value={state.caption_scale} onChange={(e) => { const v = parseFloat(e.target.value); if (state) pushHistory({ ...state, caption_scale: v, composition: { ...comp, captions: { ...comp.captions, scale: v } } }); }} className="w-full accent-violet-500" />
            </label>
            <label className="block text-xs mt-2">
              <span className="text-slate-500">Position (canvas)</span>
              <select value={comp.captions.position} onChange={(e) => pushComp((c) => ({ ...c, captions: { ...c.captions, position: e.target.value } }))} className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm">
                {["top", "center", "bottom", "custom", "off"].map((p) => (<option key={p} value={p}>{p}</option>))}
              </select>
            </label>
          </div>

          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Canvas / Export</h3>
            <div className="grid grid-cols-4 gap-1">
              {["9:16", "1:1", "4:5", "16:9"].map((a) => (
                <button key={a} onClick={() => { if (state) pushHistory({ ...state, output_aspect: a, canvas_preset: a, composition: { ...comp, canvas: a } }); }} className={`py-1 rounded text-xs ${comp.canvas === a ? "bg-violet-600" : "bg-white/10"}`}>{a}</button>
              ))}
            </div>
            <label className="block text-xs mt-2">
              <span className="text-slate-500">Export preset</span>
              <select value={state.export_preset || ""} onChange={(e) => pushHistory({ ...state, export_preset: e.target.value || null })} className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-sm">
                <option value="">Canvas default</option>
                <option value="social_9_16_1080">Social 9:16 — 1080×1920</option>
                <option value="social_9_16_720">Social 9:16 — 720×1280</option>
                <option value="square_1_1">Square 1:1 — 1080×1080</option>
                <option value="portrait_4_5">Portrait 4:5 — 1080×1350</option>
                <option value="landscape_16_9">Landscape 16:9 — 1920×1080</option>
              </select>
            </label>
          </div>

          {error && <div className="text-xs text-red-400 p-2 rounded bg-red-500/10">{error}</div>}
        </div>
      </div>
    </div>
  );
}
