"use client";

import { useEffect, useMemo, useState } from "react";
import { getApiBaseUrl, getPeopleTracks, reviewPersonTrack } from "@/lib/api";

export type PeopleCfg = {
    mode: "OFF" | "BLUR_ALL" | "MANUAL" | "REVIEW";
    blur_type: "soft" | "strong" | "pixelate" | "mask";
    region_mode: "face" | "upper" | "full";
    /** legacy shape accepted on load */
    enabled?: boolean;
};

export const DEFAULT_PEOPLE_CFG: PeopleCfg = {
    mode: "OFF",
    blur_type: "soft",
    region_mode: "full",
};

function coerceMode(v: any): PeopleCfg["mode"] {
    if (v?.mode === "BLUR_ALL" || v?.mode === "MANUAL" || v?.mode === "REVIEW" || v?.mode === "OFF") return v.mode;
    if (v?.enabled) return "BLUR_ALL";
    return "OFF";
}

type Props = {
    clipId: string;
    cfg: PeopleCfg | null;
    onChange: (cfg: PeopleCfg) => void;
    sourceStart: number;
    sourceEnd: number;
    sourcePath: string | null;
    /** Current playhead time in source-relative seconds (for mask overlay). */
    time: number;
    onMasks: (masks: { track_id: number; box: { x: number; y: number; w: number; h: number } }[]) => void;
};

