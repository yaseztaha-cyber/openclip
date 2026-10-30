"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { getSettings, saveSettings, getCaptionStyles } from "@/lib/api";
import { Settings, CaptionStyle } from "@/lib/types";

const WHISPER_OPTIONS = [
    { value: "base", label: "Base (Fast)", desc: "Lowest latency" },
    { value: "small", label: "Small (Balanced)", desc: "Optimal performance" },
    { value: "medium", label: "Medium (Accurate)", desc: "Highest precision" },
] as const;

export default function SettingsPage() {
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [savedStatus, setSavedStatus] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const [provider, setProvider] = useState<Settings["llm_provider"]>("openai");
    const [model, setModel] = useState("gpt-4o");
    const [whisperModel, setWhisperModel] = useState<Settings["whisper_model"]>("base");
    const [captionStyle, setCaptionStyle] = useState("none");
    const [captionStylesList, setCaptionStylesList] = useState<CaptionStyle[]>([]);
    const [apiKey, setApiKey] = useState("");
    const [hasApiKey, setHasApiKey] = useState(false);
    const [editingKey, setEditingKey] = useState(false);

    useEffect(() => {
        Promise.all([getSettings(), getCaptionStyles()])
            .then(([data, styles]) => {
                setProvider(data.llm_provider);
                setModel(data.llm_model);
                setWhisperModel(data.whisper_model);
                setCaptionStyle(data.caption_style || "none");
                setHasApiKey(data.has_api_key);
                setCaptionStylesList(styles);
            })
            .catch(() => setError("Failed to load settings."))
            .finally(() => setLoading(false));
    }, []);

    const handleProviderChange = (p: Settings["llm_provider"]) => {
        setProvider(p);
        if (p === "openai") setModel("gpt-4o");
        if (p === "anthropic") setModel("claude-3-5-sonnet-20241022");
        if (p === "gemini") setModel("gemini-1.5-pro");
        if (p === "ollama") setModel("llama3");
        if (p === "openrouter") setModel("cohere/north-mini-code:free");
        if (p === "mistral") setModel("ministral-8b-latest");
        if (p === "groq") setModel("qwen/qwen3.8-27b");
        if (p === "anyapi") setModel("qwen/qwen3-coder:free");
        if (p === "cohere") setModel("command-a-03-2025");
        if (p === "llm7") setModel("default");
        if (p === "xkiro") setModel("qwen/qwen3-coder-plus:free");
        if (p === "auto") setModel("");
    };

    const PROVIDERS = [
        { key: "auto", label: "Auto ✦" },
        { key: "openai", label: "OpenAI" },
        { key: "anthropic", label: "Anthropic" },
        { key: "gemini", label: "Gemini" },
        { key: "ollama", label: "Ollama" },
        { key: "openrouter", label: "OpenRouter" },
        { key: "mistral", label: "Mistral" },
        { key: "groq", label: "Groq" },
        { key: "anyapi", label: "AnyAPI" },
        { key: "cohere", label: "Cohere" },
        { key: "llm7", label: "LLM7" },
        { key: "xkiro", label: "xKiro" },
    ] as const;

    const API_KEY_PLACEHOLDERS: Record<string, string> = {
        openai: "sk-••••••••••••••••",
        anthropic: "sk-ant-••••••••",
        gemini: "AIza••••••••",
        openrouter: "sk-or-••••••••••••••••",
        mistral: "•••••••••••••••• (console.mistral.ai)",
        groq: "gsk_•••••••• (console.groq.com)",
        anyapi: "sk-•••••••• (anyapi.ai)",
        cohere: "•••••••• (dashboard.cohere.com)",
        llm7: "token from dash.llm7.io",
        xkiro: "sk-xt-•••••••• (xkiro.com)",
    };

    const API_KEY_URLS: Record<string, string> = {
        openai: "https://platform.openai.com",
        anthropic: "https://console.anthropic.com",
        gemini: "https://aistudio.google.com",
        openrouter: "https://openrouter.ai/keys",
        mistral: "https://console.mistral.ai",
        groq: "https://console.groq.com",
        anyapi: "https://anyapi.ai",
        cohere: "https://dashboard.cohere.com",
        llm7: "https://dash.llm7.io",
        xkiro: "https://xkiro.com",
    };

    const handleSave = async (e: React.FormEvent) => {
        e.preventDefault();
        setSaving(true);
        setError(null);
        setSavedStatus(null);
        try {
            const payload: Partial<Settings> & { llm_api_key?: string } = {
                llm_provider: provider,
                llm_model: model,
                whisper_model: whisperModel,
                caption_style: captionStyle,
            };
            if (editingKey || (!hasApiKey && apiKey)) payload.llm_api_key = apiKey;
            await saveSettings(payload);
            setSavedStatus("Settings saved!");
            setHasApiKey(true);
            setEditingKey(false);
            setApiKey("");
            setTimeout(() => setSavedStatus(null), 3000);
        } catch {
            setError("Failed to save settings.");
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="flex h-[calc(100vh-65px)] overflow-hidden">
            {/* Main Content */}
            <main className="flex-1 flex flex-col overflow-y-auto custom-scrollbar">
                <header className="sticky top-0 z-10 flex items-center gap-4 px-10 py-5 border-b border-white/5 glass-dark">
                    <Link href="/" className="flex h-9 w-9 items-center justify-center rounded-xl glass glass-hover transition-all text-slate-400 hover:text-white">
                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18"/></svg>
                    </Link>
                    <h1 className="text-xl font-bold text-slate-100">Settings</h1>
                </header>

                {loading ? (
                    <div className="flex-1 flex items-center justify-center">
                        <div className="flex flex-col items-center gap-3">
                            <div className="h-8 w-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
                            <p className="text-slate-400 text-sm">Loading settings…</p>
                        </div>
                    </div>
                ) : (
                    <form onSubmit={handleSave} className="max-w-2xl mx-auto w-full p-10 space-y-12">

                        {/* LLM Provider */}
                        <section className="space-y-6">
                            <h2 className="text-lg font-bold text-slate-100">LLM Provider</h2>
                            <div>
                                <div className="flex flex-wrap p-1 bg-slate-900/50 rounded-xl gap-1 mb-6">
                                    {PROVIDERS.map(({ key, label }) => (
                                        <button
                                            key={key}
                                            type="button"
                                            onClick={() => handleProviderChange(key)}
                                            className={`flex-1 py-2 text-sm font-bold rounded-lg transition-all capitalize ${
                                                provider === key ? "bg-primary text-white" : "text-slate-400 hover:text-slate-100"
                                            }`}
                                        >
                                            {label}
                                        </button>
                                    ))}
                                </div>
                                <div className="space-y-4">
                                    {provider === "auto" && (
                                        <p className="text-xs text-emerald-400 bg-emerald-500/10 p-3 rounded-lg border border-emerald-500/20">
                                            ✦ Auto tries your saved provider keys in order (Groq → LLM7 → Cohere →
                                            Mistral → xKiro → OpenRouter…) and fails over to the next one if a model
                                            stalls or errors. Add keys per provider by selecting it above.
                                        </p>
                                    )}
                                    {provider !== "ollama" && provider !== "auto" && (
                                        <div>
                                            <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">API Key</label>
                                            {hasApiKey && !editingKey ? (
                                                <div className="flex items-center justify-between p-3.5 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-400">
                                                    <div className="flex items-center gap-2 text-sm">
                                                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7"/></svg>
                                                        API key saved
                                                    </div>
                                                    <button type="button" onClick={() => setEditingKey(true)} className="text-xs font-bold hover:underline">Change</button>
                                                </div>
                                            ) : (
                                                <div className="space-y-2">
                                                    <input
                                                        type="password"
                                                        value={apiKey}
                                                        onChange={e => setApiKey(e.target.value)}
                                                        placeholder={API_KEY_PLACEHOLDERS[provider] || "paste key…"}
                                                        className="w-full bg-slate-900/50 border border-slate-800 rounded-xl px-4 py-3 text-slate-100 focus:border-primary focus:outline-none transition-all text-sm"
                                                    />
                                                    <div className="flex items-center justify-between">
                                                        <a href={API_KEY_URLS[provider] || "https://openrouter.ai/keys"} target="_blank" rel="noreferrer" className="text-xs font-bold text-primary hover:underline">
                                                            Get API key →
                                                        </a>
                                                        {editingKey && (
                                                            <button type="button" onClick={() => { setEditingKey(false); setApiKey(""); }} className="text-xs text-slate-500 hover:text-white transition-colors">Cancel</button>
                                                        )}
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                    <div>
                                        <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">Model Name</label>
                                        <input
                                            value={model}
                                            onChange={e => setModel(e.target.value)}
                                            disabled={provider === "auto"}
                                            className="w-full bg-slate-900/50 border border-slate-800 rounded-xl px-4 py-3 text-slate-100 focus:border-primary focus:outline-none transition-all text-sm disabled:opacity-50"
                                            placeholder={provider === "auto" ? "Best available — chosen automatically" : "gpt-4o-mini"}
                                        />
                                        {provider === "ollama" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Make sure Ollama is running on localhost:11434
                                            </p>
                                        )}
                                        {provider === "openrouter" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Verified free picks: cohere/north-mini-code:free (fast default)
                                                or nvidia/nemotron-3-ultra-550b-a55b:free (1M context, slower).
                                                Full list at openrouter.ai/models — free models are rate-limited.
                                            </p>
                                        )}
                                        {provider === "mistral" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Verified on free-tier keys: ministral-8b-latest (default) or
                                                ministral-3b-latest. Larger models need a paid quota.
                                            </p>
                                        )}
                                        {provider === "groq" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Free & fast with auto-fallback: qwen3.8-27b → gpt-oss-120b →
                                                gpt-oss-20b. If one fails, the next is tried automatically.
                                            </p>
                                        )}
                                        {provider === "anyapi" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                100k free tokens/day. If models fail with access errors,
                                                check your plan at anyapi.ai.
                                            </p>
                                        )}
                                        {provider === "cohere" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Verified: command-a-03-2025 (default) with auto-fallback
                                                to command-r-plus-08-2024.
                                            </p>
                                        )}
                                        {provider === "llm7" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Verified free tier: default (auto-routed) → fast →
                                                DeepSeek-V4-Flash-0731. Token from dash.llm7.io.
                                            </p>
                                        )}
                                        {provider === "xkiro" && (
                                            <p className="mt-2 text-xs text-blue-400 bg-blue-500/10 p-2 rounded-lg border border-blue-500/20">
                                                Verified free: qwen3-coder-plus (default, ~5s) →
                                                qwen3.7-max → minimax-m2.5. Full catalog at
                                                api.xkiro.com/v1/models.
                                            </p>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </section>

                        {/* Whisper Model */}
                        <section className="space-y-4">
                            <h2 className="text-lg font-bold text-slate-100">Whisper Model</h2>
                            <div className="grid grid-cols-3 gap-4">
                                {WHISPER_OPTIONS.map(opt => (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => setWhisperModel(opt.value)}
                                        className={`p-4 rounded-xl text-left border transition-all ${
                                            whisperModel === opt.value
                                                ? "border-[var(--success)] bg-[var(--success)]/5"
                                                : "border-slate-800 bg-slate-900/30 hover:border-slate-700"
                                        }`}
                                    >
                                        <p className="font-bold text-slate-100 text-sm">{opt.label}</p>
                                        <p className="text-xs text-slate-500 mt-1">{opt.desc}</p>
                                    </button>
                                ))}
                            </div>
                        </section>

                        {/* Caption Style */}
                        <section className="space-y-4">
                            <h2 className="text-lg font-bold text-slate-100">Caption Style</h2>
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                                {captionStylesList.length > 0 ? captionStylesList.map(style => (
                                    <button
                                        key={style.key}
                                        type="button"
                                        onClick={() => setCaptionStyle(style.key)}
                                        className={`space-y-2 group transition-all`}
                                    >
                                        <div className={`aspect-video rounded-xl flex items-center justify-center border-2 transition-all ${
                                            captionStyle === style.key ? "border-[var(--success)] bg-[var(--success)]/5" : "border-slate-800 bg-slate-900/50"
                                        }`}
                                            style={{ backgroundColor: style.preview_colors.background || undefined }}>
                                            {style.key === "none" ? (
                                                <span className="text-slate-600 font-bold line-through text-sm">THE QUICK</span>
                                            ) : style.animation === "one_word" ? (
                                                <span className="font-black uppercase" style={{ color: style.preview_colors.text || "#fff" }}>QUICK</span>
                                            ) : style.animation === "highlight" ? (
                                                <span className="font-bold uppercase text-sm" style={{ color: style.preview_colors.highlight || "#fff" }}>THE QUICK</span>
                                            ) : (
                                                <span className="font-bold uppercase text-sm" style={{ color: style.preview_colors.text || "#fff" }}>THE QUICK</span>
                                            )}
                                        </div>
                                        <p className={`text-center text-xs font-bold ${captionStyle === style.key ? "text-[var(--success)]" : "text-slate-500"}`}>
                                            {style.name}
                                        </p>
                                    </button>
                                )) : (
                                    [
                                        { key: "none", label: "No Captions" },
                                        { key: "dynamic", label: "Dynamic" },
                                        { key: "outline", label: "Outline" },
                                        { key: "classic", label: "Classic" },
                                    ].map(s => (
                                        <button key={s.key} type="button" onClick={() => setCaptionStyle(s.key)}
                                            className={`space-y-2`}>
                                            <div className={`aspect-video rounded-xl flex items-center justify-center border-2 bg-slate-900/50 ${captionStyle === s.key ? "border-[var(--success)]" : "border-slate-800"}`}>
                                                <span className={`font-bold text-sm ${captionStyle === s.key ? "text-[var(--success)]" : "text-slate-600"}`}>THE QUICK</span>
                                            </div>
                                            <p className={`text-center text-xs font-bold ${captionStyle === s.key ? "text-[var(--success)]" : "text-slate-500"}`}>{s.label}</p>
                                        </button>
                                    ))
                                )}
                            </div>
                        </section>

                        {error && (
                            <div className="flex items-center gap-2 rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-sm text-red-400">
                                <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                                {error}
                            </div>
                        )}

                        <div className="pb-4">
                            <button
                                type="submit"
                                disabled={saving}
                                className="w-full bg-primary hover:bg-primary/90 text-white py-4 rounded-xl font-bold transition-all glow-primary text-base disabled:opacity-50"
                            >
                                {saving ? "Saving…" : savedStatus ? "✓ " + savedStatus : "Save Settings"}
                            </button>
                        </div>
                    </form>
                )}
            </main>
        </div>
    );
}
