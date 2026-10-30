import { Project, Clip, Settings, CaptionStyle, Candidate, Review } from './types';

function getBaseUrl(): string {
    if (typeof window !== 'undefined') {
        const host = window.location.hostname;
        if (host !== 'localhost' && host !== '127.0.0.1') {
            return `https://${host.replace(/^(\d+)-/, '8000-')}`;
        }
    }
    return process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
}

export function getApiBaseUrl(): string {
    return getBaseUrl();
}

export const getProjects = async (): Promise<Project[]> => {
    try {
        const res = await fetch(`${getBaseUrl()}/api/projects`, { cache: 'no-store' });
        if (!res.ok) return [];
        return res.json();
    } catch {
        // Backend not running or network error - return empty array
        return [];
    }
};

export const getProject = async (id: string): Promise<Project & { clips: Clip[] }> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${id}`, { cache: 'no-store' });
    if (!res.ok) throw new Error("Project not found");
    return res.json();
};

export interface CreateProjectOptions {
    layout_mode?: string;
    aspect?: string;
    zoom_boost?: number;
    x_offset?: number;
    y_offset?: number;
    tracking?: boolean;
    caption_style?: string;
    output_mode?: string;
}

export const createProject = async (youtube_url: string, options: CreateProjectOptions = {}): Promise<{ project_id: string }> => {
    const res = await fetch(`${getBaseUrl()}/api/projects`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ youtube_url, ...options })
    });
    if (!res.ok) throw new Error("Failed to create project");
    return res.json();
};

export const retryProject = async (id: string): Promise<{ project_id: string }> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${id}/retry`, { method: 'POST' });
    if (!res.ok) throw new Error("Failed to retry project");
    return res.json();
};

export const makeClipFromCandidate = async (projectId: string, candidateId: string): Promise<{ job_id: string; clip_id: string; status: string }> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${projectId}/candidates/${candidateId}/make-clip`, { method: 'POST' });
    if (!res.ok) throw new Error("Failed to cut candidate");
    return res.json();
};

export const getAbExperiments = async (projectId: string): Promise<any[]> => {
    try {
        const res = await fetch(`${getBaseUrl()}/api/projects/${projectId}/ab-experiments`, { cache: 'no-store' });
        if (res.ok) return res.json();
    } catch { /* ignore */ }
    return [];
};

export const getProjectZipUrl = (projectId: string): string => `${getBaseUrl()}/api/projects/${projectId}/export-zip`;

export const getStorageUsage = async (): Promise<{ total_bytes: number; tmp_dir: string; project_count: number; projects: Project[] } | null> => {
    try {
        const res = await fetch(`${getBaseUrl()}/api/storage`, { cache: 'no-store' });
        if (res.ok) return res.json();
    } catch { /* ignore */ }
    return null;
};

export interface ReprocessOptions {
    layout_mode?: string;
    aspect?: string;
    zoom_boost?: number;
    x_offset?: number;
    y_offset?: number;
    tracking?: boolean;
    static?: boolean;
    caption_style?: string;
}

export const reprocessClip = async (id: string, opts: ReprocessOptions): Promise<Clip> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${id}/reprocess`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(opts),
    });
    if (!res.ok) throw new Error("Failed to reprocess clip");
    return res.json();
};

export interface CropPreview {
    image: string;
    width: number;
    height: number;
    aspect: string;
    layout: string;
    crop: { x: number; y: number; w: number; h: number };
    faces: { x: number; y: number }[];
    base: { cx: number; cy: number };
    slack: { x: number; y: number };
    frame: { w: number; h: number };
    at: number;
}

export const getCropPreview = async (
    id: string,
    params: { aspect: string; zoom_boost: number; x_offset: number; y_offset: number; layout_mode: string; tracking: boolean },
): Promise<CropPreview> => {
    const q = new URLSearchParams({
        aspect: params.aspect,
        zoom_boost: String(params.zoom_boost),
        x_offset: String(params.x_offset),
        y_offset: String(params.y_offset),
        layout_mode: params.layout_mode,
        tracking: String(params.tracking),
    });
    const res = await fetch(`${getBaseUrl()}/api/clips/${id}/crop-preview?${q.toString()}`, { cache: 'no-store' });
    if (!res.ok) throw new Error("Failed to load preview");
    return res.json();
};

export const deleteProject = async (id: string): Promise<void> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${id}`, {
        method: 'DELETE'
    });
    if (!res.ok) throw new Error("Failed to delete project");
};

export async function getSettings(): Promise<Settings> {
    try {
        const res = await fetch(`${getBaseUrl()}/api/settings`);
        if (!res.ok) {
            // Return default settings if backend returns error
            return {
                llm_provider: "openai",
                llm_model: "gpt-4o",
                whisper_model: "base",
                caption_style: "viral_word",
                has_api_key: false
            };
        }
        return res.json();
    } catch {
        // Backend not running - return default settings
        return {
            llm_provider: "openai",
            llm_model: "gpt-4o",
            whisper_model: "base",
            caption_style: "viral_word",
            has_api_key: false
        };
    }
}

export async function saveSettings(
    settings: Partial<Settings> & { llm_api_key?: string }
): Promise<void> {
    const res = await fetch(`${getBaseUrl()}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings)
    });
    if (!res.ok) throw new Error("Failed to save settings");
}

