export interface Project {
    id: string;
    youtube_url: string;
    title: string;
    status:
        | 'pending'
        | 'downloading'
        | 'transcribing'
        | 'analyzing'
        | 'processing'
        | 'done'
        | 'error';
    created_at: string;
    clip_count?: number;
    size_bytes?: number;
    pipeline_options?: Record<string, unknown>;
}

export interface Clip {
    id: string
    project_id: string
    file_path: string
    start_time: number
    end_time: number
    duration: number
    reframed: boolean
    captioned: boolean
    title?: string
    reason?: string
    viral_score?: number
    face_count?: number
    layout_mode?: string
    aspect?: string
    hashtags?: string[]
    tags?: string[]
}

export interface ProgressUpdate {
    stage: string;
    percent: number;
    message: string;
}

export interface Settings {
    llm_provider: 'auto' | 'openai' | 'anthropic' | 'gemini' | 'ollama' | 'openrouter' | 'mistral' | 'groq' | 'anyapi' | 'cohere' | 'llm7' | 'xkiro';
    llm_model: string;
    whisper_model: 'base' | 'small' | 'medium';
    has_api_key: boolean;
    caption_style: string;
}

export interface CaptionStyle {
    key: string;
    name: string;
    animation: 'word_by_word' | 'highlight' | 'one_word';
    preview_colors: {
        text: string;
        highlight: string | null;
        background: string | null;
    };
}

export interface ClipSuggestion {
    start: number;
    end: number;
    title: string;
    reason: string;
}

export interface Candidate {
    id: string;
    project_id: string;
    start_time: number;
    end_time: number;
    duration: number;
    hook_score?: number;
    hook_first_3s?: number;
    payoff_score?: number;
    curiosity?: number;
    information?: number;
    narrative?: number;
    audio_score?: number;
    visual_score?: number;
    reaction_score?: number;
    pacing_score?: number;
    final_score?: number;
    first_3s_transcript?: string;
    first_sentence?: string;
    payoff_sentence?: string;
    final_sentence?: string;
    failure_reasons?: string[];
    hook_detail?: Record<string, unknown>;
    payoff_detail?: Record<string, unknown>;
    context_detail?: Record<string, unknown>;
    selected?: number;
    created_at?: string;
}

export interface Review {
    id: string;
    clip_id: string;
    project_id: string;
    decision: "KEEP" | "EDIT" | "REJECT";
    reason?: string | null;
    notes?: string | null;
    created_at: string;
}

export interface ABExperimentResult {
    project_id: string;
    experiment_id?: string;
    A_current: Array<{ start: number; end: number; final_score: number; hook: number; payoff: number; first_3s: string }>;
    B_new: Array<{ start: number; end: number; final_score: number; hook_first_3s: number; payoff: number; first_3s: string }>;
    overlap: number;
}

export interface ABExperimentRun {
    id: string;
    project_id: string;
    created_at: string;
    payload: ABExperimentResult;
}

export function formatBytes(bytes?: number): string {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}
