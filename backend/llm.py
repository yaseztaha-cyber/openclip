"""
llm.py — Multi-provider LLM integration for clip suggestion (v4.0 Map-Reduce).
"""

import json
import re
import logging
import asyncio
import random
from typing import Optional, Callable

import httpx  # type: ignore
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type, retry_if_exception

logger = logging.getLogger(__name__)

_TIMEOUT = 120.0

# Rate limit tracking - stores the last retry-after value
_last_retry_after: float = 0.0

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_clip_suggestions(
    transcript: list[dict],
    provider: str,
    api_key: str,
    model: str,
    video_duration: float,
    progress_callback: Optional[Callable[[str, float, str], None]] = None
) -> list[dict]:
    """
    Map-Reduce approach to stay under TPM limits.
    
    Phase 1 (Map): Split transcript into chunks,
                   find candidates in each chunk
    Phase 2 (Reduce): Rank all candidates,
                      return final top 5-10 clips
    """
    if not transcript:
        logger.warning("Empty transcript — skipping LLM call")
        return []

    def progress(stage: str, pct: float, msg: str):
        if progress_callback:
            progress_callback(stage, pct, msg)
        else:
            logger.info(f"Progress: [{stage}] {pct}% - {msg}")

    # Step 1 — Split into chunks of max 8 minutes each (larger chunks = fewer API calls)
    chunks = _chunk_transcript(transcript, chunk_minutes=8)

    logger.info(f"Split transcript into {len(chunks)} chunks for processing")

    # Step 2 — Get candidates from each chunk (MAP)
    all_candidates = []
    for i, chunk in enumerate(chunks):
        progress(
            "analyzing",
            55 + (i / len(chunks) * 15),
            f"Analyzing part {i+1} of {len(chunks)}..."
        )
        candidates = await _analyze_chunk(chunk, i, len(chunks), provider, api_key, model)
        all_candidates.extend(candidates)

        # Add delay between chunks to avoid rate limits (skip delay on last chunk)
        # Use 4-6 second delay with jitter to stay well under 50 RPM limit
        if i < len(chunks) - 1:
            base_delay = 4.0
            jitter = random.uniform(0, 2.0)  # Add 0-2s random jitter
            delay_seconds = base_delay + jitter
            logger.info(f"Rate limit protection: waiting {delay_seconds:.1f}s before next chunk...")
            await asyncio.sleep(delay_seconds)

    # Step 3 — If only 1 chunk, candidates are already final
    if len(chunks) == 1:
        return _validate_suggestions(all_candidates, video_duration)

    # Step 4 — Rank all candidates (REDUCE)
    # Add delay before reduce step to avoid hitting rate limits
    await asyncio.sleep(random.uniform(3.0, 5.0))

    progress("analyzing", 72, "Ranking best moments...")
    final_clips = await _rank_candidates(all_candidates, video_duration, provider, api_key, model)
    progress("analyzing", 75, f"Found {len(final_clips)} clips")

    return _validate_suggestions(final_clips, video_duration)


def _chunk_transcript(
    transcript: list[dict],
    chunk_minutes: int = 3
) -> list[list[dict]]:
    """
    Split transcript segments into chunks of N minutes.
    """
    chunk_seconds = chunk_minutes * 60
    overlap = 15  # seconds
    chunks = []
    start = 0.0
    
    while start < transcript[-1]["end"]:
        end = start + chunk_seconds + overlap
        chunk = [
            seg for seg in transcript
            if seg["start"] >= start and seg["start"] < end
        ]
        if chunk:
            chunks.append(chunk)
        start += chunk_seconds  # next chunk starts without overlap
        
    return chunks


async def _analyze_chunk(
    chunk: list[dict],
    chunk_index: int,
    total_chunks: int,
    provider: str,
    api_key: str,
    model: str
) -> list[dict]:
    """
    Send ONE chunk to LLM. Stay under 1500 tokens.
    """
    prompt = _build_chunk_prompt(chunk, chunk_index, total_chunks)
    raw = await _call_provider_with_retry(provider, api_key, model, prompt)
    logger.info(f"LLM raw response for chunk {chunk_index+1}: {raw[:500]}")
    parsed = _parse_llm_json(raw)
    logger.info(f"Parsed {len(parsed)} clips from chunk {chunk_index+1}")
    return parsed