export const getEditorState = async (clipId: string): Promise<any> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/editor-state`, { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to load editor state");
    return res.json();
};
export const saveEditorState = async (clipId: string, state: any): Promise<void> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/editor-state`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
    });
    if (!res.ok) throw new Error(await res.text());
};
export const renderEditorClip = async (clipId: string): Promise<Clip> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/editor-render`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
};

export const getCandidates = async (projectId: string): Promise<Candidate[]> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${projectId}/candidates`, { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to load candidates");
    return res.json();
};

export const getReviews = async (projectId: string): Promise<Review[]> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${projectId}/reviews`, { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to load reviews");
    return res.json();
};

export const reviewClip = async (clipId: string, decision: "KEEP" | "EDIT" | "REJECT", reason?: string, notes?: string): Promise<void> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, reason, notes }),
    });
    if (!res.ok) throw new Error(await res.text());
};

export const runABExperiment = async (projectId: string): Promise<any> => {
    const res = await fetch(`${getBaseUrl()}/api/projects/${projectId}/ab-experiment`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
};

export const startEditorRender = async (clipId: string): Promise<{ job_id: string; status: string; clip_id: string }> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/editor-render`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
};

export const getRenderJob = async (clipId: string): Promise<any> => {
    const res = await fetch(`${getBaseUrl()}/api/clips/${clipId}/render-job`, { cache: "no-store" });
    if (!res.ok) return null;
    return res.json();
};

export const cancelRenderJob = async (clipId: string): Promise<void> => {
    await fetch(`${getBaseUrl()}/api/clips/${clipId}/render-job/cancel`, { method: "POST" });
};

// ---------------------------------------------------------------------------
// Automation hub
// ---------------------------------------------------------------------------

const auto = (p: string) => `${getBaseUrl()}/api/automation${p}`;

async function autoJson(p: string, init?: RequestInit): Promise<any> {
    const res = await fetch(auto(p), { cache: "no-store", ...init });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

export const getAutomationPolicy = async (): Promise<any> =>
    autoJson("/policy");

export const saveAutomationPolicy = async (policy: any): Promise<any> =>
    autoJson("/policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy }),
    });

export const scanCampaigns = async (provider = "mock", campaigns?: any[]): Promise<any> =>
    autoJson("/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, campaigns: campaigns ?? [] }),
    });

export const scanStoredCampaigns = async (): Promise<any> =>
    autoJson("/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "stored" }),
    });

export const createCampaign = async (campaign: any): Promise<any> =>
    autoJson("/campaigns", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(campaign),
    });

export const deleteCampaign = async (id: string): Promise<any> =>
    autoJson(`/campaigns/${id}`, { method: "DELETE" });

export const importClipApproval = async (clip_id: string, campaign_id?: string): Promise<any> =>
    autoJson("/approvals/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_id, campaign_id }),
    });

export const getAutomationCampaigns = async (status?: string): Promise<any[]> =>
    autoJson(status ? `/campaigns?status=${status}` : "/campaigns").then((r) => r.campaigns ?? []);

export const reviewAutomationCampaign = async (id: string, decision: "approve" | "reject", reviewer = "owner", reason = ""): Promise<any> =>
    autoJson(`/campaigns/${id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, reviewer, reason }),
    });

export const getAutomationAudit = async (campaign_id?: string): Promise<any[]> =>
    autoJson(campaign_id ? `/audit?campaign_id=${campaign_id}` : "/audit").then((r) => r.decisions ?? []);

export const getApprovals = async (status?: string): Promise<any[]> =>
    autoJson(status ? `/approvals?status=${status}` : "/approvals").then((r) => r.approvals ?? []);

export const decideApproval = async (clipId: string, approve: boolean, reviewer = "owner"): Promise<any> =>
    autoJson(`/approvals/${clipId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve, reviewer }),
    });

export const automationPublish = async (req: any): Promise<any> =>
    autoJson("/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run: true, ...req }),
    });

export const getPublications = async (): Promise<any[]> =>
    autoJson("/publications").then((r) => r.publications ?? []);

export const getAnalyticsSummary = async (): Promise<any> =>
    autoJson("/analytics/summary");

export const generateWeeklyReport = async (recipients?: string[]): Promise<any> =>
    autoJson("/reports/weekly", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipients: recipients ?? [] }),
    });

export const getWeeklyReports = async (): Promise<any[]> =>
    autoJson("/reports").then((r) => r.reports ?? []);

export const getPeopleTracks = async (clipId: string): Promise<any[]> =>
    autoJson(`/clips/${clipId}/people`).then((r) => r.tracks ?? []);