export default function PeoplePolicyPanel({ clipId, cfg, onChange, sourceStart, sourceEnd, sourcePath, time, onMasks }: Props) {
    const api = getApiBaseUrl();
    const [tracks, setTracks] = useState<any[]>([]);
    const [gate, setGate] = useState<any>(null);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [origin, setOrigin] = useState<"precheck" | "render" | null>(null);
    const [manual, setManual] = useState({ s: 0, e: 5, x: 0.3, y: 0.2, w: 0.4, h: 0.5 });

    const c: PeopleCfg = useMemo(() => ({
        mode: coerceMode(cfg),
        blur_type: (cfg as any)?.blur_type ?? "soft",
        region_mode: (cfg as any)?.region_mode ?? "full",
    }), [cfg]);
    const set = (patch: Partial<PeopleCfg>) => onChange({ ...c, ...patch });

    const active = useMemo(() => {
        const out: any[] = [];
        for (const t of tracks) {
            if (!t.blur && t.action !== "BLUR") continue;
            const boxes = t.boxes || [];
            if (!boxes.length) continue;
            if (time < boxes[0].t || time > boxes[boxes.length - 1].t) continue;
            let best = boxes[0];
            for (const b of boxes) if (Math.abs(b.t - time) < Math.abs(best.t - time)) best = b;
            out.push({ track_id: t.track_id, box: best.box });
        }
        return out;
    }, [tracks, time]);

    useEffect(() => { onMasks(active); }, [active, onMasks]);

    const precheck = async () => {
        if (!sourcePath) { setErr("No source video resolved yet."); return; }
        if (c.mode === "OFF") { setErr("Select a masking mode first."); return; }
        setBusy(true); setErr("");
        try {
            const r = await fetch(`${api}/api/automation/clips/people-check`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    video_path: sourcePath, start: sourceStart, end: sourceEnd,
                    region_mode: c.region_mode, mode: c.mode, clip_id: null,
                }),
            });
            if (!r.ok) throw new Error(await r.text());
            const j = await r.json();
            setTracks((j.analysis?.tracks || []).map((t: any) => ({ ...t, action: t.blur ? "BLUR" : "KEEP" })));
            setGate(j.gate);
            setOrigin("precheck");
        } catch (e: any) { setErr(e.message || "pre-check failed"); }
        setBusy(false);
    };

    const loadRenderTracks = async () => {
        setBusy(true); setErr("");
        try {
            const t = await getPeopleTracks(clipId);
            setTracks(t);
            setOrigin("render");
            const blurred = t.filter((x: any) => x.blur || x.action === "BLUR").length;
            const pend = t.filter((x: any) => (x.review_status || "PENDING") === "PENDING").length;
            setGate(pend && c.mode !== "BLUR_ALL" ? { gate: "NEEDS_REVIEW", reason: "unreviewed_tracks" } : blurred ? { gate: "PASS_WITH_BLUR" } : { gate: "PASS" });
        } catch (e: any) { setErr(e.message || "load failed"); }
        setBusy(false);
    };

    const review = async (track_id: number, action: "BLUR" | "KEEP") => {
        try {
            await reviewPersonTrack(clipId, track_id, action);
            setTracks((ts) => ts.map((t) => (t.track_id === track_id ? { ...t, action, blur: action === "BLUR", reviewer: "owner", review_status: "APPROVED", source: "manual" } : t)));
        } catch (e: any) { setErr(e.message || "review failed"); }
    };

    const addManual = async () => {
        setBusy(true); setErr("");
        try {
            const r = await fetch(`${api}/api/automation/clips/${clipId}/people/manual`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start_time: manual.s, end_time: manual.e,
                    box: { x: manual.x, y: manual.y, w: manual.w, h: manual.h },
                    blur: true, reviewer: "owner",
                }),
            });
            if (!r.ok) throw new Error(await r.text());
            await loadRenderTracks();
        } catch (e: any) { setErr(e.message || "manual region failed"); }
        setBusy(false);
    };

    const blurred = tracks.filter((t) => t.blur || t.action === "BLUR").length;
    const pending = tracks.filter((t) => (t.review_status || "PENDING") === "PENDING").length;

    return (
        <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">People masking</h3>
            <label className="block text-xs">
                <span className="text-slate-500">Mode (generic — no identity classification)</span>
                <select value={c.mode} onChange={(e) => set({ mode: e.target.value as any })}
                    className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-white">
                    <option value="OFF">Off</option>
                    <option value="BLUR_ALL">Blur all detected people</option>
                    <option value="MANUAL">Manual selection</option>
                    <option value="REVIEW">Review detected people</option>
                </select>
            </label>
            <p className="mt-1 text-[10px] text-slate-500">
                Detects generic person regions only. Render-time detection is authoritative; preview overlays are advisory.
            </p>
            {c.mode !== "OFF" && (
                <div className="mt-2 space-y-2 text-xs">
                    <div className="grid grid-cols-2 gap-1">
                        <label className="block">
                            <span className="text-slate-500">Blur style</span>
                            <select value={c.blur_type} onChange={(e) => set({ blur_type: e.target.value as any })}
                                className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-white">
                                <option value="soft">Soft blur</option>
                                <option value="strong">Strong blur</option>
                                <option value="pixelate">Pixelation</option>
                                <option value="mask">Solid mask</option>
                            </select>
                        </label>
                        <label className="block">
                            <span className="text-slate-500">Region</span>
                            <select value={c.region_mode} onChange={(e) => set({ region_mode: e.target.value as any })}
                                className="w-full mt-1 bg-white/5 border border-white/10 rounded px-2 py-1 text-white">
                                <option value="full">Full person</option>
                                <option value="upper">Upper body</option>
                                <option value="face">Face only</option>
                            </select>
                        </label>
                    </div>
                    <div className="flex gap-1">
                        <button onClick={precheck} disabled={busy}
                            className="flex-1 py-1 rounded bg-white/10 hover:bg-white/20 text-xs disabled:opacity-40">
                            {busy ? "Analyzing…" : "Pre-check source range"}
                        </button>
                        <button onClick={loadRenderTracks} disabled={busy}
                            className="flex-1 py-1 rounded bg-white/10 hover:bg-white/20 text-xs disabled:opacity-40">
                            Load render tracks
                        </button>
                    </div>
                    {err && <div className="text-[11px] text-amber-300">{err}</div>}
                    {(tracks.length > 0 || gate) && (
                        <div className="rounded bg-white/5 p-2 text-[11px]">
                            <div>Detected people: <b>{tracks.length}</b> · Blur: <b>{blurred}</b> · Pending review: <b>{pending}</b></div>
                            {gate && <div className="mt-1">Gate: <b>{gate.gate}</b> ({gate.reason || "—"})</div>}
                            {origin === "precheck" && <div className="text-slate-500">Advisory: source pixels. Render re-detects on output pixels.</div>}
                            {origin === "render" && <div className="text-slate-500">Authoritative: final output pixels — same data as FFmpeg masks.</div>}
                        </div>
                    )}
                    {tracks.map((t) => (
                        <div key={t.track_id} className="rounded bg-white/5 p-2 text-[11px]">
                            <div>Person #{t.track_id} · {(t.start_time ?? 0).toFixed(1)} → {(t.end_time ?? 0).toFixed(1)}s
                                <span className="text-slate-500"> · {t.source || "automatic"} · conf {(t.confidence ?? 0).toFixed(2)}</span></div>
                            <div>Status: <b>{(t.blur || t.action === "BLUR") ? "Blurred" : (t.review_status || "PENDING") === "PENDING" ? "Needs Review" : "Visible"}</b></div>
                            <div className="mt-1 flex gap-1">
                                <button onClick={() => review(t.track_id, "BLUR")}
                                    className={`flex-1 py-0.5 rounded text-[11px] ${(t.blur || t.action === "BLUR") ? "bg-violet-600" : "bg-white/10"}`}>Blur</button>
                                <button onClick={() => review(t.track_id, "KEEP")}
                                    className={`flex-1 py-0.5 rounded text-[11px] ${!(t.blur || t.action === "BLUR") && t.review_status === "APPROVED" ? "bg-violet-600" : "bg-white/10"}`}>Keep Visible</button>
                            </div>
                        </div>
                    ))}
                    <div className="rounded bg-white/5 p-2 text-[11px]">
                        <div className="font-bold">Manual region (output-relative 0–1)</div>
                        <div className="mt-1 grid grid-cols-3 gap-1">
                            {(["s", "e", "x", "y", "w", "h"] as const).map((k) => (
                                <label key={k} className="block">
                                    <span className="text-slate-500">{k}</span>
                                    <input type="number" step={k === "s" || k === "e" ? 0.5 : 0.05} value={manual[k]}
                                        onChange={(e) => setManual({ ...manual, [k]: parseFloat(e.target.value) || 0 })}
                                        className="w-full bg-white/5 border border-white/10 rounded px-1 py-0.5 text-white" />
                                </label>
                            ))}
                        </div>
                        <button onClick={addManual} disabled={busy}
                            className="mt-1 w-full py-1 rounded bg-white/10 hover:bg-white/20 text-xs disabled:opacity-40">Add manual mask</button>
                    </div>
                </div>
            )}
        </div>
    );
}