async def _rank_candidates(
    candidates: list[dict],
    video_duration: float, # passed for consistency but might not be used here
    provider: str,
    api_key: str,
    model: str
) -> list[dict]:
    """
    Final reduce step. Send ALL candidates to LLM
    and ask it to pick the best 5-10.
    """
    prompt = _build_reduce_prompt(candidates)
    raw = await _call_provider_with_retry(provider, api_key, model, prompt)
    return _parse_llm_json(raw)


def _format_transcript(segments: list[dict]) -> str:
    """
    Format segments as readable timestamped text.
    """
    lines = []
    for seg in segments:
        text = seg['text'].strip()
        if not text:
            continue
        start_m = int(seg['start'] // 60)
        start_s = int(seg['start'] % 60)
        end_m = int(seg['end'] // 60)
        end_s = int(seg['end'] % 60)
        lines.append(f"[{start_m:02d}:{start_s:02d}-{end_m:02d}:{end_s:02d}] {text}")
    return "\n".join(lines)


def _format_candidates(candidates: list[dict]) -> str:
    """
    Format candidates list as compact text for reduce step.
    """
    lines = []
    for i, c in enumerate(candidates):
        start_m = int(c['start'] // 60)
        start_s = int(c['start'] % 60)
        end_m = int(c['end'] // 60)
        end_s = int(c['end'] % 60)
        lines.append(f"{i+1}. [{start_m:02d}:{start_s:02d}-{end_m:02d}:{end_s:02d}] \"{c.get('title','')}\" score:{c.get('viral_score',0)} | {c.get('reason','')}")
    return "\n".join(lines)


def _validate_suggestions(
    suggestions: list[dict],
    video_duration: float
) -> list[dict]:
    """
    Final validation pass: dynamic lengths from semantic completeness.

    Keeps 10-120s suggestions (trims overlong), drops fragments. No forced
    extension — a short complete idea beats padded rambling. Boundary snap
    and score-based merge/rank happen downstream in editorial.select_clips.
    """
    logger.info(f"RAW LLM SUGGESTIONS RECEIVED FOR VALIDATION ({len(suggestions)} clips): {suggestions}")

    if not suggestions:
        logger.warning("No suggestions received from LLM at all")
        return []

    valid = _filter_suggestions(suggestions, video_duration, min_dur=10.0, target_max=120.0)
    if valid:
        logger.info(f"Validation kept {len(valid)} clips")
    else:
        logger.warning("No valid clips survived validation")
    return valid


def _filter_suggestions(
    suggestions: list[dict],
    video_duration: float,
    min_dur: float,
    target_max: float,
) -> list[dict]:
    """
    Filter and clamp clip suggestions within duration bounds.
    Returns non-overlapping clips sorted by start time.
    """
    import copy
    valid = []
    for s in suggestions:
        s = copy.deepcopy(s)

        # Swap if start > end
        if s["start"] > s["end"]:
            s["start"], s["end"] = s["end"], s["start"]

        # Clamp to valid bounds
        s["start"] = max(0, s["start"])
        s["end"] = min(s["end"], video_duration)

        if s["start"] >= s["end"]:
            continue

        dur = s["end"] - s["start"]

        if dur < min_dur:
            logger.warning(f"Dropping clip - too short ({dur:.1f}s < {min_dur}s): {s.get('title')}")
            continue
        if dur > target_max:
            s["end"] = s["start"] + target_max
            logger.info(f"Trimmed long clip to {target_max}s: {s.get('title')}")

        valid.append(s)

    valid.sort(key=lambda x: x["start"])

    # Remove overlaps: keep the STRONGER clip (hook+payoff+viral), not just
    # the earliest — an early mediocre moment must not kill a later great one.
    # (Finer IoU merge happens downstream in editorial.)
    def _strength(s: dict) -> float:
        return (float(s.get("hook", 5)) * 2.0
                + float(s.get("payoff", 5)) * 1.5
                + float(s.get("viral_score", 5)))

    non_overlapping: list[dict] = []
    for s in valid:
        if non_overlapping and s["start"] < non_overlapping[-1]["end"]:
            prev = non_overlapping[-1]
            if _strength(s) > _strength(prev):
                non_overlapping[-1] = s
            # else: drop s, keep prev (prev keeps its end)
        else:
            non_overlapping.append(s)

    return non_overlapping


def _build_chunk_prompt(chunk: list[dict], chunk_index: int, total_chunks: int) -> str:
    # Get the time bounds of this chunk
    chunk_start = chunk[0]["start"] if chunk else 0
    chunk_end = chunk[-1]["end"] if chunk else 0

    return f"""You are an expert viral short-form video editor. Your job is finding
moments viewers CANNOT scroll past — not loud moments, not random highlights,
but complete mini-stories: HOOK → CONTEXT → BUILDUP → PAYOFF → quick resolution.

The FIRST 3 SECONDS determine if the viewer stays. Every clip's opening must
be a HOOK — a sentence that alone makes a stranger stop scrolling.

This is part {chunk_index+1} of {total_chunks} of a YouTube video transcript.
This section covers timestamps from {chunk_start:.0f}s to {chunk_end:.0f}s.

Find the 2-5 BEST moments in this section. Vary lengths: if the section supports
it, include at least one punchy clip ≤25s alongside any longer story clips.

=== HOOK TEST (apply to every candidate's first sentence) ===
Would a cold viewer who knows NOTHING about the video stop scrolling on this
opening line alone? If not, the clip is WEAK — pick a different moment or a
different start point. Strong hooks contain ONE of: a surprising claim, a bold
problem, a challenged assumption, a curiosity gap ("wait, what happens next?").
Weak hooks: greetings ("hey guys"), generic advice ("you should focus on..."),
pure setup with no tension, mid-thought fragments.

=== WHAT MAKES A STRONG HOOK (judge every candidate on these) ===
1. CURIOSITY: does it open an information gap? Surprising claim, bold problem,
   challenged assumption, "wait, what happens next?" energy.
2. EMOTION: surprise, excitement, humor, tension, disbelief, controversy,
   confidence, urgency.
3. INFORMATION: concrete insight, strong claim, uncommon knowledge, actionable
   advice, important conclusion — not vibes, VALUE.
4. NARRATIVE: setup → conflict → reveal/payoff, transformation, unexpected result.
Classic hook shapes (understand equivalents, don't keyword-match):
"Nobody tells you...", "The biggest mistake...", "I was completely wrong...",
"Here's what happened...", "This changed everything...", "The problem is...",
"What most people don't understand...", "Don't ever...", "Turns out...",
"So I tried...", "But then...", "Here's the interesting part..."

=== START RULES (the opening is everything) ===
- The "start" timestamp MUST be the first word of the hook sentence itself,
  not a preamble. If the hook needs 1 sentence of setup to make sense, include
  it — but the setup itself must still be interesting (no "so, um, hey guys").
- Never start mid-sentence. Never start on filler (um/uh), greetings
  ("hey guys", "welcome back"), dead air, or a reference that needs removed
  context ("as I said before...").

=== END RULES (end AFTER the payoff, never before it) ===
- End when the idea COMPLETES: answer given, punchline landed, story resolved,
  conclusion stated, plus a beat of reaction. Then STOP — no trailing filler.
- The clip MUST end on a sentence terminal (. ? !). Never cut mid-sentence.
- Ideal arc: HOOK → CONTEXT → BUILDUP → PAYOFF → SHORT RESOLUTION.

=== LENGTH ===
- Duration comes from COMPLETENESS, not a template: 10-20s for a killer insight,
  20-45s for a story/reveal, 30-90s for an explanation. Hard limits: 10s min, 90s max.
- A short complete idea BEATS a long rambling one. Cut the fluff, keep the payoff.

=== SCORING CALIBRATION ===
- hook 8-10 = a stranger would stop scrolling on the first sentence alone.
- hook 1-4  = generic opening that needs context to matter.
- payoff 8-10 = the payoff sentence alone satisfies the hook's promise.
- payoff 1-4 = clip ends before the answer/punchline lands.
Score honestly — a weak hook with a strong payoff is still a weak clip.

=== REJECT ===
Incomplete thoughts, unclear references, weak openings, no payoff, only
interesting after endless setup, pure reactions with no information.

Return 2-5 clips (fewer is fine if the section is weak — quality over quota).

TRANSCRIPT SECTION:
{_format_transcript(chunk)}

Return ONLY JSON array:
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<catchy clickbait title, max 8 words>",
    "reason": "<one sentence: hook + payoff>",
    "viral_score": <1-10>,
    "hook": <1-10 curiosity strength>,
    "curiosity": <1-10 information gap>,
    "emotion": <1-10 emotional intensity>,
    "info": <1-10 information value>,
    "narrative": <1-10 story completeness>,
    "payoff": <1-10 payoff strength>,
    "hashtags": ["#tag1", "#tag2", "#tag3"],
    "tags": ["keyword1", "keyword2", "keyword3"]
  }}
]
If no strong moments found return: []"""


def _build_reduce_prompt(candidates: list[dict]) -> str:
    return f"""You are an expert viral short-form video editor doing final selection.
Below are candidate clip moments found across a YouTube video. Each already has
a HOOK → PAYOFF arc. Your job: keep only genuinely strong, DISTINCT clips.

CANDIDATES:
{_format_candidates(candidates)}

Rules:
- Return 5-10 clips. Quality over quota: 5 great beats 10 mediocre.
- REJECT any clip whose first sentence fails the HOOK TEST (a cold viewer
  would not stop scrolling) or whose payoff is missing/incomplete.
- Among near-duplicates of the same moment, keep the ONE with the strongest
  hook+payoff combo, drop the rest.
- Prefer clips covering DIFFERENT moments/topics across the video.
- Ensure length variety: keep at least two clips ≤30s when candidates allow —
  Shorts audiences need fast payoffs, not only 60-90s explainers.
- Keep each clip's timestamps/scores/fields exactly as given (do not lengthen
  to fill time). Lengths vary by completeness: 10-90s.
- Every clip MUST end on a sentence terminal (. ? !) — reject mid-sentence ends.
- Sort by start time ascending. "start"/"end" stay float seconds.

Return ONLY the selected candidates as JSON array in the exact same format. Do not remove any fields.
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "title": "<catchy clickbait title, max 8 words>",
    "reason": "<one sentence>",
    "viral_score": <1-10>,
    "hook": <1-10>,
    "curiosity": <1-10>,
    "emotion": <1-10>,
    "info": <1-10>,
    "narrative": <1-10>,
    "payoff": <1-10>,
    "hashtags": ["#tag1", "#tag2", "#tag3"],
    "tags": ["keyword1", "keyword2", "keyword3"]
  }}
]"""

# ---------------------------------------------------------------------------
# Provider wrappers
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    """Custom exception for rate limit errors with retry-after support."""
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def _get_retry_wait():
    """Custom wait strategy that respects retry-after header."""
    # Use random exponential backoff: min=5s, max=120s
    return wait_random_exponential(multiplier=2, min=5, max=120)


# Per-provider model fallback chains (tried in order after the requested model).
# Verified working Sep 2026 — see handler docstrings.
PROVIDER_MODEL_FALLBACKS: dict[str, list[str]] = {
    "groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "cohere": ["command-a-03-2025", "command-r-plus-08-2024"],
    "xkiro": ["qwen/qwen3-coder-plus:free", "qwen/qwen3.7-max:free", "minimax/minimax-m2.5:free"],
    "llm7": ["default", "fast", "DeepSeek-V4-Flash-0731"],
    "openrouter": ["cohere/north-mini-code:free", "nvidia/nemotron-3-ultra-550b-a55b:free"],
    "mistral": ["ministral-8b-latest", "ministral-3b-latest"],
    "anyapi": ["qwen/qwen3-coder:free", "nvidia/nemotron-3-nano-30b-a3b:free"],
}


def _is_retriable(exc: BaseException) -> bool:
    """Don't waste backoff retries on auth errors, dead models, or bad requests."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code not in (400, 401, 403, 404)
    return True


@retry(
    stop=stop_after_attempt(6),  # 6 attempts total per model
    wait=_get_retry_wait(),
    retry=retry_if_exception(_is_retriable),
    reraise=True
)
async def _call_single_model(handler, prompt: str, api_key: str, model: str) -> str:
    global _last_retry_after

    # If we recently hit a rate limit, wait the specified time before trying
    if _last_retry_after > 0:
        wait_time = _last_retry_after
        _last_retry_after = 0  # Reset after using
        logger.info(f"Respecting previous retry-after: waiting {wait_time}s...")
        await asyncio.sleep(wait_time)

    try:
        return await handler(prompt, api_key, model)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            # Parse retry-after header if present
            retry_after_str = e.response.headers.get("retry-after", "")
            try:
                retry_after = float(retry_after_str) if retry_after_str else 30.0
            except ValueError:
                retry_after = 30.0  # Default 30s if header is malformed

            # Add jitter to prevent thundering herd
            retry_after += random.uniform(1, 5)
            _last_retry_after = retry_after

            logger.warning(f"Rate limit hit (429). retry-after header: {retry_after_str}. Waiting {retry_after:.1f}s...")
            await asyncio.sleep(retry_after)
        raise
    except RuntimeError as e:
        # Handle Gemini rate limit and timeout errors (raised as RuntimeError)
        error_str = str(e).lower()
        if "rate limit" in error_str or "quota" in error_str:
            retry_after = 30.0 + random.uniform(5, 15)  # 35-45 seconds
            _last_retry_after = retry_after
            logger.warning(f"Gemini rate limit detected. Waiting {retry_after:.1f}s...")
            await asyncio.sleep(retry_after)
        elif "timeout" in error_str:
            # For timeouts, wait a bit less but still retry
            retry_after = 10.0 + random.uniform(2, 5)
            logger.warning(f"Gemini timeout. Waiting {retry_after:.1f}s before retry...")
            await asyncio.sleep(retry_after)
        raise


async def _call_provider_with_retry(provider: str, api_key: str, model: str, prompt: str) -> str:
    """
    Call a provider, falling back to other models if one fails.

    Tries the requested model first, then PROVIDER_MODEL_FALLBACKS.
    A bad API key (401) or missing key fails fast; dead/gated models
    (403/404) skip straight to the next candidate; rate limits and
    transient errors are retried with backoff per model.
    """
    dispatch = {
        "openai": _call_openai,
        "anthropic": _call_anthropic,
        "gemini": _call_gemini,
        "ollama": _call_ollama,
        "openrouter": _call_openrouter,
        "mistral": _call_mistral,
        "groq": _call_groq,
        "anyapi": _call_anyapi,
        "cohere": _call_cohere,
        "llm7": _call_llm7,
        "xkiro": _call_xkiro,
    }
    handler = dispatch.get(provider)
    if handler is None:
        raise ValueError(f"Unknown LLM provider: {provider}")

    candidates: list[str] = []
    for m in [model, *PROVIDER_MODEL_FALLBACKS.get(provider, [])]:
        m = (m or "").strip()
        if m and m not in candidates:
            candidates.append(m)
    if not candidates:
        candidates = [""]  # let the handler apply its own default model

    last_exc: Exception | None = None
    for idx, candidate in enumerate(candidates):
        try:
            if idx > 0:
                logger.warning("Provider %s: falling back to model '%s'", provider, candidate)
            return await _call_single_model(handler, prompt, api_key, candidate)
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else -1
            if status == 401:
                raise  # bad key — fallbacks can't help
            logger.warning(
                "Provider %s model '%s' failed (HTTP %s), trying next...",
                provider, candidate, status,
            )
        except RuntimeError as e:
            last_exc = e
            if "api key missing" in str(e).lower():
                raise  # no key — fallbacks can't help
            logger.warning("Provider %s model '%s' errored (%s), trying next...", provider, candidate, e)

    assert last_exc is not None
    raise last_exc


async def _call_openai(prompt: str, api_key: str, model: str) -> str:
    model = model or "gpt-4o"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_anthropic(prompt: str, api_key: str, model: str) -> str:
    model = model or "claude-3-5-sonnet-20241022"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def _call_gemini(prompt: str, api_key: str, model: str) -> str:
    # Use the user's specified model, or fall back to gemini-2.0-flash
    model = model or "gemini-2.0-flash"
    logger.info(f"Calling Gemini with model: {model}")

    def _do_call() -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "Gemini SDK missing. Install with: pip install google-genai"
            ) from exc

        effective_api_key = api_key or ""
        if not effective_api_key:
            import os
            effective_api_key = os.environ.get("GEMINI_API_KEY", "")
        if not effective_api_key:
            raise RuntimeError("Gemini API key missing. Set it in Settings or GEMINI_API_KEY env var.")

        # Create client with explicit timeout via http_options
        client = genai.Client(
            api_key=effective_api_key,
            http_options={"timeout": 90000}  # 90 second timeout in milliseconds
        )

        # Simplified config - no tools or thinking mode to reduce token usage
        generate_content_config = types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        )

        try:
            logger.info(f"Sending prompt to Gemini ({len(prompt)} chars)...")
            # Use non-streaming for simpler, more reliable responses
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=generate_content_config,
            )
            result = response.text or "[]"
            logger.info(f"Gemini response received ({len(result)} chars)")
            return result
        except Exception as e:
            error_str = str(e).lower()
            logger.error(f"Gemini API error: {e}")
            # Handle Gemini rate limit errors
            if "429" in error_str or "resource_exhausted" in error_str or "quota" in error_str:
                logger.warning(f"Gemini rate limit detected: {e}")
                raise RuntimeError(f"Rate limit: {e}")
            # Handle timeout errors
            if "timeout" in error_str or "deadline" in error_str:
                logger.warning(f"Gemini timeout detected: {e}")
                raise RuntimeError(f"Timeout: {e}")
            raise

    # Use asyncio.wait_for with timeout to prevent hanging
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_do_call),
            timeout=120.0  # 120 second overall timeout
        )
    except asyncio.TimeoutError:
        logger.error("Gemini API call timed out after 120 seconds")
        raise RuntimeError("Gemini API timeout - the model is taking too long to respond")


async def _call_openrouter(prompt: str, api_key: str, model: str) -> str:
    """
    OpenRouter (OpenAI-compatible gateway).

    Get a key at https://openrouter.ai/keys and use any model id from
    https://openrouter.ai/models. Verified working free picks (Sep 2026):
      - "cohere/north-mini-code:free" (default: fast, valid JSON, 256K ctx)
      - "nvidia/nemotron-3-ultra-550b-a55b:free" (fallback: 1M ctx, slower)
    Note: free models are rate-limited (20 req/min). The map-reduce
    pipeline already spaces calls and retries with backoff on 429s.
    """
    model = model or "cohere/north-mini-code:free"
    if not api_key:
        raise RuntimeError(
            "OpenRouter API key missing. Get one at https://openrouter.ai/keys "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Recommended by OpenRouter for analytics/rate-limit attribution
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "OpenClip",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected OpenRouter response: {data!r:.500}") from exc


async def _call_mistral(prompt: str, api_key: str, model: str) -> str:
    """
    Mistral AI (La Plateforme), OpenAI-compatible chat completions.

    Get a key at https://console.mistral.ai. Verified working on a
    free-tier key (Sep 2026):
      - "ministral-8b-latest" (default: valid JSON, fast)
      - "ministral-3b-latest" (fallback: smaller, faster)
    Note: larger models (mistral-small/medium-latest) may return 429
    on keys without quota for them. The pipeline retries 429s with
    backoff automatically.
    """
    model = model or "ministral-8b-latest"
    if not api_key:
        raise RuntimeError(
            "Mistral API key missing. Get one at https://console.mistral.ai "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Mistral response: {data!r:.500}") from exc


async def _call_groq(prompt: str, api_key: str, model: str) -> str:
    """
    Groq (OpenAI-compatible, ultra-low latency).

    Get a key at https://console.groq.com. Free tier with generous
    limits. Verified working (Sep 2026), fastest first:
      - "qwen/qwen3.8-27b" (default: valid JSON, ~1.4s)
      - "openai/gpt-oss-120b" (biggest, valid JSON, ~2.2s)
      - "openai/gpt-oss-20b" (smallest, valid JSON, ~2s)
    """
    model = model or "qwen/qwen3.8-27b"
    if not api_key:
        raise RuntimeError(
            "Groq API key missing. Get one at https://console.groq.com "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Groq response: {data!r:.500}") from exc


async def _call_anyapi(prompt: str, api_key: str, model: str) -> str:
    """
    AnyAPI unified gateway (OpenAI-compatible).

    Docs: https://docs.anyapi.ai — free tier is 100k tokens/day.
    Free models use OpenRouter-style ids, e.g. "qwen/qwen3-coder:free".
    """
    model = model or "qwen/qwen3-coder:free"
    if not api_key:
        raise RuntimeError(
            "AnyAPI key missing. Get one at https://anyapi.ai "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.anyapi.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected AnyAPI response: {data!r:.500}") from exc


async def _call_cohere(prompt: str, api_key: str, model: str) -> str:
    """
    Cohere (v2 Chat API — own format, not OpenAI-compatible).

    Get a key at https://dashboard.cohere.com. Verified working (Sep 2026):
      - "command-a-03-2025" (default: flagship, valid JSON)
      - "command-r-plus-08-2024" (fallback: valid JSON)
    """
    model = model or "command-a-03-2025"
    if not api_key:
        raise RuntimeError(
            "Cohere API key missing. Get one at https://dashboard.cohere.com "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.cohere.com/v2/chat",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            blocks = data.get("message", {}).get("content", []) or []
            text = "".join(
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ) or data.get("text", "")
            if not text:
                raise ValueError("empty content")
            return text
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Unexpected Cohere response: {data!r:.500}") from exc


async def _call_llm7(prompt: str, api_key: str, model: str) -> str:
    """
    LLM7.io unified gateway (OpenAI-compatible).

    Docs: https://docs.llm7.io — base https://api.llm7.io/v1.
    Free turbo tier. Verified working (Sep 2026, valid JSON ~3s):
      - "default" (auto-routed, recommended)
      - "fast" (lowest latency route)
      - "DeepSeek-V4-Flash-0731" (concrete turbo model)
    """
    model = model or "default"
    if not api_key:
        raise RuntimeError(
            "LLM7 token missing. Get one at https://dash.llm7.io/ "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.llm7.io/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM7 response: {data!r:.500}") from exc


async def _call_xkiro(prompt: str, api_key: str, model: str) -> str:
    """
    xKiro unified gateway (OpenAI-compatible, vendor/model IDs).

    Docs: https://docs.xkiro.com — base https://api.xkiro.com/v1.
    Free tier, verified working (Sep 2026, valid JSON):
      - "qwen/qwen3-coder-plus:free" (default: coder, ~5s)
      - "qwen/qwen3.7-max:free" (flagship free, ~6s)
      - "minimax/minimax-m2.5:free" (fallback, slower ~20s)
    """
    model = model or "qwen/qwen3-coder-plus:free"
    if not api_key:
        raise RuntimeError(
            "xKiro API key missing. Get one at https://xkiro.com "
            "and paste it in Settings."
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.xkiro.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected xKiro response: {data!r:.500}") from exc


async def _call_ollama(prompt: str, api_key: str, model: str) -> str:
    model = model or "llama3"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "[]")


# ---------------------------------------------------------------------------
# JSON response parser
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_llm_json(raw: str) -> list[dict]:
    fence_match = _FENCE_RE.search(raw)
    if fence_match:
        raw = fence_match.group(1)

    raw = raw.strip()
    if not raw:
        return []

    # Try to extract just the JSON array if there's extra text around it
    # (some LLMs add explanatory text before/after the JSON)
    if not raw.startswith("["):
        bracket_start = raw.find("[")
        if bracket_start != -1:
            raw = raw[bracket_start:]

    if not raw.endswith("]"):
        bracket_end = raw.rfind("]")
        if bracket_end != -1:
            raw = raw[:bracket_end + 1]

    # Fix trailing commas before ] (common LLM mistake)
    raw = re.sub(r",\s*]", "]", raw)
    # Fix trailing commas before } (common LLM mistake)
    raw = re.sub(r",\s*}", "}", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM JSON: %.500s", raw)
        return []

    if not isinstance(parsed, list):
        logger.error("LLM returned non-list JSON: %s", type(parsed))
        return []

    required_keys = {"start", "end", "title"}
    valid: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not required_keys.issubset(item.keys()):
            logger.warning("Skipping invalid suggestion: %s", item)
            continue
        try:
            hashtags = item.get("hashtags", [])
            if isinstance(hashtags, str):
                # LLM returned a single string like "#tag1 #tag2" — split it
                hashtags = [h.strip() for h in hashtags.replace(",", " ").split() if h.strip()]
            elif not isinstance(hashtags, list):
                hashtags = []
            hashtags = [str(h) for h in hashtags if h]

            tags = item.get("tags", [])
            if isinstance(tags, str):
                # LLM returned a single string like "keyword1, keyword2" — split it
                tags = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
            elif not isinstance(tags, list):
                tags = []
            tags = [str(t) for t in tags if t]

            def _score10(key: str) -> int:
                try:
                    return max(1, min(10, int(item.get(key, 5))))
                except (ValueError, TypeError):
                    return 5

            valid.append({
                "start": float(item["start"]),
                "end": float(item["end"]),
                "title": str(item["title"]),
                "reason": str(item.get("reason", "")),
                "viral_score": _score10("viral_score"),
                "hook": _score10("hook"),
                "curiosity": _score10("curiosity"),
                "emotion": _score10("emotion"),
                "info": _score10("info"),
                "narrative": _score10("narrative"),
                "payoff": _score10("payoff"),
                "hashtags": hashtags,
                "tags": tags,
            })
        except (ValueError, TypeError):
            logger.warning("Skipping suggestion with bad types: %s", item)

    return valid