export const reviewPersonTrack = async (clipId: string, track_id: number, action: "BLUR" | "KEEP", reviewer = "owner"): Promise<any> =>
    autoJson(`/clips/${clipId}/people/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_id, action, reviewer }),
    });

export const addManualTrack = async (clipId: string, req: { start_time: number; end_time: number; box: { x: number; y: number; w: number; h: number }; blur?: boolean }): Promise<any> =>
    autoJson(`/clips/${clipId}/people/manual`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer: "owner", blur: true, ...req }),
    });

export const getTransitions = async (opts: { clip_id?: string; campaign_id?: string } = {}): Promise<any[]> => {
    const q = new URLSearchParams();
    if (opts.clip_id) q.set("clip_id", opts.clip_id);
    if (opts.campaign_id) q.set("campaign_id", opts.campaign_id);
    return autoJson(`/transitions?${q.toString()}`).then((r) => r.transitions ?? []);
};

export const reEvaluatePolicy = async (): Promise<any> =>
    autoJson("/policy/re-evaluate", { method: "POST" });

export const getUnresolvedSubmissions = async (): Promise<any[]> =>
    autoJson("/submissions/unresolved").then((r) => r.unresolved ?? []);

export const reconcileSubmission = async (key: string, actor = "owner"): Promise<any> =>
    autoJson(`/submissions/${encodeURIComponent(key)}/reconcile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ actor }),
    });

export const resolveSubmission = async (key: string, resolution: "confirmed" | "not_published", reason = "", post_url?: string): Promise<any> =>
    autoJson(`/submissions/${encodeURIComponent(key)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution, reviewer: "owner", reason, post_url }),
    });

export const getProviderCapabilities = async (platform = "tiktok"): Promise<any> =>
    autoJson(`/providers/capabilities?platform=${encodeURIComponent(platform)}`);

export const getAutomationHealth = async (): Promise<any> =>
    autoJson("/health");

export const getAutomationMeta = async (): Promise<any> =>
    autoJson("/meta");

export const getLiveness = async (): Promise<any> =>
    autoJson("/liveness");

export const getDiagnosticsConfig = async (): Promise<any> =>
    autoJson("/diagnostics/config");

export const getConsistency = async (): Promise<any> =>
    autoJson("/consistency");

export const getDiagnostics = async (): Promise<any> =>
    autoJson("/diagnostics");

export const getDiagnosticsExport = async (): Promise<any> =>
    autoJson("/diagnostics/export");

export const getJobTimeline = async (jobId: string): Promise<any> =>
    autoJson(`/jobs/${encodeURIComponent(jobId)}/timeline`);

export const getAutomationOverview = async (): Promise<any> =>
    autoJson("/overview");

export const getSchedulerState = async (): Promise<any> =>
    autoJson("/scheduler");

export const schedulerJobAction = async (jobId: string, action: "retry" | "cancel"): Promise<any> =>
    autoJson(`/scheduler/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });

export const getTelemetry = async (): Promise<any> =>
    autoJson("/telemetry");

export const getAlerts = async (): Promise<any[]> =>
    autoJson("/alerts").then((r) => r.alerts ?? []);

export const getReadiness = async (): Promise<any> =>
    autoJson("/readiness");

export const retentionCleanup = async (dry_run = true, paths?: string[]): Promise<any> =>
    autoJson("/retention/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run, paths: paths ?? [] }),
    });

export const providerCheck = async (platform = "tiktok"): Promise<any> =>
    autoJson(`/providers/check?platform=${encodeURIComponent(platform)}`, { method: "POST" });

export const publishPreflight = async (req: any): Promise<any> =>
    autoJson("/publish/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
    });

export const getQualifications = async (provider?: string, platform?: string): Promise<any[]> => {
    const q = new URLSearchParams();
    if (provider) q.set("provider", provider);
    if (platform) q.set("platform", platform);
    return autoJson(`/providers/qualifications?${q.toString()}`).then((r) => r.qualifications ?? []);
};

export const smtpVerify = async (test_recipient?: string): Promise<any> =>
    autoJson("/smtp/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ test_recipient }),
    });

export const checkClipMusic = async (clip_path: string, start = 0, duration?: number): Promise<any> =>
    autoJson("/clips/music-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_path, start, duration }),
    });

export async function getCaptionStyles(): Promise<CaptionStyle[]> {
    try {
        const res = await fetch(`${getBaseUrl()}/api/caption-styles`);
        if (res.ok) return res.json();
    } catch {
    }
    return [
        {
            key: "none",
            name: "No Captions",
            animation: "word_by_word",
            preview_colors: { text: "#666666", highlight: null, background: null }
        },
        {
            key: "classic_white",
            name: "Classic White",
            animation: "word_by_word",
            preview_colors: { text: "#FFFFFF", highlight: null, background: null }
        },
        {
            key: "tiktok_style",
            name: "TikTok Style",
            animation: "highlight",
            preview_colors: { text: "#FFFFFF", highlight: "#FFFF00", background: null }
        },
        {
            key: "viral_word",
            name: "Viral Word",
            animation: "one_word",
            preview_colors: { text: "#FFFFFF", highlight: null, background: null }
        }
    ];
}
