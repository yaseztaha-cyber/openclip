"use client";

import { useEffect, useState, useCallback } from "react";
import { getApiBaseUrl } from "@/lib/api";

interface BenchmarkVideo {
  video_id: string;
  source_hash: string;
  duration: number;
  resolution: string;
  fps: number;
  has_audio: boolean;
  permission_status: string;
  media_type: string;
  content_category?: string;
  dataset_version: string;
  added_at: string;
  partition?: string;
}

interface DatasetInfo {
  version: string;
  video_count: number;
  created_at: string;
}

export default function BenchmarkPage() {
  const api = getApiBaseUrl();
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [videos, setVideos] = useState<BenchmarkVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [permissionStatus, setPermissionStatus] = useState("pending");
  const [contentCategory, setContentCategory] = useState("other");
  const [showImport, setShowImport] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [dsRes, vidRes] = await Promise.all([
        fetch(`${api}/api/benchmark/datasets`).then(r => r.json()),
        fetch(`${api}/api/benchmark/videos`).then(r => r.json()),
      ]);
      setDatasets(dsRes.datasets || []);
      setVideos(vidRes.videos || []);
    } catch (e) {
      console.error(e);
      setMessage("Failed to load data");
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleImport = async () => {
    if (!selectedFile) { setMessage("No file selected"); return; }
    if (permissionStatus !== "authorized") {
      setMessage("You must confirm authorization before importing");
      return;
    }
    setUploading(true);
    setMessage("Uploading...");
    try {
      const formData = new FormData();
      formData.append("file", selectedFile);
      formData.append("video_id", "vid_" + Date.now().toString(36));
      formData.append("permission_status", permissionStatus);
      formData.append("permission_basis", "User self-attestation via web interface");
      formData.append("content_category", contentCategory);
      formData.append("media_type", "real");

      const res = await fetch(`${api}/api/benchmark/import`, { method: "POST", body: formData });
      const data = await res.json();
      if (data.success) {
        if (data.duplicate) {
          setMessage("Duplicate detected: " + data.existing_video_id);
        } else {
          setMessage("Imported: " + data.video_id + " (" + data.duration + "s, " + data.resolution + ")");
        }
        setSelectedFile(null);
        loadData();
      } else {
        setMessage("Import failed: " + (data.detail || "unknown error"));
      }
    } catch (e) {
      setMessage("Error: " + String(e));
    } finally {
      setUploading(false);
    }
  };

  const authorized = videos.filter(v => v.permission_status === "authorized");
  const pending = videos.filter(v => v.permission_status === "pending");
  const synthetic = videos.filter(v => v.media_type === "synthetic");

  return (
    <div style={{ padding: 20, maxWidth: 1200, margin: "0 auto" }}>
      <h1>Benchmark Datasets</h1>
      {message && <div style={{ padding: 10, background: "#222", color: "#0f0", marginBottom: 10, borderRadius: 4 }}>{message}</div>}

      {/* Stats */}
      <div style={{ display: "flex", gap: 20, marginBottom: 20, flexWrap: "wrap" }}>
        <div style={{ padding: 15, border: "1px solid #444", borderRadius: 8, minWidth: 120 }}>
          <div style={{ fontSize: 24, fontWeight: "bold" }}>{videos.length}</div>
          <div style={{ fontSize: 12, color: "#888" }}>Total Videos</div>
        </div>
        <div style={{ padding: 15, border: "1px solid #444", borderRadius: 8, minWidth: 120 }}>
          <div style={{ fontSize: 24, fontWeight: "bold" }}>{authorized.length}</div>
          <div style={{ fontSize: 12, color: "#888" }}>Authorized</div>
        </div>
        <div style={{ padding: 15, border: "1px solid #444", borderRadius: 8, minWidth: 120 }}>
          <div style={{ fontSize: 24, fontWeight: "bold" }}>{pending.length}</div>
          <div style={{ fontSize: 12, color: "#888" }}>Pending</div>
        </div>
        <div style={{ padding: 15, border: "1px solid #444", borderRadius: 8, minWidth: 120 }}>
          <div style={{ fontSize: 24, fontWeight: "bold" }}>{synthetic.length}</div>
          <div style={{ fontSize: 12, color: "#888" }}>Synthetic</div>
        </div>
        <div style={{ padding: 15, border: "1px solid #444", borderRadius: 8, minWidth: 120 }}>
          <div style={{ fontSize: 24, fontWeight: "bold" }}>{datasets.length}</div>
          <div style={{ fontSize: 12, color: "#888" }}>Datasets</div>
        </div>
      </div>

      {/* Import Section */}
      <div style={{ border: "1px solid #444", padding: 15, marginBottom: 20, borderRadius: 8 }}>
        <h2>Import Video</h2>
        <div style={{ marginBottom: 10 }}>
          <input type="file" accept="video/*" onChange={e => setSelectedFile(e.target.files?.[0] || null)} />
          {selectedFile && <span style={{ marginLeft: 10, fontSize: 12 }}>{selectedFile.name} ({(selectedFile.size / 1024 / 1024).toFixed(1)} MB)</span>}
        </div>
        <div style={{ marginBottom: 10 }}>
          <label>Content Category: </label>
          <select value={contentCategory} onChange={e => setContentCategory(e.target.value)}>
            <option value="interview">Interview</option>
            <option value="podcast">Podcast</option>
            <option value="education">Education</option>
            <option value="presentation">Presentation</option>
            <option value="tutorial">Tutorial</option>
            <option value="commentary">Commentary</option>
            <option value="demonstration">Demonstration</option>
            <option value="conversation">Conversation</option>
            <option value="storytelling">Storytelling</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div style={{ marginBottom: 10 }}>
          <label>
            <input type="checkbox" checked={permissionStatus === "authorized"} onChange={e => setPermissionStatus(e.target.checked ? "authorized" : "pending")} />
            I confirm that I own this video or have permission to use it for benchmark analysis (self-attestation, not legal verification)
          </label>
        </div>
        <button onClick={handleImport} disabled={uploading || !selectedFile}>
          {uploading ? "Importing..." : "Import Video"}
        </button>
      </div>

      {/* Video List */}
      <div style={{ border: "1px solid #444", padding: 15, borderRadius: 8 }}>
        <h2>Videos ({videos.length})</h2>
        {loading && <div>Loading...</div>}
        {!loading && videos.length === 0 && <div style={{ color: "#888" }}>No videos imported yet. Use the import form above.</div>}
        {videos.map((v, i) => (
          <div key={i} style={{ padding: 10, borderBottom: "1px solid #333", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <strong>{v.video_id}</strong>
              <span style={{ marginLeft: 10, fontSize: 12, color: "#888" }}>
                {v.duration ? v.duration.toFixed(1) + "s" : "?"} | {v.resolution || "?"} | {v.has_audio ? "audio" : "no audio"} | {v.media_type}
              </span>
              <div style={{ fontSize: 11, color: "#666" }}>
                hash: {v.source_hash?.substring(0, 12)}... | perm: {v.permission_status} | partition: {v.partition || "unassigned"}
              </div>
            </div>
            <div style={{ display: "flex", gap: 5 }}>
              <a href={`/benchmark/${v.video_id}`} style={{ padding: "4px 8px", background: "#333", borderRadius: 4, fontSize: 12, textDecoration: "none", color: "#fff" }}>Annotate</a>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
