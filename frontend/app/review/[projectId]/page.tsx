"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getApiBaseUrl, getCandidates, getReviews, reviewClip, retryProject, runABExperiment, makeClipFromCandidate, getAbExperiments } from "@/lib/api";
import { Candidate, Review, ABExperimentRun } from "@/lib/types";

const REJECT_REASONS = [
  "Weak hook",
  "No payoff",
  "Too much setup",
  "Bad framing",
  "Poor pacing",
  "Redundant",
  "Not interesting",
  "Other",
];

export default function ReviewPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;
  const [project, setProject] = useState<any | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loading, setLoading] = useState(true);
  const [ab, setAb] = useState<any | null>(null);
  const [abHistory, setAbHistory] = useState<ABExperimentRun[]>([]);
  const [abLoading, setAbLoading] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [cutting, setCutting] = useState<Record<string, string>>({});

  const api = getApiBaseUrl();

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [proj, cands, revs, abRuns] = await Promise.all([
        fetch(`${api}/api/projects/${projectId}`).then((r) => r.json()),
        getCandidates(projectId),
        getReviews(projectId),
        getAbExperiments(projectId),
      ]);
      setProject(proj);
      setCandidates(cands);
      setReviews(revs);
      setAbHistory(abRuns);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, [api, projectId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleReview = async (cand: Candidate, decision: "KEEP" | "EDIT" | "REJECT", reason?: string) => {
    try {
      await reviewClip(cand.id, decision, reason, notes[cand.id] || undefined);
      const revs = await getReviews(projectId);
      setReviews(revs);
      if (decision === "REJECT") {
        setCandidates((prev) => prev.filter((c) => c.id !== cand.id));
      }
    } catch (e: any) {
      alert(e.message || "Failed to save review");
    }
  };

  const runAB = async () => {
    setAbLoading(true);
    setAb(null);
    try {
      const res = await runABExperiment(projectId);
      setAb(res);
    } catch (e: any) {
      alert(e.message || "A/B failed — no candidates to compare");
    } finally {
      setAbLoading(false);
    }
  };

  const cutCandidate = async (cand: Candidate) => {
    if (cutting[cand.id]) return;
    setCutting((c) => ({ ...c, [cand.id]: "queued" }));
    try {
      const { job_id, clip_id } = await makeClipFromCandidate(projectId, cand.id);
      const pollClip = (cid: string) =>
        fetch(`${api}/api/clips/${cid}/render-job`)
          .then((r) => r.json())
          .then(async (j: any) => {
            if (j.status === "done" || j.status === "error") {
              setCutting((c) => ({ ...c, [cand.id]: j.status }));
              if (j.status === "done") await loadData();
            } else {
              setCutting((c) => ({ ...c, [cand.id]: `${j.status} ${Math.round((j.percent ?? 0) * 1)}%` }));
              setTimeout(() => pollClip(cid), 3000);
            }
          })
          .catch(() => setTimeout(() => pollClip(cid), 3000));
      if (clip_id && job_id) pollClip(clip_id);
    } catch (e: any) {
      alert(e.message || "Failed to cut clip");
      setCutting((c) => ({ ...c, [cand.id]: "" }));
    }
  };

  // A/B → new experiment / history / candidate cards

  if (loading) return <div className="p-8 text-slate-400">Loading review…</div>;
  if (!project) return <div className="p-8 text-red-400">Project not found</div>;

  return (
    <div className="min-h-screen bg-[#0a0a0f] text-white flex flex-col">
      {/* Top bar */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/10 bg-black/40">
        <div className="flex items-center gap-2">
          <Link href={`/project/${projectId}`} className="text-sm text-slate-400 hover:text-white">← Back</Link>
          <span className="text-sm font-bold">{project.title}</span>
          <span className="text-xs text-slate-500">({candidates.length} candidates)</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={runAB}
            disabled={abLoading}
            className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-[11px] font-bold"
          >
            {abLoading ? "Comparing…" : "A/B Experiment"}
          </button>
          <Link
            href={`/project/${projectId}`}
            className="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-sm font-bold"
          >
            Back to Project
          </Link>
        </div>
      </div>

      <div className="flex flex-1 min-h-0 p-4 gap-4">
        {/* Left: Candidates */}
        <div className="flex-1 flex flex-col space-y-4 min-w-0">
          {candidates.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-500">
              <p className="text-sm font-bold">No candidates yet</p>
              <p className="text-xs text-slate-500">Run the pipeline first</p>
              <button
                onClick={() => retryProject(projectId).then(() => window.location.reload())}
                className="mt-3 px-4 py-2 rounded-lg bg-primary hover:bg-primary/90 text-white text-sm font-bold"
              >
                Run Pipeline
              </button>
            </div>
          ) : (
            <div className="space-y-3 overflow-y-auto" style={{ maxHeight: "calc(100vh - 180px)" }}>
              {candidates.map((cand) => (
                <div
                  key={cand.id}
                  className="glass rounded-2xl p-4 flex gap-4 transition-all border border-transparent hover:border-white/10"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-2 mb-2">
                      <div className="min-w-0">
                        <p className="text-sm font-bold text-white truncate">
                          {cand.first_sentence || cand.first_3s_transcript?.slice(0, 70) || "Untitled"}
                        </p>
                        <p className="text-xs text-slate-500">
                          {cand.start_time?.toFixed(1)}s – {cand.end_time?.toFixed(1)}s · {cand.duration?.toFixed(1)}s
                        </p>
                      </div>
                      <div className="flex items-center gap-1.5 text-[10px] font-bold text-slate-500 shrink-0">
                        <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5">{(cand.final_score ?? 0).toFixed(1)}</span>
                        <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5 text-[9px]">Hook {(cand.hook_first_3s ?? cand.hook_score ?? 0).toFixed(1)}</span>
                        <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5 text-[9px]">Payoff {(cand.payoff_score ?? 0).toFixed(1)}</span>
                        <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5 text-[9px]">Aud {(cand.audio_score ?? 0).toFixed(1)}</span>
                        <span className="rounded bg-white/5 border border-white/10 px-1.5 py-0.5 text-[9px]">Vis {(cand.visual_score ?? 0).toFixed(1)}</span>
                      </div>
                    </div>

                    {Array.isArray(cand.failure_reasons) && cand.failure_reasons.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {cand.failure_reasons.map((r: string) => (
                          <span key={r} className="text-[9px] px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/30">
                            {r}
                          </span>
                        ))}
                      </div>
                    )}

                    <div className="mt-3 flex items-center gap-2">
                      <a
                        href={`/editor/${cand.id}`}
                        className="flex-1 text-center py-1.5 rounded-lg bg-primary hover:bg-primary/90 text-white text-sm font-bold"
                      >
                        {cutting[cand.id] ? `CUTTING: ${cutting[cand.id]}` : "OPEN IN EDITOR"}
                      </a>
                      <button
                        onClick={() => cutCandidate(cand)}
                        disabled={!!cutting[cand.id]}
                        className="px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-700 disabled:opacity-50 text-white text-[10px] font-bold"
                      >
                        CUT CLIP
                      </button>
                      <div className="flex gap-1">
                        <button
                          onClick={() => handleReview(cand, "KEEP")}
                          className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-[10px] font-bold"
                        >
                          KEEP
                        </button>
                        <button
                          onClick={() => handleReview(cand, "EDIT")}
                          className="px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-700 text-white text-[10px] font-bold"
                        >
                          EDIT
                        </button>
                        <button
                          onClick={() => setConfirming(confirming === cand.id ? null : cand.id)}
                          className="px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-700 text-white text-[10px] font-bold"
                        >
                          REJECT
                        </button>
                      </div>
                    </div>

                    {confirming === cand.id && (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        <input
                          value={notes[cand.id] || ""}
                          onChange={(e) => setNotes((n) => ({ ...n, [cand.id]: e.target.value }))}
                          placeholder="Note (optional)"
                          className="w-full max-w-[180px] px-2 py-1 text-[10px] rounded bg-white/5 border border-white/10 text-white placeholder:text-slate-600"
                        />
                        {REJECT_REASONS.map((r) => (
                          <button
                            key={r}
                            onClick={() => handleReview(cand, "REJECT", r)}
                            className="px-2 py-1 text-[9px] rounded bg-red-500/20 hover:bg-red-500/40 text-red-300 border border-red-500/30"
                          >
                            {r}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right: Review History + A/B */}
        <div className="w-[340px] shrink-0 border-l border-white/10 bg-[#111117] p-3 space-y-4 overflow-y-auto" style={{ maxHeight: "calc(100vh - 120px)" }}>
          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Review History ({reviews.length})</h3>
            {reviews.length === 0 ? (
              <p className="text-xs text-slate-500 text-center py-4">No reviews yet</p>
            ) : (
              <div className="space-y-2">
                {reviews.map((rev) => (
                  <div key={rev.id} className="glass rounded-xl p-3 text-xs">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-bold text-white truncate">{rev.clip_id?.slice(0, 8)}…</span>
                      <span
                        className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                          rev.decision === "KEEP" ? "bg-emerald-600 text-white" : rev.decision === "EDIT" ? "bg-violet-600 text-white" : "bg-red-600 text-white"
                        }`}
                      >
                        {rev.decision}
                      </span>
                    </div>
                    {rev.reason && <p className="text-[10px] text-slate-400">{rev.reason}</p>}
                    {rev.notes && <p className="text-[10px] text-slate-500">{rev.notes}</p>}
                    <p className="text-[9px] text-slate-500 mt-1">{new Date(rev.created_at).toLocaleString()}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          {ab && (
            <div>
              <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">
                A/B — current scorer vs editorial rerank
                <span className="ml-2 text-[9px] bg-white/5 border border-white/10 px-1.5 py-0.5 rounded">{ab.overlap} overlap</span>
              </h3>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <p className="text-[9px] text-slate-500 mb-1 uppercase">A · Current</p>
                  <div className="space-y-1.5">
                    {ab.A_current.map((c: any, i: number) => (
                      <div key={i} className="glass rounded p-2 text-[9px] text-slate-300">
                        <div className="font-bold text-white">{c.final_score.toFixed(1)}</div>
                        <div>{c.start.toFixed(1)}–{c.end.toFixed(1)}s · {c.first_3s}…</div>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="text-[9px] text-slate-500 mb-1 uppercase">B · New</p>
                  <div className="space-y-1.5">
                    {ab.B_new.map((c: any, i: number) => (
                      <div key={i} className="glass rounded p-2 text-[9px] text-slate-300">
                        <div className="font-bold text-white">{c.final_score.toFixed(1)}</div>
                        <div>{c.start.toFixed(1)}–{c.end.toFixed(1)}s · {c.first_3s}…</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}

          {abHistory.length > 0 && (
            <div>
              <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Past A/B Runs ({abHistory.length})</h3>
              <div className="space-y-2">
                {abHistory.map((run) => (
                  <div key={run.id} className="glass rounded-xl p-2.5 text-[10px] text-slate-400">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-slate-300">{new Date(run.created_at).toLocaleString()}</span>
                      <span className="text-[9px] bg-white/5 border border-white/10 px-1.5 py-0.5 rounded">
                        {run.payload?.overlap} overlap · A {run.payload?.A_current?.length} / B {run.payload?.B_new?.length}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}