"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api";

export default function AudioCleanupPage() {
  const api = getApiBaseUrl();
  const [videoPath, setVideoPath] = useState("");
  const [outputPath, setOutputPath] = useState("");
  const [status, setStatus] = useState<any>(null);
  const [modelStatus, setModelStatus] = useState<any>(null);
  const [processing, setProcessing] = useState(false);
  const [message, setMessage] = useState("");
  const [mode, setMode] = useState("mute");

  useEffect(() => {
    fetch(`${api}/api/audio-cleanup/model-status`)
      .then(r => r.json())
      .then(setModelStatus)
      .catch(() => setMessage("Failed to load model status"));
  }, [api]);

  const inspect = async () => {
    if (!videoPath) { setMessage("Enter video path"); return; }
    try {
      const res = await fetch(`${api}/api/audio-cleanup/inspect?video_path=${encodeURIComponent(videoPath)}`, { method: "POST" });
      const data = await res.json();
      setStatus(data);
      setMessage("Inspection complete");
    } catch (e) {
      setMessage("Error: " + String(e));
    }
  };

  const processMute = async () => {
    if (!videoPath || !outputPath) { setMessage("Enter both paths"); return; }
    setProcessing(true);
    setMessage("Processing...");
    try {
      const res = await fetch(`${api}/api/audio-cleanup/mute?video_path=${encodeURIComponent(videoPath)}&output_path=${encodeURIComponent(outputPath)}`, { method: "POST" });
      const data = await res.json();
      setStatus(data);
      setMessage(data.status === "verified_silent" ? "Verified silent - ready to publish" : "Failed: " + (data.error || "unknown"));
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setProcessing(false);
    }
  };

  const processRemoveMusic = async () => {
    if (!videoPath || !outputPath) { setMessage("Enter both paths"); return; }
    setProcessing(true);
    setMessage("Processing...");
    try {
      const res = await fetch(`${api}/api/audio-cleanup/remove-music?video_path=${encodeURIComponent(videoPath)}&output_path=${encodeURIComponent(outputPath)}`, { method: "POST" });
      const data = await res.json();
      setStatus(data);
      setMessage(data.status === "needs_review" ? "Needs review - check result" : "Failed: " + (data.error || "unknown"));
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setProcessing(false);
    }
  };

  const processReview = async () => {
    if (!videoPath) { setMessage("Enter video path"); return; }
    setProcessing(true);
    try {
      const res = await fetch(`${api}/api/audio-cleanup/review?video_path=${encodeURIComponent(videoPath)}`, { method: "POST" });
      const data = await res.json();
      setStatus(data);
      setMessage("Review complete");
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setProcessing(false);
    }
  };

  return (
    <div style={{ padding: 20, maxWidth: 900, margin: "0 auto" }}>
      <h1>Audio Cleanup</h1>
      {message && <div style={{ padding: 10, background: "#222", color: "#0f0", marginBottom: 10, borderRadius: 4 }}>{message}</div>}

      {/* Model Status */}
      {modelStatus && (
        <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
          <h2>Source Separation Model</h2>
          <div>Available: {modelStatus.available ? "Yes" : "No"}</div>
          <div>Name: {modelStatus.name}</div>
          <div>Status: {modelStatus.status}</div>
          <div style={{ fontSize: 12, color: "#888", marginTop: 5 }}>{modelStatus.message}</div>
        </div>
      )}

      {/* Input */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Video Paths</h2>
        <div style={{ marginBottom: 10 }}>
          <label>Input Video: <input type="text" value={videoPath} onChange={e => setVideoPath(e.target.value)} style={{ width: "100%" }} placeholder="Path to video file" /></label>
        </div>
        <div style={{ marginBottom: 10 }}>
          <label>Output Video: <input type="text" value={outputPath} onChange={e => setOutputPath(e.target.value)} style={{ width: "100%" }} placeholder="Path for cleaned output" /></label>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button onClick={inspect} disabled={processing}>Inspect</button>
          <button onClick={processReview} disabled={processing}>Review Only</button>
        </div>
      </div>

      {/* Mode Selection */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Cleanup Mode</h2>
        <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
          <label><input type="radio" name="mode" value="mute" checked={mode === "mute"} onChange={() => setMode("mute")} /> Mute All Audio (guaranteed music-free)</label>
          <label><input type="radio" name="mode" value="remove" checked={mode === "remove"} onChange={() => setMode("remove")} /> Remove Music (experimental, may need review)</label>
        </div>
        {mode === "mute" && (
          <div>
            <p style={{ fontSize: 12, color: "#888" }}>Removes all audio streams. Guarantees no music but also removes dialogue and sound effects.</p>
            <button onClick={processMute} disabled={processing}>{processing ? "Processing..." : "Mute All Audio"}</button>
          </div>
        )}
        {mode === "remove" && (
          <div>
            <p style={{ fontSize: 12, color: "#888" }}>Attempts to remove background music while preserving speech. Requires source separation model (not available on this hardware). Result will need manual review.</p>
            <button onClick={processRemoveMusic} disabled={processing}>{processing ? "Processing..." : "Remove Music (Experimental)"}</button>
          </div>
        )}
      </div>

      {/* Status */}
      {status && (
        <div style={{ border: "1px solid #444", padding: 15, borderRadius: 8 }}>
          <h2>Status</h2>
          <div><strong>Status:</strong> {status.status}</div>
          {status.mode && <div><strong>Mode:</strong> {status.mode}</div>}
          {status.processing_time !== undefined && <div><strong>Processing time:</strong> {status.processing_time}s</div>}
          {status.error && <div style={{ color: "red" }}><strong>Error:</strong> {status.error}</div>}
          {status.verification_report && (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <strong>Verification Report:</strong>
              <pre style={{ background: "#111", padding: 10, borderRadius: 4, overflow: "auto" }}>
                {JSON.stringify(status.verification_report, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
