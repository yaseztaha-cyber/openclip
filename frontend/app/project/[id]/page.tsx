"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getProject, getApiBaseUrl, retryProject, reprocessClip, getCaptionStyles, getCropPreview, CropPreview, getProjectZipUrl } from "@/lib/api";
import { Project, Clip, CaptionStyle } from "@/lib/types";

function formatDuration(s: number) {
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}:${sec.toString().padStart(2, "0")}`;
}

function getYouTubeId(url: string) {
    const m = url.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&?/]+)/);
    return m ? m[1] : null;
}

const STAGE_LABEL: Record<string, string> = {
    downloading: "Downloading video…",
    transcribing: "Extracting captions…",
    analyzing: "AI analyzing…",
    processing: "Cutting clips…",
    done: "Complete",
    error: "Failed",
};

export default function ProjectPage() {
    const params = useParams();
    const router = useRouter();
    const id = params.id as string;

    const [project, setProject] = useState<(Project & { clips: Clip[] }) | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [selectedClip, setSelectedClip] = useState<Clip | null>(null);
    const [wsProgress, setWsProgress] = useState<{ stage: string; percent: number; message: string } | null>(null);
    const [logs, setLogs] = useState<string[]>([]);
    const [retrying, setRetrying] = useState(false);
    const [retryError, setRetryError] = useState<string | null>(null);
    const [adjustId, setAdjustId] = useState<string | null>(null);
    const [adjLayout, setAdjLayout] = useState("auto");
    const [adjAspect, setAdjAspect] = useState("9:16");
    const [adjTracking, setAdjTracking] = useState(true);
    const [adjZoom, setAdjZoom] = useState(1);
    const [adjShift, setAdjShift] = useState(0);
    const [adjY, setAdjY] = useState(0);
    const [adjCaption, setAdjCaption] = useState("keep");
    const [applying, setApplying] = useState(false);
    const [applyError, setApplyError] = useState<string | null>(null);
    const [captionStyles, setCaptionStyles] = useState<CaptionStyle[]>([]);
    const [preview, setPreview] = useState<CropPreview | null>(null);
    const [pvLoading, setPvLoading] = useState(false);
    const [dragging, setDragging] = useState(false);
    const pvTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const imgBox = useRef<HTMLDivElement | null>(null);
    const [batchJob, setBatchJob] = useState<any | null>(null);
    const batchTimer = useRef<ReturnType<typeof setInterval> | null>(null);

    useEffect(() => {
        return () => {
            if (batchTimer.current) clearInterval(batchTimer.current);
        };
    }, []);

    const handleExportAll = async () => {
        const base = getApiBaseUrl();
        setBatchJob({ status: "queued", percent: 0, message: "Queued…" });
        try {
            const r = await fetch(`${base}/api/projects/${id}/render-all`, { method: "POST" });
            if (!r.ok) throw new Error(await r.text());
            const j = await r.json();
            if (batchTimer.current) clearInterval(batchTimer.current);
            batchTimer.current = setInterval(async () => {
                try {
                    const rr = await fetch(`${base}/api/projects/${id}/render-job?job_id=${j.job_id}`, { cache: "no-store" });
                    if (!rr.ok) return;
                    const st = await rr.json();
                    setBatchJob(st);
                    if (st.status === "done" || st.status === "error" || st.status === "cancelled") {
                        if (batchTimer.current) clearInterval(batchTimer.current);
                        setProject(prev => prev ? { ...prev, clips: (st?.clips && Array.isArray(st.clips)) ? st.clips : prev.clips } : prev);
                        if (st.status === "done") alert(`Exported ${st.total ?? "all"} clips successfully.`);
                    }
                } catch { /* keep polling */ }
            }, 1500);
        } catch (e: any) {
            setBatchJob(null);
            alert(e.message || "Batch export failed — is the backend running?");
        }
    };

    useEffect(() => {
        getCaptionStyles().then(setCaptionStyles).catch(() => {});
    }, []);

    const loadPreview = async (
        clipId: string,
        p: { aspect: string; zoom: number; x: number; y: number; layout: string; tracking: boolean },
    ) => {
        setPvLoading(true);
        try {
            const pv = await getCropPreview(clipId, {
                aspect: p.aspect,
                zoom_boost: p.zoom,
                x_offset: p.x / 100,
                y_offset: p.y / 100,
                layout_mode: p.layout,
                tracking: p.tracking,
            });
            setPreview(pv);
        } catch {
            setPreview(null);
        } finally {
            setPvLoading(false);
        }
    };

    const schedulePreview = (
        clipId: string,
        p: { aspect: string; zoom: number; x: number; y: number; layout: string; tracking: boolean },
    ) => {
        if (pvTimer.current) clearTimeout(pvTimer.current);
        pvTimer.current = setTimeout(() => loadPreview(clipId, p), 600);
    };

    const openAdjust = (clip: Clip) => {
        if (adjustId === clip.id) {
            setAdjustId(null);
            setPreview(null);
            return;
        }
        setAdjustId(clip.id);
        setApplyError(null);
        const a = {
            aspect: "9:16",
            zoom: 1, x: 0, y: 0,
            layout: "auto", tracking: true,
        };
        setAdjAspect(a.aspect);
        setAdjZoom(a.zoom);
        setAdjShift(a.x);
        setAdjY(a.y);
        setAdjLayout(a.layout);
        setAdjTracking(a.tracking);
        setAdjCaption("keep");
        loadPreview(clip.id, a);
    };

    const tweak = (clipId: string, patch: Partial<{ aspect: string; zoom: number; x: number; y: number; layout: string; tracking: boolean }>) => {
        const cur = {
            aspect: patch.aspect ?? adjAspect,
            zoom: patch.zoom ?? adjZoom,
            x: patch.x ?? adjShift,
            y: patch.y ?? adjY,
            layout: patch.layout ?? adjLayout,
            tracking: patch.tracking ?? adjTracking,
        };
        if (patch.aspect !== undefined) setAdjAspect(patch.aspect);
        if (patch.zoom !== undefined) setAdjZoom(patch.zoom);
        if (patch.x !== undefined) setAdjShift(patch.x);
        if (patch.y !== undefined) setAdjY(patch.y);
        if (patch.layout !== undefined) setAdjLayout(patch.layout);
        if (patch.tracking !== undefined) setAdjTracking(patch.tracking);
        schedulePreview(clipId, cur);
    };

    const onPreviewDrag = useCallback((e: React.MouseEvent, clipId: string) => {
        if (!dragging || !preview || !imgBox.current) return;
        const rect = imgBox.current.getBoundingClientRect();
        const px = ((e.clientX - rect.left) / rect.width) * preview.width;
        const py = ((e.clientY - rect.top) / rect.height) * preview.height;
        const k = preview.width / preview.frame.w;
        const baseX = preview.base.cx * k;
        const baseY = preview.base.cy * k;
        const slackX = preview.slack.x * k;
        const slackY = preview.slack.y * k;
        const wantCx = px;
        const wantCy = py;
        const nx = slackX > 1 ? Math.max(-100, Math.min(100, ((wantCx - baseX) / (slackX / 2)) * 100)) : 0;
        const ny = slackY > 1 ? Math.max(-100, Math.min(100, ((wantCy - baseY) / (slackY / 2)) * 100)) : 0;
        setAdjShift(Math.round(nx));
        setAdjY(Math.round(ny));
        schedulePreview(clipId, {
            aspect: adjAspect, zoom: adjZoom, x: Math.round(nx), y: Math.round(ny),
            layout: adjLayout, tracking: adjTracking,
        });
    }, [dragging, preview, adjAspect, adjZoom, adjLayout, adjTracking]);

    const handleDownload = async (clip: Clip) => {
        try {
            const url = `${getApiBaseUrl()}${clip.file_path}`;
            const res = await fetch(url);
            if (!res.ok) throw new Error("fetch failed");
            const blob = await res.blob();
            const blobUrl = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = blobUrl;
            a.download = clip.title ? `${clip.title.replace(/[^a-zA-Z0-9 _-]/g, "")}.mp4` : `clip_${clip.id.slice(0, 8)}.mp4`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(blobUrl), 4000);
        } catch {
            // Fallback: open in new tab
            window.open(`${getApiBaseUrl()}${clip.file_path}`, "_blank");
        }
    };

    const handleApplyAdjust = async (clip: Clip) => {
        setApplying(true);
        setApplyError(null);
        try {
            const opts: Record<string, string | number | boolean> = {
                aspect: adjAspect,
                tracking: adjTracking,
            };
            if (adjLayout !== "auto") opts.layout_mode = adjLayout;
            if (adjZoom !== 1) opts.zoom_boost = adjZoom;
            if (adjShift !== 0) opts.x_offset = adjShift / 100;
            if (adjY !== 0) opts.y_offset = adjY / 100;
            if (adjCaption !== "keep") opts.caption_style = adjCaption;
            const updated = await reprocessClip(clip.id, opts);
            // Update clip in place without full reload
            setProject(prev => prev ? { ...prev, clips: prev.clips.map(c => c.id === clip.id ? { ...c, ...updated } : c) } : prev);
            setSelectedClip(updated);
            setAdjustId(null);
            setPreview(null);
        } catch {
            setApplyError("Adjust failed — backend busy or source video missing.");
        } finally {
            setApplying(false);
        }
    };

    const handleRetry = async () => {
        setRetrying(true);
        setRetryError(null);
        try {
            await retryProject(id);
            router.refresh();
            // Re-fetch project after a short delay to pick up new status
            setTimeout(async () => {
                try {
                    const data = await getProject(id);
                    setProject(data);
                    if (data.clips?.length > 0) setSelectedClip(data.clips[0]);
                } catch { /* ignore */ }
                setRetrying(false);
            }, 800);
        } catch {
            setRetryError("Retry failed — is the backend running?");
            setRetrying(false);
        }
    };

    useEffect(() => {
        getProject(id)
            .then(data => {
                setProject(data);
                if (data.clips?.length > 0) setSelectedClip(data.clips[0]);
            })
            .catch(err => setError(err instanceof Error ? err.message : "Not found"))
            .finally(() => setLoading(false));
    }, [id]);

    // Cleanup preview timer on unmount
    useEffect(() => {
        return () => {
            if (pvTimer.current) clearTimeout(pvTimer.current);
        };
    }, []);

    useEffect(() => {
        if (!project || ["done", "error"].includes(project.status)) return;
        const baseUrl = getApiBaseUrl();
        const wsBase = baseUrl.replace(/^https/, "wss").replace(/^http/, "ws");
        const ws = new WebSocket(`${wsBase}/ws/progress/${id}`);
        ws.onmessage = e => {
            try {
                const d = JSON.parse(e.data);
                setWsProgress({ stage: d.stage, percent: d.percent, message: d.message });
                setLogs(prev => [...prev.slice(-49), `[${new Date().toLocaleTimeString()}] ${d.message}`]);
                if (d.stage === "done") {
                    ws.close();
                    // Refetch instead of hard reload
                    getProject(id).then(data => {
                        setProject(data);
                        if (data.clips?.length > 0) setSelectedClip(data.clips[0]);
                    }).catch(() => router.refresh());
                }
                if (d.stage === "error") {
                    ws.close();
                    getProject(id).then(setProject).catch(() => {});
                }
            } catch { /* ignore */ }
        };
        ws.onerror = () => { /* will reconnect on next mount */ };
        return () => ws.close();
    }, [project, id, router]);

    const ytId = project ? getYouTubeId(project.youtube_url) : null;
    const isProcessing = project && !["done", "error"].includes(project.status);
    const pct = wsProgress?.percent ?? 0;
    const clampedPct = Math.min(100, Math.max(0, pct));

    if (loading) {
        return (
            <div className="flex h-[calc(100vh-65px)] items-center justify-center">
                <div className="flex flex-col items-center gap-3">
                    <div className="h-8 w-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
                    <p className="text-slate-400 text-sm">Loading project…</p>
                </div>
            </div>
        );
    }

    if (error || !project) {
        return (
            <div className="flex h-[calc(100vh-65px)] items-center justify-center flex-col gap-4">
                <p className="text-red-400 font-semibold">{error || "Project not found"}</p>
                <Link href="/" className="text-sm text-primary hover:underline">← Back to Dashboard</Link>
            </div>
        );
    }

    return (
        <div className="flex h-[calc(100vh-65px)] overflow-hidden">
            {/* Left: Video + Meta */}
            <section className="flex-[2] flex flex-col gap-6 overflow-y-auto custom-scrollbar p-8">
                {/* Header */}
                <div className="flex items-start justify-between gap-4">
                    <div>
                        <div className="flex items-center gap-2 mb-1.5">
                            <svg className="h-4 w-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>
                            <span className="text-xs font-medium text-slate-500 uppercase tracking-widest">
                                Project / {id.slice(0, 8).toUpperCase()}
                            </span>
                        </div>
                        <h1 className="text-2xl font-bold text-white">{project.title || "Untitled Project"}</h1>
                    </div>
                    <div className="flex gap-3 flex-shrink-0">
                        <Link href="/" className="px-4 py-2 rounded-xl glass glass-hover text-sm font-semibold flex items-center gap-2 text-slate-300">
                            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18"/></svg>
                            Dashboard
                        </Link>
                        {project.clips?.length > 0 && (
                            <button
                                onClick={handleExportAll}
                                disabled={batchJob?.status === "running" || batchJob?.status === "queued"}
                                className="px-5 py-2 rounded-xl bg-primary hover:bg-primary/90 text-white text-sm font-bold flex items-center gap-2 glow-primary transition-all disabled:opacity-50"
                            >
                                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z"/></svg>
                                {batchJob?.status === "running" || batchJob?.status === "queued" ? `Exporting… ${Math.round(batchJob.percent || 0)}%` : "Export All"}
                            </button>
                        )}
                        {project.clips?.length > 0 && (
                            <a
                                href={getProjectZipUrl(id)}
                                download
                                className="px-5 py-2 rounded-xl bg-white/10 hover:bg-white/20 text-white text-sm font-bold flex items-center gap-2 transition-all"
                            >
                                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"/></svg>
                                Download Zip
                            </a>
                        )}
                    </div>
                </div>

                {/* Video Player */}
                <div className="relative aspect-video rounded-3xl overflow-hidden glass border border-white/5 shadow-2xl bg-black">
                    {selectedClip ? (
                        <video
                            src={`${getApiBaseUrl()}${selectedClip.file_path}`}
                            controls
                            className="w-full h-full object-contain"
                            key={selectedClip.id}
                        />
                    ) : ytId ? (
                        <iframe
                            src={`https://www.youtube.com/embed/${ytId}`}
                            className="w-full h-full"
                            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                            allowFullScreen
                        />
                    ) : (
                        <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
                            {isProcessing ? (
                                <>
                                    <div className="h-10 w-10 rounded-full border-2 border-primary border-t-transparent animate-spin" />
                                    <p className="text-slate-400 text-sm">{STAGE_LABEL[wsProgress?.stage || project.status] || "Processing…"}</p>
                                </>
                            ) : (
                                <div className="flex flex-col items-center gap-2 text-slate-600">
                                    <svg className="h-12 w-12" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                                    <p className="text-sm">No video available</p>
                                </div>
                            )}
                        </div>
                    )}
                </div>

                {/* Live Progress (when processing) */}
                {isProcessing && (
                    <div className="glass-card rounded-2xl p-6 space-y-4">
                        <div className="flex justify-between items-center">
                            <div className="flex items-center gap-2">
                                <span className="relative flex h-2 w-2">
                                    <span className="animate-ping absolute h-full w-full rounded-full bg-primary opacity-75" />
                                    <span className="relative rounded-full h-2 w-2 bg-primary" />
                                </span>
                                <p className="text-slate-100 font-semibold text-sm">
                                    {STAGE_LABEL[wsProgress?.stage || project.status] || "Processing…"}
                                </p>
                            </div>
                            <p className="text-primary font-bold">{Math.round(clampedPct)}%</p>
                        </div>
                        <div className="h-2 w-full bg-white/5 rounded-full overflow-hidden">
                            <div
                                className="h-full rounded-full transition-all duration-500"
                                style={{ width: `${clampedPct}%`, background: "linear-gradient(to right, #2e1ded, #8b5cf6)" }}
                            />
                        </div>
                        <div className="bg-[#0a0a0a] rounded-xl p-4 font-mono text-xs space-y-1 max-h-32 overflow-y-auto custom-scrollbar border border-white/5">
                            {logs.length === 0 ? (
                                <span className="text-slate-600 italic">Connecting…</span>
                            ) : (
                                logs.map((log, i) => (
                                    <p key={i} className="text-slate-400">
                                        <span className="text-accent-purple/60">[LOG]</span> {log}
                                    </p>
                                ))
                            )}
                        </div>
                    </div>
                )}

                {/* Video Metadata */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                    {[
                        { label: "Status", val: project.status.charAt(0).toUpperCase() + project.status.slice(1) },
                        { label: "Clips", val: `${project.clips?.length || 0} generated` },
                        { label: "Source", val: ytId ? `YouTube: ${ytId}` : "Unknown" },
                        { label: "Created", val: new Date(project.created_at).toLocaleDateString() },
                    ].map(({ label, val }) => (
                        <div key={label} className="glass rounded-2xl p-4">
                            <p className="text-xs text-slate-500 mb-1">{label}</p>
                            <p className="text-sm font-bold text-white truncate">{val}</p>
                        </div>
                    ))}
                </div>

                <a href={project.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-2 text-sm text-primary hover:underline">
                    <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24"><path d="M23.498 6.186a3.016 3.016 0 00-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 00.502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 002.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 002.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>
                    View original YouTube video
                </a>
            </section>

            {/* Right: Clips Sidebar */}
            <aside className="w-80 flex-shrink-0 border-l border-white/5 flex flex-col">
                <div className="p-5 border-b border-white/5 glass-dark flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <svg className="h-5 w-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 3l14 9-14 9V3z"/></svg>
                        <h3 className="font-bold text-white">Generated Clips</h3>
                    </div>
                    {project.clips?.length > 0 && (
                        <span className="text-xs font-bold px-2.5 py-1 rounded-lg bg-primary/20 text-primary border border-primary/30">
                            {project.clips.length} TOTAL
                        </span>
                    )}
                </div>

                <div className="flex-1 overflow-y-auto custom-scrollbar p-5 space-y-4">
                    {project.clips?.length === 0 && project.status === "done" ? (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-600">
                            <svg className="h-10 w-10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.894L15 14M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8z"/></svg>
                            <p className="text-sm">No clips generated</p>
                        </div>
                    ) : project.clips?.length === 0 && project.status === "error" ? (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-center px-4">
                            <p className="text-sm font-bold text-red-400">Processing failed</p>
                            <p className="text-xs text-slate-500">The pipeline stopped with an error. Your video and settings are kept — just retry.</p>
                            <button
                                onClick={handleRetry}
                                disabled={retrying}
                                className="px-5 py-2 rounded-xl bg-primary hover:bg-primary/90 text-white text-sm font-bold transition-all disabled:opacity-50"
                            >
                                {retrying ? "Retrying…" : "↻ Retry pipeline"}
                            </button>
                            {retryError && <p className="text-xs text-red-400">{retryError}</p>}
                        </div>
                    ) : project.clips?.length === 0 ? (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-600">
                            <div className="h-6 w-6 rounded-full border-2 border-primary/40 border-t-primary animate-spin" />
                            <p className="text-sm">Processing clips…</p>
                        </div>
                    ) : (
                        project.clips.map((clip, i) => {
                            const isSelected = selectedClip?.id === clip.id;
                            const viralPct = clip.viral_score ? (clip.viral_score / 10) * 100 : 0;
                            return (
                                <div
                                    key={clip.id}
                                    onClick={() => setSelectedClip(clip)}
                                    className={`glass rounded-2xl p-4 flex gap-4 group cursor-pointer transition-all ${
                                        isSelected ? "border border-primary/50 bg-primary/5" : "border border-transparent hover:border-primary/30"
                                    }`}
                                >
                                    <div className="relative w-20 aspect-[9/16] rounded-xl overflow-hidden flex-shrink-0 bg-slate-900">
                                        <div className="absolute inset-0 flex items-center justify-center bg-black/40 group-hover:bg-black/20 transition-opacity">
                                            <svg className={`h-6 w-6 ${isSelected ? "text-primary" : "text-white/60"}`} fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                                        </div>
                                        <span className="absolute top-1.5 left-1.5 rounded px-1 py-0.5 text-[9px] font-bold bg-primary text-white">
                                            {formatDuration(clip.duration)}
                                        </span>
                                    </div>
                                    <div className="flex-1 flex flex-col justify-between min-w-0">
                                        <div>
                                            <div className="flex justify-between items-start gap-1">
                                                <h4 className="text-xs font-bold text-white line-clamp-2 leading-snug">
                                                    {clip.title || `Clip ${i + 1}`}
                                                </h4>
                                            </div>
                                            {clip.viral_score !== undefined && (
                                                <div className="mt-2 flex items-center gap-1.5">
                                                    <span className="text-[9px] font-bold uppercase tracking-wider text-slate-500">Viral:</span>
                                                    <div className="flex-1 h-1 bg-white/10 rounded-full">
                                                        <div className="h-full rounded-full bg-gradient-to-r from-primary to-accent-purple" style={{ width: `${viralPct}%` }} />
                                                    </div>
                                                    <span className="text-[9px] font-bold text-primary">{clip.viral_score}/10</span>
                                                </div>
                                            )}
                                        </div>
                                        <div className="flex gap-1.5 mt-3">
                                            <button
                                                onClick={e => { e.stopPropagation(); setSelectedClip(clip); }}
                                                className="flex-1 py-1.5 rounded-lg bg-primary/10 hover:bg-primary text-[9px] font-bold transition-colors text-primary hover:text-white"
                                            >
                                                PREVIEW
                                            </button>
                                            <button
                                                onClick={e => { e.stopPropagation(); handleDownload(clip); }}
                                                className="flex-1 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-[9px] font-bold transition-colors border border-white/10 text-center"
                                            >
                                                DOWNLOAD
                                            </button>
                                            <button
                                                onClick={e => { e.stopPropagation(); openAdjust(clip); }}
                                                className="flex-1 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-[9px] font-bold transition-colors border border-white/10"
                                            >
                                                {adjustId === clip.id ? "CLOSE" : "ADJUST"}
                                            </button>
                                            <Link
                                                href={`/editor/${clip.id}`}
                                                onClick={e => e.stopPropagation()}
                                                className="flex-1 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-[9px] font-bold text-white text-center"
                                            >
                                                EDIT
                                            </Link>
                                        </div>
                                        <div className="mt-1.5 flex items-center gap-1.5 text-[9px] font-bold text-slate-500">
                                            <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5">{clip.aspect || "9:16"}</span>
                                            {clip.layout_mode && <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5 capitalize">{clip.layout_mode}</span>}
                                        </div>
                                        {adjustId === clip.id && (
                                            <div className="mt-3 space-y-2.5 rounded-xl bg-black/30 border border-white/10 p-3" onClick={e => e.stopPropagation()}>
                                                <div
                                                    ref={imgBox}
                                                    className="relative w-full rounded-lg overflow-hidden bg-black select-none"
                                                    style={{ cursor: dragging ? "grabbing" : "grab", touchAction: "none" }}
                                                    onMouseDown={e => { setDragging(true); onPreviewDrag(e, clip.id); }}
                                                    onMouseMove={e => onPreviewDrag(e, clip.id)}
                                                    onMouseUp={() => setDragging(false)}
                                                    onMouseLeave={() => setDragging(false)}
                                                >
                                                    {preview ? (
                                                        <img src={`data:image/jpeg;base64,${preview.image}`} alt="crop preview"
                                                            className="w-full block pointer-events-none" draggable={false} />
                                                    ) : (
                                                        <div className="aspect-video flex items-center justify-center">
                                                            <div className="h-5 w-5 rounded-full border-2 border-primary/40 border-t-primary animate-spin" />
                                                        </div>
                                                    )}
                                                    {pvLoading && preview && (
                                                        <div className="absolute top-1.5 right-1.5 rounded bg-black/60 px-1.5 py-0.5 text-[9px] text-slate-300">updating…</div>
                                                    )}
                                                </div>
                                                <p className="text-[9px] text-slate-500 -mt-1">Drag the frame to move the camera • <span className="text-lime-300">○</span> faces • <span className="text-red-400">▭</span> crop</p>
                                                <div>
                                                    <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">Ratio</label>
                                                    <div className="grid grid-cols-4 gap-1">
                                                        {["9:16", "1:1", "16:9", "4:3"].map(a => (
                                                            <button key={a} type="button"
                                                                onClick={() => tweak(clip.id, { aspect: a })}
                                                                className={`py-1.5 rounded-lg text-[10px] font-bold transition-all border ${
                                                                    adjAspect === a ? "bg-primary border-primary text-white" : "bg-white/5 border-white/10 text-slate-400 hover:text-white"
                                                                }`}>
                                                                {a}
                                                            </button>
                                                        ))}
                                                    </div>
                                                </div>
                                                <div>
                                                    <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">Camera</label>
                                                    <div className="grid grid-cols-2 gap-1">
                                                        <button type="button" onClick={() => tweak(clip.id, { tracking: true })}
                                                            className={`py-1.5 rounded-lg text-[10px] font-bold transition-all border ${
                                                                adjTracking ? "bg-primary border-primary text-white" : "bg-white/5 border-white/10 text-slate-400 hover:text-white"
                                                            }`}>
                                                            ◉ Auto-track
                                                        </button>
                                                        <button type="button" onClick={() => tweak(clip.id, { tracking: false })}
                                                            className={`py-1.5 rounded-lg text-[10px] font-bold transition-all border ${
                                                                !adjTracking ? "bg-primary border-primary text-white" : "bg-white/5 border-white/10 text-slate-400 hover:text-white"
                                                            }`}>
                                                            ▣ Fixed
                                                        </button>
                                                    </div>
                                                </div>
                                                <div>
                                                    <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">Framing</label>
                                                    <select value={adjLayout} onChange={e => tweak(clip.id, { layout: e.target.value })}
                                                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none">
                                                        {[["auto", "Auto (detect)"], ["single", "Face track"], ["center", "Center"], ["tutorial", "Tutorial (wide)"], ["podcast", "Podcast (wide)"], ["panel", "Panel (wide)"], ["none", "No faces"]].map(([v, l]) => (
                                                            <option key={v} value={v}>{l}</option>
                                                        ))}
                                                    </select>
                                                </div>
                                                <div>
                                                    <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">Zoom: {adjZoom.toFixed(2)}x</label>
                                                    <input type="range" min={0.8} max={1.6} step={0.05} value={adjZoom}
                                                        onChange={e => tweak(clip.id, { zoom: parseFloat(e.target.value) })} className="w-full accent-violet-500" />
                                                </div>
                                                <div className="grid grid-cols-2 gap-2">
                                                    <div>
                                                        <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">← → {adjShift}%</label>
                                                        <input type="range" min={-100} max={100} step={5} value={adjShift}
                                                            onChange={e => tweak(clip.id, { x: parseInt(e.target.value) })} className="w-full accent-violet-500" />
                                                    </div>
                                                    <div>
                                                        <label className="block text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">↑ ↓ {adjY}%</label>
                                                        <input type="range" min={-100} max={100} step={5} value={adjY}
                                                            onChange={e => tweak(clip.id, { y: parseInt(e.target.value) })} className="w-full accent-violet-500" />
                                                    </div>
                                                </div>
                                                <div>
                                                    <select value={adjCaption} onChange={e => setAdjCaption(e.target.value)}
                                                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none">
                                                        <option value="keep">Captions: keep</option>
                                                        <option value="none">Captions: off</option>
                                                        {captionStyles.map(s => (
                                                            <option key={s.key} value={s.key}>{s.name}</option>
                                                        ))}
                                                    </select>
                                                </div>
                                                {applyError && <p className="text-[10px] text-red-400">{applyError}</p>}
                                                <button
                                                    onClick={() => handleApplyAdjust(clip)}
                                                    disabled={applying}
                                                    className="w-full py-2 rounded-lg bg-primary hover:bg-primary/90 text-white text-[10px] font-bold transition-all disabled:opacity-50"
                                                >
                                                    {applying ? "Re-rendering…" : `Apply ${adjAspect} ${adjTracking ? "• tracking" : "• fixed"} & re-render`}
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        })
                    )}
                </div>

                {project.status === "done" && (
                    <div className="p-5 border-t border-white/5">
                        <Link
                            href="/create"
                            className="flex items-center justify-center gap-2 w-full py-3.5 rounded-xl bg-primary hover:bg-primary/90 text-white text-sm font-bold glow-primary transition-all"
                        >
                            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                            Generate More Clips
                        </Link>
                    </div>
                )}
            </aside>
        </div>
    );
}
