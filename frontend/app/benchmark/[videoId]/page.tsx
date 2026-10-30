"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api";

const CATEGORIES = [
  "strong_opening", "surprising_information", "emotional_reaction",
  "important_explanation", "funny_moment", "demonstration",
  "story_payoff", "strong_opinion", "transformation",
  "useful_information", "other",
];

const REJECTION_REASONS = [
  "starts_mid_sentence", "ends_mid_sentence", "missing_context",
  "excessive_silence", "uninteresting_moment", "poor_audio",
  "poor_framing", "duplicate_moment", "incorrect_duration", "other",
];

const LABELS = ["Excellent", "Good", "Acceptable", "Weak", "Reject"];

function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m.toString().padStart(2, "0")}:${sec.toFixed(3).padStart(6, "0")}`;
}

export default function BenchmarkReviewPage() {
  const params = useParams();
  const videoId = params.videoId as string;
  const api = getApiBaseUrl();
  const videoRef = useRef<HTMLVideoElement>(null);

  const [videoUrl, setVideoUrl] = useState("");
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);

  // Annotation state
  const [annStart, setAnnStart] = useState(0);
  const [annEnd, setAnnEnd] = useState(0);
  const [annTitle, setAnnTitle] = useState("");
  const [annDesc, setAnnDesc] = useState("");
  const [annWhy, setAnnWhy] = useState("");
  const [annCategories, setAnnCategories] = useState<string[]>([]);
  const [annImportance, setAnnImportance] = useState(3);
  const [annConfidence, setAnnConfidence] = useState(3);
  const [annNotes, setAnnNotes] = useState("");
  const [annotations, setAnnotations] = useState<any[]>([]);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  // Candidate review state
  const [candidates, setCandidates] = useState<any[]>([]);
  const [activeCandidate, setActiveCandidate] = useState<any | null>(null);
  const [reviewRatings, setReviewRatings] = useState({
    opening_hook: 0, context_completeness: 0, interesting_moment: 0,
    ending_payoff: 0, audio_clarity: 0, visual_quality: 0, overall_usefulness: 0,
  });
  const [reviewLabel, setReviewLabel] = useState("");
  const [reviewReasons, setReviewReasons] = useState<string[]>([]);
  const [reviewNotes, setReviewNotes] = useState("");
  const [reviews, setReviews] = useState<any[]>([]);

  useEffect(() => {
    // Try to load video from project
    fetch(`${api}/api/projects`).then(r => r.json()).then(projects => {
      // Find project with matching video
      if (Array.isArray(projects) && projects.length > 0) {
        const proj = projects[0];
        // Use files endpoint
        setVideoUrl(`${api}/files/${videoId}/video.mp4`);
      }
    }).catch(() => {});
    loadAnnotations();
    loadReviews();
  }, [api, videoId]);

  const loadAnnotations = async () => {
    try {
      const res = await fetch(`${api}/api/benchmark/annotations/${videoId}`);
      const data = await res.json();
      setAnnotations(data.annotations || []);
    } catch (e) { console.error(e); }
  };

  const loadReviews = async () => {
    try {
      const res = await fetch(`${api}/api/benchmark/reviews/${videoId}`);
      const data = await res.json();
      setReviews(data.reviews || []);
    } catch (e) { console.error(e); }
  };

  const handleTimeUpdate = () => {
    if (videoRef.current) setCurrentTime(videoRef.current.currentTime);
  };

  const handleLoadedMetadata = () => {
    if (videoRef.current) setDuration(videoRef.current.duration);
  };

  const togglePlay = () => {
    if (videoRef.current) {
      if (playing) videoRef.current.pause();
      else videoRef.current.play();
      setPlaying(!playing);
    }
  };

  const seekTo = (t: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = Math.max(0, Math.min(duration, t));
    }
  };

  const setStart = () => setAnnStart(currentTime);
  const setEnd = () => setAnnEnd(currentTime);

  const previewMoment = () => {
    seekTo(Math.max(0, annStart - 2));
    if (videoRef.current) videoRef.current.play();
    setTimeout(() => { if (videoRef.current) videoRef.current.pause(); }, (annEnd - annStart + 4) * 1000);
  };

  const saveAnnotation = async () => {
    if (annEnd <= annStart) { setMessage("End must be after start"); return; }
    if (annStart < 0 || annEnd > duration) { setMessage("Timestamps out of range"); return; }
    setSaving(true);
    try {
      const res = await fetch(`${api}/api/benchmark/annotations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          video_id: videoId,
          annotator_id: "local_reviewer",
          start: annStart,
          end: annEnd,
          title: annTitle,
          description: annDesc,
          why_valuable: annWhy,
          categories: annCategories,
          importance: annImportance,
          confidence: annConfidence,
          notes: annNotes,
        }),
      });
      const data = await res.json();
      if (data.success) {
        setMessage("Annotation saved");
        loadAnnotations();
      } else {
        setMessage("Failed to save");
      }
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setSaving(false);
    }
  };

  const toggleCategory = (cat: string) => {
    setAnnCategories(prev =>
      prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat]
    );
  };

  const toggleReason = (r: string) => {
    setReviewReasons(prev =>
      prev.includes(r) ? prev.filter(x => x !== r) : [...prev, r]
    );
  };

  const saveReview = async () => {
    if (!activeCandidate) return;
    setSaving(true);
    try {
      const res = await fetch(`${api}/api/benchmark/reviews`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_id: activeCandidate.id || activeCandidate.candidate_id || "cand",
          video_id: videoId,
          reviewer_id: "local_reviewer",
          ...reviewRatings,
          label: reviewLabel,
          rejection_reasons: reviewReasons,
          notes: reviewNotes,
        }),
      });
      const data = await res.json();
      if (data.success) {
        setMessage("Review saved");
        loadReviews();
      } else {
        setMessage("Failed to save review");
      }
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setSaving(false);
    }
  };

  const previewCandidate = (c: any) => {
    setActiveCandidate(c);
    seekTo(Math.max(0, (c.start || 0) - 2));
    if (videoRef.current) videoRef.current.play();
  };

  return (
    <div style={{ padding: 20, maxWidth: 1200, margin: "0 auto" }}>
      <h1>Benchmark Review: {videoId}</h1>
      {message && <div style={{ padding: 10, background: "#222", color: "#0f0", marginBottom: 10 }}>{message}</div>}

      {/* Video Player */}
      <div style={{ marginBottom: 20 }}>
        <video
          ref={videoRef}
          src={videoUrl}
          onTimeUpdate={handleTimeUpdate}
          onLoadedMetadata={handleLoadedMetadata}
          style={{ width: "100%", maxHeight: 400, background: "#000" }}
        />
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10 }}>
          <button onClick={togglePlay}>{playing ? "Pause" : "Play"}</button>
          <span>{formatTime(currentTime)} / {formatTime(duration)}</span>
          <input
            type="range" min={0} max={duration} step={0.1} value={currentTime}
            onChange={e => seekTo(parseFloat(e.target.value))}
            style={{ flex: 1 }}
          />
        </div>
      </div>

      {/* Source Annotation */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Source Annotation (Human-Interest Moment)</h2>
        <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
          <button onClick={setStart}>Set Start ({formatTime(annStart)})</button>
          <button onClick={setEnd}>Set End ({formatTime(annEnd)})</button>
          <button onClick={previewMoment}>Preview</button>
        </div>
        <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
          <label>Start: <input type="number" step={0.1} value={annStart} onChange={e => setAnnStart(parseFloat(e.target.value) || 0)} style={{ width: 80 }} /></label>
          <label>End: <input type="number" step={0.1} value={annEnd} onChange={e => setAnnEnd(parseFloat(e.target.value) || 0)} style={{ width: 80 }} /></label>
          <span>Duration: {(annEnd - annStart).toFixed(2)}s</span>
        </div>
        <div style={{ marginBottom: 10 }}>
          <input placeholder="Title" value={annTitle} onChange={e => setAnnTitle(e.target.value)} style={{ width: "100%", marginBottom: 5 }} />
          <textarea placeholder="Description" value={annDesc} onChange={e => setAnnDesc(e.target.value)} style={{ width: "100%", marginBottom: 5 }} rows={2} />
          <textarea placeholder="Why valuable" value={annWhy} onChange={e => setAnnWhy(e.target.value)} style={{ width: "100%", marginBottom: 5 }} rows={2} />
        </div>
        <div style={{ marginBottom: 10 }}>
          <strong>Categories:</strong>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 5 }}>
            {CATEGORIES.map(cat => (
              <label key={cat} style={{ fontSize: 12 }}>
                <input type="checkbox" checked={annCategories.includes(cat)} onChange={() => toggleCategory(cat)} />
                {cat.replace(/_/g, " ")}
              </label>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", gap: 20, marginBottom: 10 }}>
          <label>Importance: <input type="number" min={1} max={5} value={annImportance} onChange={e => setAnnImportance(parseInt(e.target.value) || 3)} style={{ width: 50 }} /></label>
          <label>Confidence: <input type="number" min={1} max={5} value={annConfidence} onChange={e => setAnnConfidence(parseInt(e.target.value) || 3)} style={{ width: 50 }} /></label>
        </div>
        <textarea placeholder="Notes" value={annNotes} onChange={e => setAnnNotes(e.target.value)} style={{ width: "100%", marginBottom: 10 }} rows={2} />
        <button onClick={saveAnnotation} disabled={saving}>{saving ? "Saving..." : "Save Annotation"}</button>
      </div>

      {/* Existing Annotations */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Existing Annotations ({annotations.length})</h2>
        {annotations.map((a, i) => (
          <div key={i} style={{ padding: 8, borderBottom: "1px solid #333", cursor: "pointer" }}
            onClick={() => { setAnnStart(a.start); setAnnEnd(a.end); seekTo(a.start); }}>
            <strong>{a.title || "Untitled"}</strong> [{formatTime(a.start)} - {formatTime(a.end)}]
            <span style={{ marginLeft: 10, fontSize: 12, color: "#888" }}>
              {a.categories?.join(", ")} | imp={a.importance} conf={a.confidence}
            </span>
          </div>
        ))}
      </div>

      {/* Candidate Review */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Candidate Review</h2>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
          {candidates.length === 0 && <span style={{ color: "#888" }}>No candidates loaded. Run analysis first.</span>}
          {candidates.map((c, i) => (
            <button key={i} onClick={() => previewCandidate(c)}
              style={{ padding: 8, background: activeCandidate === c ? "#333" : "#222", border: "1px solid #555", borderRadius: 4 }}>
              #{i+1} [{formatTime(c.start)}-{formatTime(c.end)}]
            </button>
          ))}
        </div>
        {activeCandidate && (
          <div style={{ padding: 10, background: "#1a1a1a", borderRadius: 4 }}>
            <div><strong>Active:</strong> {activeCandidate.title} [{formatTime(activeCandidate.start)} - {formatTime(activeCandidate.end)}]</div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 10 }}>
              {["opening_hook", "context_completeness", "interesting_moment", "ending_payoff", "audio_clarity", "visual_quality", "overall_usefulness"].map(field => (
                <label key={field} style={{ fontSize: 12 }}>
                  {field.replace(/_/g, " ")}:
                  <select value={reviewRatings[field as keyof typeof reviewRatings]} onChange={e => setReviewRatings(prev => ({ ...prev, [field]: parseInt(e.target.value) }))}>
                    <option value={0}>-</option>
                    {[1,2,3,4,5].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </label>
              ))}
            </div>
            <div style={{ marginTop: 10 }}>
              <strong>Label:</strong>
              {LABELS.map(l => (
                <label key={l} style={{ marginRight: 10, fontSize: 12 }}>
                  <input type="radio" name="label" value={l} checked={reviewLabel === l} onChange={() => setReviewLabel(l)} /> {l}
                </label>
              ))}
            </div>
            <div style={{ marginTop: 10 }}>
              <strong>Rejection reasons:</strong>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 5 }}>
                {REJECTION_REASONS.map(r => (
                  <label key={r} style={{ fontSize: 11 }}>
                    <input type="checkbox" checked={reviewReasons.includes(r)} onChange={() => toggleReason(r)} />
                    {r.replace(/_/g, " ")}
                  </label>
                ))}
              </div>
            </div>
            <textarea placeholder="Notes" value={reviewNotes} onChange={e => setReviewNotes(e.target.value)} style={{ width: "100%", marginTop: 10 }} rows={2} />
            <button onClick={saveReview} disabled={saving} style={{ marginTop: 10 }}>{saving ? "Saving..." : "Save Review"}</button>
          </div>
        )}
      </div>

      {/* Existing Reviews */}
      <div style={{ border: "1px solid #444", padding: 15, borderRadius: 8 }}>
        <h2>Existing Reviews ({reviews.length})</h2>
        {reviews.map((r, i) => (
          <div key={i} style={{ padding: 8, borderBottom: "1px solid #333", fontSize: 12 }}>
            <strong>{r.candidate_id}</strong> by {r.reviewer_id}: {r.label || "unlabeled"}
            {r.overall_usefulness > 0 && <span> (usefulness={r.overall_usefulness})</span>}
            {r.notes && <div style={{ color: "#888" }}>{r.notes}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}
