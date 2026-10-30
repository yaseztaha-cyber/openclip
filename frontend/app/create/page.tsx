"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { createProject, getProject, getSettings, saveSettings, getApiBaseUrl, getCaptionStyles, CreateProjectOptions } from "@/lib/api";
import { Clip, Settings, CaptionStyle } from "@/lib/types";
import { useProjectProgress } from "@/lib/websocket";

const STAGE_LABELS: Record<string, string> = {
    downloading: "Downloading video…",
    transcribing: "Extracting captions…",
    analyzing: "AI analyzing video…",
    processing: "Cutting & reframing clips…",
    done: "All clips ready!",
    error: "Pipeline failed",
};

function formatDuration(s: number) {
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}:${sec.toString().padStart(2, "0")}`;
}

export default function CreateProjectPage() {
    const router = useRouter();
    const [urlsText, setUrlsText] = useState("");
    const [queueIndex, setQueueIndex] = useState(0);
    const [queueTotal, setQueueTotal] = useState(0);
    const [isProcessing, setIsProcessing] = useState(false);
    const [generatedClips, setGeneratedClips] = useState<Clip[]>([]);
    const [projectId, setProjectId] = useState<string | null>(null);
    const [apiError, setApiError] = useState<string | null>(null);
    const [hasApiKey, setHasApiKey] = useState<boolean | null>(null);
    const [provider, setProvider] = useState<Settings["llm_provider"]>("openai");
    const [model, setModel] = useState("gpt-4o");
    const [aspect, setAspect] = useState("9:16");
    const [outputMode, setOutputMode] = useState("auto");
    const [layoutMode, setLayoutMode] = useState("auto");
    const [zoomBoost, setZoomBoost] = useState(1.0);
    const [fixedCamera, setFixedCamera] = useState(false);
    const [captionStyle, setCaptionStyle] = useState("");
    const [captionStyles, setCaptionStyles] = useState<CaptionStyle[]>([]);
    const logsEndRef = useRef<HTMLDivElement>(null);

    const { stage, percent, message, logs } = useProjectProgress(projectId);

    useEffect(() => {
        getSettings()
            .then(s => {
                setHasApiKey(s.has_api_key);
                setProvider(s.llm_provider);
                setModel(s.llm_model);
            })
            .catch(console.error);
        getCaptionStyles().then(setCaptionStyles).catch(() => {});
    }, []);

    useEffect(() => {
        logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [logs]);

    useEffect(() => {
        if (stage === "done" && projectId) {
            getProject(projectId)
                .then(data => { setGeneratedClips(data.clips || []); setIsProcessing(false); })
                .catch(() => { setApiError("Failed to load clips."); setIsProcessing(false); });
        } else if (stage === "error") {
            setIsProcessing(false);
        }
    }, [stage, projectId]);

    const handlePaste = async () => {
        try { setUrlsText(await navigator.clipboard.readText()); } catch { /* ignore */ }
    };

    const parseUrls = (): string[] => {
        const seen = new Set<string>();
        const out: string[] = [];
        for (const raw of urlsText.split(/\r?\n/)) {
            const u = raw.trim();
            if (u && !seen.has(u)) { seen.add(u); out.push(u); }
        }
        return out;
    };

    const handleGenerate = async (e: React.FormEvent) => {
        e.preventDefault();
        const list = parseUrls();
        if (list.length === 0 || isProcessing) return;
        setIsProcessing(true);
        setGeneratedClips([]);
        setApiError(null);
        setProjectId(null);
        setQueueTotal(0);
        setQueueIndex(0);
        try {
            await saveSettings({ llm_provider: provider, llm_model: model });
            const options: CreateProjectOptions = {
                aspect,
                layout_mode: layoutMode,
                zoom_boost: zoomBoost,
                tracking: !fixedCamera,
                output_mode: outputMode,
            };
            if (captionStyle) options.caption_style = captionStyle;
            if (list.length === 1) {
                const { project_id } = await createProject(list[0], options);
                setProjectId(project_id);
            } else {
                setQueueTotal(list.length);
                for (let i = 0; i < list.length; i++) {
                    setQueueIndex(i + 1);
                    await createProject(list[i], options);
                }
                router.push("/");
            }
        } catch {
            setApiError("Something went wrong. Please try again.");
            setIsProcessing(false);
        }
    };

    const clampedPercent = Math.min(100, Math.max(0, percent));
    const isLive = isProcessing && projectId;

    return (
        <div className="mx-auto w-full max-w-3xl px-6 py-12">

            {/* No API key warning */}
            {hasApiKey === false && (
                <div className="mb-8 flex items-center justify-between gap-4 rounded-2xl border border-amber-500/20 bg-amber-500/10 p-5">
                    <div className="flex items-center gap-3 text-amber-400">
                        <svg className="h-5 w-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                        <div>
                            <p className="font-bold text-sm">No LLM configured</p>
                            <p className="text-xs opacity-80">AI clip detection requires an API key.</p>
                        </div>
                    </div>
                    <Link href="/settings" className="flex-shrink-0 rounded-xl bg-amber-500/20 px-4 py-2 text-xs font-bold text-amber-400 hover:bg-amber-500/30 transition-colors">
                        Configure →
                    </Link>
                </div>
            )}

            {/* Header */}
            {!isLive && generatedClips.length === 0 && (
                <div className="mb-10 text-center">
                    <span className="mb-4 inline-block rounded-full bg-primary/20 border border-primary/30 px-4 py-1 text-xs font-bold uppercase tracking-widest text-primary">
                        New Project
                    </span>
                    <h1 className="text-4xl font-bold text-white mb-3">
                        Create from <span className="text-gradient">YouTube</span>
                    </h1>
                    <p className="text-slate-400 text-base max-w-lg mx-auto">
                        Paste any YouTube URL. The AI pipeline will download, transcribe, analyze and cut your clips automatically.
                    </p>
                </div>
            )}

            {/* URL Input Card */}
            {!isLive && generatedClips.length === 0 && (
                <form onSubmit={handleGenerate} className="glass-card rounded-3xl p-8 space-y-6">
                    {/* URL Field */}
                    <div>
                        <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">YouTube URLs</label>
                        <div className="rounded-xl glass px-4 py-3 border border-white/10 focus-within:border-primary/50 transition-all">
                            <textarea
                                value={urlsText}
                                onChange={e => setUrlsText(e.target.value)}
                                rows={3}
                                className="w-full bg-transparent text-sm text-white placeholder-slate-500 focus:outline-none resize-none"
                                placeholder={"https://youtu.be/...\nhttps://www.youtube.com/watch?v=...\none per line"}
                                required
                            />
                        </div>
                        <p className="text-[10px] text-slate-600 mt-1.5">{parseUrls().length > 1 ? `${parseUrls().length} videos queued — they'll process one at a time` : "Paste one URL per line to batch-create projects"}</p>
                        <button
                            type="button"
                            onClick={handlePaste}
                            className="mt-2 rounded-lg bg-primary/20 px-3 py-1.5 text-xs font-bold text-primary hover:bg-primary/30 transition-colors uppercase tracking-wider"
                        >
                            Paste
                        </button>
                    </div>

                    {/* Provider + Model */}
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">AI Provider</label>
                            <div className="flex flex-wrap rounded-xl bg-slate-900/50 p-1 gap-1">
                                {(["auto", "openai", "anthropic", "gemini", "ollama", "openrouter", "mistral", "groq", "anyapi", "cohere", "llm7", "xkiro"] as const).map(p => (
                                    <button
                                        key={p}
                                        type="button"
                                        onClick={() => setProvider(p)}
                                        className={`flex-1 py-1.5 text-[11px] font-bold rounded-lg transition-all capitalize whitespace-nowrap px-2 ${
                                            provider === p ? "bg-primary text-white" : "text-slate-400 hover:text-slate-200"
                                        }`}
                                    >
                                        {({ auto: "Auto ✦", openai: "OpenAI", anthropic: "Claude", gemini: "Gemini", ollama: "Ollama", openrouter: "OpenRouter", mistral: "Mistral", groq: "Groq", anyapi: "AnyAPI", cohere: "Cohere", llm7: "LLM7", xkiro: "xKiro" } as Record<string, string>)[p]}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <div>
                            <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">Model</label>
                            <input
                                value={model}
                                onChange={e => setModel(e.target.value)}
                                className="w-full rounded-xl bg-slate-900/50 border border-slate-800 px-3 py-2.5 text-sm text-slate-100 focus:border-primary focus:outline-none transition-all"
                                placeholder="gpt-4o"
                            />
                        </div>
                    </div>

                    {/* Adjustments — applied to every auto-cut clip */}
                    <div>
                        <div className="mb-2 flex items-end justify-between">
                            <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider">Framing Adjustments</label>
                            <span className="text-[10px] text-slate-600 font-semibold uppercase tracking-wider">applied to all auto clips</span>
                        </div>
                        <div className="rounded-2xl border border-white/10 bg-slate-900/30 p-4 space-y-4">
                            {/* Output format — auto detects the best framing per clip */}
                            <div>
                                <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Output Format <span className="text-primary">(auto recommended)</span></label>
                                <div className="flex flex-wrap gap-1">
                                    {([
                                        ["auto", "Auto ✦"],
                                        ["crop_9x16", "Crop 9:16"],
                                        ["blur_9x16", "Blur 9:16"],
                                        ["native_16x9", "16:9"],
                                        ["crop_1x1", "Square 1:1"],
                                    ] as const).map(([val, label]) => (
                                        <button
                                            key={val}
                                            type="button"
                                            onClick={() => setOutputMode(val)}
                                            className={`flex-1 py-1.5 text-[11px] font-bold rounded-lg transition-all px-2 whitespace-nowrap ${
                                                outputMode === val ? "bg-primary text-white" : "text-slate-400 hover:text-slate-200 bg-slate-800/40"
                                            }`}
                                        >
                                            {label}
                                        </button>
                                    ))}
                                </div>
                                <p className="text-[10px] text-slate-600 mt-1.5">Auto picks a sharp 9:16 face crop for talking heads and a blurred backdrop for screen/tutorial or wide shots.</p>
                            </div>

                            {/* Aspect */}
                            <div>
                                <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Aspect Ratio {outputMode === "auto" && <span className="text-slate-600">(auto overrides)</span>}</label>
                                <div className="flex flex-wrap gap-1">
                                    {(["9:16", "1:1", "16:9", "4:3"] as const).map(a => (
                                        <button
                                            key={a}
                                            type="button"
                                            onClick={() => setAspect(a)}
                                            className={`flex-1 py-1.5 text-[11px] font-bold rounded-lg transition-all px-2 ${
                                                aspect === a ? "bg-primary text-white" : "text-slate-400 hover:text-slate-200 bg-slate-800/40"
                                            }`}
                                        >
                                            {a}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Framing / layout */}
                            <div>
                                <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Framing</label>
                                <div className="flex flex-wrap gap-1">
                                    {(["auto", "single", "tutorial", "podcast", "panel", "center"] as const).map(l => (
                                        <button
                                            key={l}
                                            type="button"
                                            onClick={() => setLayoutMode(l)}
                                            className={`flex-1 py-1.5 text-[11px] font-bold rounded-lg transition-all px-2 ${
                                                layoutMode === l ? "bg-primary text-white" : "text-slate-400 hover:text-slate-200 bg-slate-800/40"
                                            }`}
                                        >
                                            {l}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Zoom */}
                            <div>
                                <div className="flex items-center justify-between mb-1.5">
                                    <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider">Zoom</label>
                                    <span className="text-[11px] font-bold text-primary">{(zoomBoost * 100).toFixed(0)}%</span>
                                </div>
                                <input
                                    type="range"
                                    min={0.8}
                                    max={1.6}
                                    step={0.05}
                                    value={zoomBoost}
                                    onChange={e => setZoomBoost(parseFloat(e.target.value))}
                                    className="w-full accent-primary"
                                />
                                <div className="flex justify-between text-[10px] text-slate-600 mt-0.5">
                                    <span>Wide 80%</span>
                                    <span>Standard 100%</span>
                                    <span>Tight 160%</span>
                                </div>
                            </div>

                            {/* Fixed camera */}
                            <div className="flex items-center justify-between">
                                <div>
                                    <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider">Fixed Camera</label>
                                    <p className="text-[10px] text-slate-600 mt-0.5">Lock the frame instead of auto-tracking the subject</p>
                                </div>
                                <button
                                    type="button"
                                    role="switch"
                                    aria-checked={fixedCamera}
                                    onClick={() => setFixedCamera(v => !v)}
                                    className={`relative h-6 w-11 rounded-full transition-colors ${fixedCamera ? "bg-primary" : "bg-slate-700"}`}
                                >
                                    <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${fixedCamera ? "left-[22px]" : "left-0.5"}`} />
                                </button>
                            </div>

                            {/* Captions */}
                            <div>
                                <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Caption Style</label>
                                <select
                                    value={captionStyle}
                                    onChange={e => setCaptionStyle(e.target.value)}
                                    className="w-full rounded-xl bg-slate-900/60 border border-slate-800 px-3 py-2.5 text-sm text-slate-100 focus:border-primary focus:outline-none transition-all"
                                >
                                    <option value="">Account default</option>
                                    {captionStyles.map(s => (
                                        <option key={s.key} value={s.key}>{s.name}</option>
                                    ))}
                                </select>
                            </div>
                        </div>
                    </div>

                    {apiError && (
                        <div className="flex items-center gap-2 rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-sm text-red-400">
                            <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12"/></svg>
                            {apiError}
                        </div>
                    )}

                    <button
                        type="submit"
                        className="w-full rounded-xl bg-primary py-4 text-sm font-bold text-white glow-primary hover:bg-primary/90 transition-all"
                    >
                        Generate Clips
                    </button>
                </form>
            )}

            {/* Live Processing View */}
            {isLive && (
                <div className="space-y-6">
                    <div className="text-center mb-8">
                        <span className="mb-4 inline-flex items-center gap-2 rounded-full bg-primary/20 border border-primary/30 px-4 py-1 text-xs font-bold uppercase tracking-widest text-primary">
                            <span className="relative flex h-2 w-2">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-primary opacity-75" />
                                <span className="relative inline-flex rounded-full h-2 w-2 bg-primary" />
                            </span>
                            Live Processing
                        </span>
                        <h1 className="text-4xl font-bold text-white mt-3">
                            Processing <span className="text-gradient">Video</span>
                        </h1>
                        {queueTotal > 0 && (
                            <p className="text-sm font-bold text-emerald-400 mt-2">Queue {queueIndex}/{queueTotal} — submitting projects…</p>
                        )}
                        <p className="text-slate-400 mt-2">Our AI pipeline is extracting and cutting your clips.</p>
                    </div>

                    {/* Progress Card */}
                    <div className="glass-card rounded-2xl p-8 space-y-6">
                        <div className="flex items-end justify-between">
                            <div>
                                <p className="text-slate-100 text-lg font-semibold">Overall Progress</p>
                                <p className="text-slate-400 text-sm mt-1">{STAGE_LABELS[stage] || message || "Processing…"}</p>
                            </div>
                            <p className="text-primary text-3xl font-bold">{Math.round(clampedPercent)}%</p>
                        </div>
                        <div className="h-3 w-full bg-white/5 rounded-full overflow-hidden border border-white/5">
                            <div
                                className="h-full rounded-full transition-all duration-500"
                                style={{
                                    width: `${clampedPercent}%`,
                                    background: stage === "error"
                                        ? "#f87171"
                                        : "linear-gradient(to right, #2e1ded, #8b5cf6)",
                                    boxShadow: stage !== "error" ? "0 0 12px rgba(46,29,237,0.5)" : undefined,
                                }}
                            />
                        </div>
                        <div className="grid grid-cols-4 gap-4 pt-2 border-t border-white/5">
                            {[
                                { label: "Stage", val: stage || "—" },
                                { label: "Progress", val: `${Math.round(clampedPercent)}%` },
                                { label: "Engine", val: provider.toUpperCase() },
                                { label: "Model", val: model },
                            ].map(({ label, val }) => (
                                <div key={label}>
                                    <p className="text-slate-500 text-xs uppercase font-bold tracking-tighter">{label}</p>
                                    <p className="text-slate-100 font-semibold text-sm truncate">{val}</p>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Terminal Log */}
                    <div>
                        <h3 className="text-white text-base font-bold mb-3 px-1">Pipeline Output</h3>
                        <div className="bg-[#0a0a0a] border border-accent-purple/30 rounded-xl p-5 font-mono text-sm leading-relaxed overflow-hidden shadow-2xl">
                            <div className="flex items-center gap-2 mb-4 pb-3 border-b border-white/5">
                                <div className="flex gap-1.5">
                                    <div className="w-3 h-3 rounded-full bg-red-500/50" />
                                    <div className="w-3 h-3 rounded-full bg-amber-500/50" />
                                    <div className="w-3 h-3 rounded-full bg-emerald-500/50" />
                                </div>
                                <span className="text-slate-500 text-[10px] uppercase tracking-widest ml-2">pipeline_logs_v4.0</span>
                            </div>
                            <div className="space-y-1.5 h-48 overflow-y-auto custom-scrollbar">
                                {logs.length === 0 ? (
                                    <p className="text-slate-500 italic">Connecting to pipeline…</p>
                                ) : (
                                    logs.map((log, i) => {
                                        const isErr = log.includes("error") || log.includes("Error");
                                        const isWarn = log.includes("warn") || log.includes("Warning");
                                        const isOk = log.includes("done") || log.includes("complete") || log.includes("success");
                                        return (
                                            <p key={i} className={
                                                isErr ? "text-red-400" :
                                                isWarn ? "text-amber-400" :
                                                isOk ? "text-emerald-400" : "text-slate-400"
                                            }>
                                                <span className="text-accent-purple/70">[{new Date().toLocaleTimeString()}]</span>{" "}
                                                <span className="text-white font-medium">{log.split("]").pop()?.trim() || log}</span>
                                            </p>
                                        );
                                    })
                                )}
                                <div ref={logsEndRef} />
                                {isProcessing && (
                                    <div className="flex items-center gap-1">
                                        <span className="text-accent-purple/70">[{new Date().toLocaleTimeString()}]</span>
                                        <span className="text-white animate-pulse">_</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>

                    {/* Info bar */}
                    <div className="flex items-center justify-between p-5 glass-card rounded-xl border border-primary/20">
                        <div className="flex items-center gap-3">
                            <svg className="h-5 w-5 text-accent-purple flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                            <p className="text-slate-300 text-sm">You can safely leave this page — we&apos;ll keep processing in the background.</p>
                        </div>
                        {projectId && (
                            <Link href={`/project/${projectId}`} className="text-slate-400 hover:text-white text-sm font-semibold transition-colors flex-shrink-0 ml-4">
                                View Project →
                            </Link>
                        )}
                    </div>
                </div>
            )}

            {/* Done — show clips */}
            {generatedClips.length > 0 && stage === "done" && (
                <div className="space-y-8">
                    <div className="text-center">
                        <h2 className="text-3xl font-bold text-white">
                            <span className="text-gradient">{generatedClips.length} Clips</span> Generated
                        </h2>
                        <p className="text-slate-400 mt-2">Your clips are ready to preview and download.</p>
                    </div>

                    <div className="grid grid-cols-1 gap-4">
                        {generatedClips.map(clip => (
                            <div key={clip.id} className="glass rounded-2xl p-5 flex gap-5 group hover:border-primary/30 transition-all">
                                <div className="relative w-24 aspect-[9/16] rounded-xl overflow-hidden flex-shrink-0 bg-slate-900">
                                    <div className="absolute inset-0 flex items-center justify-center">
                                        <svg className="h-8 w-8 text-primary/60" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                                    </div>
                                    <span className="absolute top-2 left-2 rounded px-1.5 py-0.5 text-[10px] font-bold bg-primary text-white">
                                        {formatDuration(clip.duration)}
                                    </span>
                                </div>
                                <div className="flex-1 flex flex-col justify-between">
                                    <div>
                                        <h4 className="text-sm font-bold text-white line-clamp-2">{clip.title || `Clip ${formatDuration(clip.start_time)} – ${formatDuration(clip.end_time)}`}</h4>
                                        {clip.viral_score !== undefined && (
                                            <div className="mt-2 flex items-center gap-2">
                                                <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Viral Score:</span>
                                                <div className="flex-1 h-1 bg-white/10 rounded-full max-w-[120px]">
                                                    <div className="h-full rounded-full bg-gradient-to-r from-primary to-accent-purple" style={{ width: `${(clip.viral_score / 10) * 100}%` }} />
                                                </div>
                                                <span className="text-[10px] font-bold text-primary">{clip.viral_score}/10</span>
                                            </div>
                                        )}
                                        {clip.reason && <p className="text-xs text-slate-500 mt-1 line-clamp-2">{clip.reason}</p>}
                                    </div>
                                    <div className="flex gap-2 mt-3">
                                        <a href={`${getApiBaseUrl()}${clip.file_path}`} target="_blank" rel="noreferrer"
                                            className="flex-1 py-2 rounded-lg bg-primary/10 hover:bg-primary text-[10px] font-bold text-center transition-colors text-primary hover:text-white">
                                            PREVIEW
                                        </a>
                                        <a href={`${getApiBaseUrl()}${clip.file_path}`} download
                                            className="flex-1 py-2 rounded-lg bg-white/5 hover:bg-white/10 text-[10px] font-bold text-center transition-colors border border-white/10">
                                            DOWNLOAD
                                        </a>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>

                    <button
                        onClick={() => router.push(`/project/${projectId}`)}
                        className="w-full rounded-xl glass glass-hover py-4 text-sm font-bold text-white transition-all"
                    >
                        View Full Project →
                    </button>
                </div>
            )}
        </div>
    );
}
