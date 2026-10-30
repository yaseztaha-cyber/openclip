"use client";

import { useEffect, useState } from "react";
import {
    getAutomationPolicy, saveAutomationPolicy, scanStoredCampaigns,
    getAutomationCampaigns, reviewAutomationCampaign, getAutomationAudit,
    getApprovals, decideApproval, automationPublish, getPublications,
    getAnalyticsSummary, generateWeeklyReport, getWeeklyReports,
    reEvaluatePolicy, getTransitions, getUnresolvedSubmissions,
    reconcileSubmission, resolveSubmission, getProviderCapabilities,
    getAutomationHealth, getAutomationOverview, getSchedulerState,
    schedulerJobAction, getTelemetry, getAlerts, getReadiness,
    retentionCleanup, providerCheck, publishPreflight, getQualifications,
    smtpVerify, getAutomationMeta, getConsistency, getDiagnostics,
    createCampaign, deleteCampaign, importClipApproval,
    getProjects, getProject,
} from "@/lib/api";

type Campaign = { campaign_id: string; name: string; status: string; reasons_json?: string; score?: number | null; payload_json?: string };

function parseJson(s?: string, fb: any = null) {
    try { return s ? JSON.parse(s) : fb; } catch { return fb; }
}

function parseErr(e: any): string {
    try {
        const j = JSON.parse(e.message || "");
        if (j?.error) return `${j.error.code}: ${j.error.message}`;
    } catch { /* not an envelope */ }
    return e.message || "request failed";
}

export default function AutomationPage() {
    const [tab, setTab] = useState<"ELIGIBLE" | "NEEDS_REVIEW" | "REJECTED">("ELIGIBLE");
    const [campaigns, setCampaigns] = useState<Campaign[]>([]);
    const [policy, setPolicy] = useState<any>(null);
    const [audit, setAudit] = useState<any[]>([]);
    const [approvals, setApprovals] = useState<any[]>([]);
    const [pubs, setPubs] = useState<any[]>([]);
    const [summary, setSummary] = useState<any>(null);
    const [reports, setReports] = useState<any[]>([]);
    const [loading, setLoading] = useState(false);
    const [msg, setMsg] = useState("");
    const [expanded, setExpanded] = useState<string | null>(null);
    const [policyMeta, setPolicyMeta] = useState<any>(null);
    const [transitions, setTransitions] = useState<any[]>([]);
    const [unresolved, setUnresolved] = useState<any[]>([]);
    const [caps, setCaps] = useState<any>(null);
    const [health, setHealth] = useState<any>(null);
    const [overview, setOverview] = useState<any>(null);
    const [sched, setSched] = useState<any>(null);
    const [alerts, setAlerts] = useState<any[]>([]);
    const [telemetry, setTelemetry] = useState<any>(null);
    const [readiness, setReadiness] = useState<any>(null);
    const [cleanup, setCleanup] = useState<any>(null);
    const [meta, setMeta] = useState<any>(null);
    const [consistency, setConsistency] = useState<any>(null);
    const [diagnostics, setDiagnostics] = useState<any>(null);

    const refresh = async () => {
        try {
            const [all, pol, ap, pb, an, rep, ad] = await Promise.all([
                getAutomationCampaigns(), getAutomationPolicy(), getApprovals(),
                getPublications(), getAnalyticsSummary().catch(() => null),
                getWeeklyReports(), getAutomationAudit(),
            ]);
            setCampaigns(all); setPolicy(pol?.policy ?? pol); setApprovals(ap);
            setPubs(pb); setSummary(an); setReports(rep); setAudit(ad.slice(0, 100));
            setPolicyMeta({ version: pol?.version ?? null, hash: pol?.policy_hash ?? null });
            try { setTransitions(await getTransitions()); } catch { /* optional */ }
            try { setUnresolved(await getUnresolvedSubmissions()); } catch { /* optional */ }
            try { setCaps(await getProviderCapabilities()); } catch { /* optional */ }
            try { setHealth(await getAutomationHealth()); } catch { /* optional */ }
            try { setOverview(await getAutomationOverview()); } catch { /* optional */ }
            try { setSched(await getSchedulerState()); } catch { /* optional */ }
            try { setAlerts(await getAlerts()); } catch { /* optional */ }
            try { setTelemetry(await getTelemetry()); } catch { /* optional */ }
            try { setReadiness(await getReadiness()); } catch { /* optional */ }
            try { setMeta(await getAutomationMeta()); } catch { /* optional */ }
            try { setConsistency(await getConsistency()); } catch { /* optional */ }
            try { setDiagnostics(await getDiagnostics()); } catch { /* optional */ }
        } catch (e: any) { setMsg(e.message || "load failed"); }
    };

    useEffect(() => { refresh(); }, []);

    const runScan = async () => {
        setLoading(true); setMsg("");
        try {
            const r = await scanStoredCampaigns();
            await refresh();
            const n = (r.campaigns || []).length;
            setMsg(`Scan complete: ${n} stored campaign(s) evaluated.`);
        } catch (e: any) { setMsg(parseErr(e)); }
        setLoading(false);
    };

    const [newCamp, setNewCamp] = useState({ name: "", description: "", platforms: "tiktok", content_sources: "" });
    const [projects, setProjects] = useState<any[]>([]);
    const [importPid, setImportPid] = useState("");
    const [importClips, setImportClips] = useState<any[]>([]);

    const addCampaign = async () => {
        if (!newCamp.name.trim()) { setMsg("Campaign name required"); return; }
        try {
            await createCampaign({
                name: newCamp.name.trim(),
                description: newCamp.description,
                platforms: newCamp.platforms.split(",").map((s) => s.trim()).filter(Boolean),
                content_sources: newCamp.content_sources.split(",").map((s) => s.trim()).filter(Boolean),
            });
            setNewCamp({ name: "", description: "", platforms: "tiktok", content_sources: "" });
            await refresh();
            setMsg("Campaign added. Run Scan to evaluate it.");
        } catch (e: any) { setMsg(parseErr(e)); }
    };

    const loadProjects = async () => {
        try { setProjects(await getProjects()); }
        catch (e: any) { setMsg(parseErr(e)); }
    };

    const loadImportClips = async (pid: string) => {
        setImportPid(pid);
        if (!pid) { setImportClips([]); return; }
        try {
            const det = await getProject(pid);
            setImportClips(det.clips || []);
        } catch (e: any) { setMsg(parseErr(e)); }
    };

    const review = async (id: string, decision: "approve" | "reject") => {
        try {
            await reviewAutomationCampaign(id, decision, "owner");
            await refresh();
        } catch (e: any) { setMsg(parseErr(e)); }
    };

    const savePolicy = async () => {
        try {
            await saveAutomationPolicy(policy);
            setMsg("Policy saved.");
        } catch (e: any) { setMsg(e.message || "policy save failed"); }
    };

    const filtered = campaigns.filter((c) => c.status === tab);
    const counts = {
        ELIGIBLE: campaigns.filter((c) => c.status === "ELIGIBLE").length,
        NEEDS_REVIEW: campaigns.filter((c) => c.status === "NEEDS_REVIEW").length,
        REJECTED: campaigns.filter((c) => c.status === "REJECTED").length,
    };

    return (
        <div className="mx-auto max-w-7xl px-6 py-8 text-white">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold">Automation Hub</h1>
                    <p className="text-sm text-slate-400">Discover → Filter → Analyze → Select → Edit → Review → Publish → Track → Report</p>
                    {meta && <p className="text-[11px] text-slate-500">v{meta.application_version} · schema v{meta.schema_version} · policy v{meta.policy_version ?? "—"} · {meta.mode} · auth {meta.auth_mode}</p>}
                </div>
                <button onClick={runScan} disabled={loading}
                    className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-bold hover:bg-violet-500 disabled:opacity-40">
                    {loading ? "Scanning…" : "Scan campaigns"}
                </button>
            </div>
            {diagnostics && (
                <div className="mt-2 rounded-lg bg-white/5 p-3 text-xs">
                    <div className="font-bold">Diagnostics</div>
                    <div className="text-slate-400">
                        DB {diagnostics.database?.state} · FFmpeg {diagnostics.ffmpeg?.state} ·
                        disk {diagnostics.storage?.state} ({Math.round((diagnostics.storage?.disk_free_bytes ?? -1) / 1048576)}MB free) ·
                        unresolved {diagnostics.unresolved_submissions} · consistency {diagnostics.consistency ? "ok" : "issues"}
                    </div>
                    <div className="text-slate-500">last scan {diagnostics.last_scan || "never"} · last successful job {diagnostics.last_successful_job || "never"}</div>
                </div>
            )}
            {consistency && !consistency.ok && (
                <div className="mt-3 rounded bg-red-600/20 border border-red-500/40 px-3 py-2 text-xs">
                    <b>Data consistency issues ({consistency.errors?.length}):</b>
                    {(consistency.errors || []).slice(0, 5).map((e: any, i: number) => (
                        <div key={i}>{e.code}: {e.message}</div>
                    ))}
                </div>
            )}
            {msg && <div className="mt-3 rounded bg-white/5 px-3 py-2 text-sm text-slate-300">{msg}</div>}
            {health && (
                <div className="mt-3 flex flex-wrap gap-2 text-xs">
                    <span className={`rounded px-2 py-1 ${health.application === "HEALTHY" ? "bg-emerald-600/30" : "bg-amber-600/30"}`}>
                        App: {health.application}
                    </span>
                    {Object.entries(health.dependencies || {}).map(([k, v]: [string, any]) => (
                        <span key={k} className="rounded bg-white/10 px-2 py-1 text-slate-300" title={v.reason || v.detail || ""}>
                            {k}: <b>{v.state}</b>
                        </span>
                    ))}
                </div>
            )}

            {/* Campaign tabs */}
            <div className="mt-6 rounded-lg bg-white/5 p-3">
                <div className="text-sm font-bold">Add campaign (evaluated for real on Scan)</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                    <input value={newCamp.name} onChange={(e) => setNewCamp({ ...newCamp, name: e.target.value })}
                        placeholder="Name (e.g. Gadget launch)" className="rounded bg-white/5 border border-white/10 px-2 py-1 text-white" />
                    <input value={newCamp.platforms} onChange={(e) => setNewCamp({ ...newCamp, platforms: e.target.value })}
                        placeholder="Platforms (comma-separated)" className="rounded bg-white/5 border border-white/10 px-2 py-1 text-white" />
                    <input value={newCamp.description} onChange={(e) => setNewCamp({ ...newCamp, description: e.target.value })}
                        placeholder="Description / requirements" className="col-span-2 rounded bg-white/5 border border-white/10 px-2 py-1 text-white" />
                    <input value={newCamp.content_sources} onChange={(e) => setNewCamp({ ...newCamp, content_sources: e.target.value })}
                        placeholder="Content sources (comma-separated URLs)" className="col-span-2 rounded bg-white/5 border border-white/10 px-2 py-1 text-white" />
                </div>
                <button onClick={addCampaign} className="mt-2 rounded bg-violet-600 px-3 py-1 text-xs font-bold">Add campaign</button>
            </div>
            <div className="mt-4 flex gap-2">
                {(["ELIGIBLE", "NEEDS_REVIEW", "REJECTED"] as const).map((t) => (
                    <button key={t} onClick={() => setTab(t)}
                        className={`rounded-lg px-4 py-2 text-sm font-bold ${tab === t ? "bg-violet-600" : "bg-white/10"}`}>
                        {t === "ELIGIBLE" ? "Eligible" : t === "NEEDS_REVIEW" ? "Needs Review" : "Rejected"} ({counts[t]})
                    </button>
                ))}
            </div>
            <div className="mt-3 space-y-2">
                {filtered.map((c) => {
                    const reasons = parseJson(c.reasons_json, []);
                    const payload = parseJson(c.payload_json, {});
                    return (
                        <div key={c.campaign_id} className="rounded-lg bg-white/5 p-3">
                            <div className="flex items-center justify-between">
                                <div>
                                    <div className="font-bold">{c.name || c.campaign_id}</div>
                                    <div className="text-xs text-slate-400">
                                        {(payload.platforms || []).join(", ")} · score {c.score ?? "—"}
                                    </div>
                                </div>
                                <div className="flex gap-2">
                                    <button onClick={() => setExpanded(expanded === c.campaign_id ? null : c.campaign_id)}
                                        className="rounded bg-white/10 px-2 py-1 text-xs">
                                        {c.status === "REJECTED" ? "Why rejected?" : "Details"}
                                    </button>
                                    {c.status === "NEEDS_REVIEW" && (
                                        <>
                                            <button onClick={() => review(c.campaign_id, "approve")}
                                                className="rounded bg-emerald-600 px-2 py-1 text-xs font-bold">Approve</button>
                                            <button onClick={() => review(c.campaign_id, "reject")}
                                                className="rounded bg-red-600 px-2 py-1 text-xs font-bold">Reject</button>
                                        </>
                                    )}
                                    <button onClick={async () => {
                                        if (!window.confirm(`Delete campaign ${c.name || c.campaign_id}?`)) return;
                                        try { await deleteCampaign(c.campaign_id); await refresh(); }
                                        catch (e: any) { setMsg(parseErr(e)); }
                                    }} className="rounded bg-white/10 px-2 py-1 text-xs text-red-300">Delete</button>
                                </div>
                            </div>
                            {expanded === c.campaign_id && (
                                <div className="mt-2 text-xs text-slate-300">
                                    <div><span className="text-slate-500">Status:</span> {c.status}</div>
                                    <div><span className="text-slate-500">Reasons:</span> {reasons.join(", ") || "—"}</div>
                                    <div className="mt-1"><span className="text-slate-500">Description:</span> {payload.description || "—"}</div>
                                    <div><span className="text-slate-500">Required content:</span> {payload.requirements || payload.allowed_content || "—"}</div>
                                    <div className="mt-1 text-slate-500">Decisions are logged in the audit trail below; rejected campaigns stay visible here.</div>
                                </div>
                            )}
                        </div>
                    );
                })}
                {!filtered.length && <div className="text-sm text-slate-500">No campaigns in this state. Run a scan.</div>}
            </div>

            {/* Pipeline */}
            <h2 className="mt-8 text-lg font-bold">Pipeline</h2>
            <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-4">
                {[
                    ["Awaiting approval", approvals.filter((a) => ["READY_FOR_APPROVAL", "RENDERED", "SELECTED"].includes(a.status)).length],
                    ["Approved", approvals.filter((a) => a.status === "APPROVED").length],
                    ["Published", pubs.length],
                    ["Failed", approvals.filter((a) => a.status === "FAILED").length],
                ].map(([k, v]) => (
                    <div key={k as string} className="rounded-lg bg-white/5 p-3 text-center">
                        <div className="text-xl font-bold">{v}</div>
                        <div className="text-xs text-slate-400">{k}</div>
                    </div>
                ))}
            </div>

            {/* Approvals */}
            {/* Approval queue */}
            <h2 className="mt-8 text-lg font-bold">Import project clips</h2>
            <div className="mt-2 rounded-lg bg-white/5 p-3 text-xs">
                <div className="flex gap-2">
                    <button onClick={loadProjects} className="rounded bg-white/10 px-2 py-1">Load projects</button>
                    <select value={importPid} onChange={(e) => loadImportClips(e.target.value)}
                        className="flex-1 rounded bg-white/5 border border-white/10 px-2 py-1 text-white">
                        <option value="">Select project…</option>
                        {projects.map((p: any) => (
                            <option key={p.id} value={p.id}>{p.title || p.id} ({p.clip_count ?? "?"})</option>
                        ))}
                    </select>
                </div>
                {importClips.map((cl: any) => (
                    <div key={cl.id} className="mt-1 flex items-center justify-between rounded bg-white/5 px-2 py-1">
                        <span>{cl.title || cl.id} · {Number(cl.start_time || 0).toFixed(1)}s→{Number(cl.end_time || 0).toFixed(1)}s</span>
                        <button onClick={async () => {
                            try { await importClipApproval(cl.id); await refresh(); setMsg(`Imported ${cl.title || cl.id} to approval queue`); }
                            catch (e: any) { setMsg(parseErr(e)); }
                        }} className="rounded bg-violet-600 px-2 py-0.5 font-bold">Import</button>
                    </div>
                ))}
            </div>
            <h2 className="mt-8 text-lg font-bold">Approval queue</h2>
            <div className="mt-2 space-y-2">
                {approvals.slice(0, 20).map((a) => {
                    const expired = a.expires_at ? new Date(a.expires_at).getTime() < Date.now() : a.status === "APPROVED";
                    return (
                    <div key={a.id} className="flex items-center justify-between rounded-lg bg-white/5 p-3 text-sm">
                        <div>
                            <span className="font-bold">{a.title || a.clip_id}</span>
                            <span className="ml-2 text-xs text-slate-400">{a.status} · music {parseJson(a.detail_json, {}).music_status || "—"}</span>
                            <div className="text-[11px] text-slate-500">
                                render {a.render_hash ? a.render_hash.slice(0, 12) : "unbound"} · policy v{a.policy_version ?? "—"} · approval v{a.approval_version ?? 1}
                                {a.status === "APPROVED" && (
                                    <span className={expired ? "text-red-400 font-bold" : "text-emerald-400"}>
                                        {" "}· {expired ? "EXPIRED — re-approve" : `expires ${a.expires_at ? new Date(a.expires_at).toLocaleString() : "—"}`}
                                    </span>
                                )}
                                {a.invalidated_reason && <span className="text-red-400"> · invalidated: {a.invalidated_reason}</span>}
                            </div>
                        </div>
                        <div className="flex gap-2">
                            <button onClick={async () => {
                                try { await decideApproval(a.clip_id, true); await refresh(); }
                                catch (e: any) { setMsg(parseErr(e)); }
                            }}
                                className="rounded bg-emerald-600 px-2 py-1 text-xs font-bold">Approve</button>
                            <button onClick={async () => {
                                try { await decideApproval(a.clip_id, false); await refresh(); }
                                catch (e: any) { setMsg(parseErr(e)); }
                            }}
                                className="rounded bg-red-600 px-2 py-1 text-xs font-bold">Reject</button>
                            <button onClick={async () => {
                                try {
                                    const r = await automationPublish({ clip_id: a.clip_id, campaign_id: a.campaign_id, platform: "tiktok", title: a.title, description: a.description, render_hash: a.render_hash, dry_run: true });
                                    setMsg(`Dry run: ${JSON.stringify(r.status)}`);
                                } catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-white/10 px-2 py-1 text-xs">Dry-run publish</button>
                            <button onClick={async () => {
                                try {
                                    const r = await publishPreflight({ clip_id: a.clip_id, campaign_id: a.campaign_id, platform: "tiktok", title: a.title, description: a.description, render_hash: a.render_hash });
                                    setMsg(`Preflight: ${r.verdict} — ${JSON.stringify(r.checks)}`);
                                } catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-white/10 px-2 py-1 text-xs">Preflight</button>
                            <button onClick={async () => {
                                try {
                                    const r = await automationPublish({ clip_id: a.clip_id, campaign_id: a.campaign_id, platform: "tiktok", title: a.title, description: a.description, render_hash: a.render_hash, approval_id: a.id, dry_run: false, render_verified: false });
                                    setMsg(`Live: ${JSON.stringify(r)}`);
                                    await refresh();
                                } catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-amber-600 px-2 py-1 text-xs font-bold" title="Live publish (hard-gated; blocked unless every condition holds)">Publish live</button>
                        </div>
                    </div>
                    );
                })}
                {!approvals.length && <div className="text-sm text-slate-500">Nothing awaiting approval.</div>}
            </div>

            {/* Unresolved submissions */}
            <h2 className="mt-8 text-lg font-bold">Submissions needing reconciliation</h2>
            <div className="mt-2 space-y-2">
                {unresolved.map((p) => (
                    <div key={p.idempotency_key} className="rounded-lg bg-amber-500/10 border border-amber-500/30 p-3 text-sm">
                        <div className="font-bold">{p.clip_id} → {p.platform} · <span className="text-amber-300">{p.submission_state}</span></div>
                        <div className="mt-1 text-xs text-slate-400">
                            campaign {p.campaign_id || "—"} · attempt #{(p.attempts || []).length} · key {p.idempotency_key} · {p.published_at || p.created_at || ""}
                        </div>
                        {(p.attempts || []).length > 0 && (
                            <div className="mt-1 text-[11px] text-slate-500">
                                last attempt: {(p.attempts[p.attempts.length - 1] || {}).result_state || "—"}
                                {" "}· {(p.attempts[p.attempts.length - 1] || {}).error_code || ""}
                            </div>
                        )}
                        <div className="mt-2 flex flex-wrap gap-2">
                            <button onClick={async () => {
                                try { const r = await reconcileSubmission(p.idempotency_key); setMsg(`Reconcile: ${r.outcome} — ${r.reason || ""}`); await refresh(); }
                                catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-white/10 px-2 py-1 text-xs">Reconcile</button>
                            <button onClick={async () => {
                                const url = window.prompt("Provider post URL (evidence required to confirm):");
                                if (!url) return;
                                try { await resolveSubmission(p.idempotency_key, "confirmed", "owner verified provider post", url); await refresh(); }
                                catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-emerald-600 px-2 py-1 text-xs font-bold">Mark Resolved — Confirmed</button>
                            <button onClick={async () => {
                                const reason = window.prompt("Reason (recorded in audit):") || "";
                                try { await resolveSubmission(p.idempotency_key, "not_published", reason); await refresh(); }
                                catch (e: any) { setMsg(parseErr(e)); }
                            }} className="rounded bg-red-600 px-2 py-1 text-xs font-bold">Mark Resolved — Not Published</button>
                        </div>
                    </div>
                ))}
                {!unresolved.length && <div className="text-sm text-slate-500">No unresolved submissions.</div>}
            </div>

            {/* Provider capabilities */}
            <h2 className="mt-8 text-lg font-bold">Provider capabilities</h2>            <div className="mt-2 space-y-1 text-xs">
                {caps ? (
                    <>
                        {Object.entries(caps.publishers || {}).map(([k, v]: [string, any]) => (
                            <div key={k} className="rounded bg-white/5 px-2 py-1 text-slate-300">
                                {k}: <b>{v.state}</b> · {v.reason} · lookup:{String(v.capabilities?.supports_submission_lookup)} idempotency:{String(v.capabilities?.supports_idempotency_keys)}
                            </div>
                        ))}
                        <div className="rounded bg-white/5 px-2 py-1 text-slate-300">
                            whop_official: <b>{caps.campaign_provider?.state}</b> · {caps.campaign_provider?.reason}
                        </div>
                    </>
                ) : <div className="text-slate-500">Loading…</div>}
            </div>

            {/* Analytics */}
            <h2 className="mt-8 text-lg font-bold">Analytics</h2>
            <div className="mt-2 rounded-lg bg-white/5 p-3 text-sm">
                {summary ? (
                    <div className="flex flex-wrap gap-4">
                        <span>Views: <b>{String(summary.total_views)}</b></span>
                        <span>Engagement: <b>{summary.engagement}</b></span>
                        <span>Confirmed: <b>{String(summary.confirmed_earnings)}</b></span>
                        <span>Pending: <b>{String(summary.pending_earnings)}</b></span>
                        <span>Estimated: <b>{String(summary.estimated_earnings)}</b></span>
                    </div>
                ) : <span className="text-slate-500">No analytics yet.</span>}
            </div>

            {/* Reports */}
            <h2 className="mt-8 text-lg font-bold">Weekly reports</h2>
            <button onClick={async () => {
                try { const r = await generateWeeklyReport(); setMsg(`Report ${r.report_id}: ${r.email_status}`); await refresh(); }
                catch (e: any) { setMsg(e.message); }
            }} className="mt-2 rounded bg-white/10 px-3 py-1 text-xs">Generate weekly report</button>
            <div className="mt-2 space-y-1 text-sm">
                {reports.slice(0, 10).map((r) => (
                    <div key={r.report_id} className="rounded bg-white/5 px-3 py-2">
                        {r.week_start}–{r.week_end} · {r.email_status}
                    </div>
                ))}
            </div>

            {/* Policy */}
            <h2 className="mt-8 text-lg font-bold">Content safety</h2>
            <div className="mt-1 text-xs text-slate-400">
                Policy version: <b>{policyMeta?.version ?? "—"}</b>
                {policyMeta?.hash && <span> · {String(policyMeta.hash).slice(0, 12)}</span>}
                <button onClick={async () => {
                    try { const r = await reEvaluatePolicy(); setMsg(`Re-evaluated ${r.results?.length ?? 0} campaigns under v${r.policy_version}`); await refresh(); }
                    catch (e: any) { setMsg(parseErr(e)); }
                }} className="ml-2 rounded bg-white/10 px-2 py-0.5 text-[11px]">Re-evaluate all</button>
            </div>
            {policy ? (
                <div className="mt-2 space-y-2 rounded-lg bg-white/5 p-4 text-sm">
                    {[
                        ["exclude_adult", "Exclude sexual/adult campaigns"],
                        ["exclude_gambling", "Exclude gambling campaigns"],
                        ["reject_background_music", "Reject background music"],
                        ["apply_affiliation_exclusions", "Apply configured affiliation exclusions"],
                    ].map(([k, label]) => (
                        <label key={k} className="flex items-center gap-2">
                            <input type="checkbox" checked={!!policy[k]}
                                onChange={(e) => setPolicy({ ...policy, [k]: e.target.checked })} />
                            {label}
                        </label>
                    ))}
                    <div className="mt-2">
                        <div className="font-bold">User affiliation exclusions</div>
                        <p className="text-xs text-slate-400">Campaign exclusion is based on explicit campaign metadata or configured evidence, not inferred identity or nationality.</p>
                        {(["excluded_affiliations", "excluded_brands", "excluded_entities", "excluded_campaigns"] as const).map((k) => (
                            <label key={k} className="mt-1 block text-xs">
                                <span className="text-slate-500">{k} (comma-separated)</span>
                                <input value={(policy[k] || []).join(", ")}
                                    onChange={(e) => setPolicy({ ...policy, [k]: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
                                    className="mt-1 w-full rounded bg-white/5 border border-white/10 px-2 py-1 text-white" />
                            </label>
                        ))}
                    </div>
                    <div className="mt-2 text-xs text-slate-400">Ambiguous campaign: [{policy.ambiguous_action || "NEEDS_REVIEW"}]</div>
                    <button onClick={savePolicy} className="mt-2 rounded bg-violet-600 px-3 py-1 text-xs font-bold">Save policy</button>
                </div>
            ) : <div className="text-sm text-slate-500">Loading policy…</div>}

            {/* Audit */}
            <h2 className="mt-8 text-lg font-bold">Policy audit log</h2>
            <div className="mt-2 max-h-64 space-y-1 overflow-y-auto text-xs">
                {audit.map((d) => (
                    <div key={d.id} className="rounded bg-white/5 px-2 py-1 text-slate-300">
                        {d.ts} · {d.campaign_id || d.clip_id} · <b>{d.decision}</b> · {d.rules_json} · {d.operator}
                    </div>
                ))}
            </div>

            {/* Transitions */}
            <h2 className="mt-8 text-lg font-bold">State transitions</h2>
            <div className="mt-2 max-h-64 space-y-1 overflow-y-auto text-xs">
                {transitions.slice(0, 100).map((t) => (
                    <div key={t.id} className="rounded bg-white/5 px-2 py-1 text-slate-300">
                        {t.ts} · {t.machine} · {t.previous_state ?? "∅"} → <b>{t.new_state}</b> · {t.actor} · {t.reason} {t.clip_id || t.campaign_id || ""}
                    </div>
                ))}
                {!transitions.length && <div className="text-slate-500">No transitions recorded yet.</div>}
            </div>

            {/* Operations */}
            <h2 className="mt-8 text-lg font-bold">Operations</h2>
            {readiness && (
                <div className="mt-2 rounded-lg bg-white/5 p-3 text-xs">
                    <div>Automation readiness: <b>{readiness.automation}</b> · mode {(readiness.config || {}).mode}</div>
                    <div className="mt-1 text-slate-400">{readiness.note}</div>
                </div>
            )}
            {overview && (
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
                    <div className="rounded bg-white/5 p-2">Pipeline: {JSON.stringify(overview.pipeline)}</div>
                    <div className="rounded bg-white/5 p-2">Publication: {JSON.stringify(overview.publication?.by_state || {})}</div>
                    <div className="rounded bg-white/5 p-2">Jobs: {JSON.stringify((overview.system || {}).jobs || {})}</div>
                    <div className="rounded bg-white/5 p-2">Last scan: {(overview.system || {}).last_scan || "never"}</div>
                </div>
            )}
            {alerts.length > 0 && (
                <div className="mt-2 space-y-1 text-xs">
                    {alerts.map((a, i) => (
                        <div key={i} className={`rounded px-2 py-1 ${a.severity === "critical" ? "bg-red-600/30" : "bg-amber-500/20"}`}>
                            <b>{a.alert}</b> · {a.detail} ({a.count ?? a.free_bytes ?? ""})
                        </div>
                    ))}
                </div>
            )}
            {sched && (
                <div className="mt-2 rounded-lg bg-white/5 p-3 text-xs">
                    <div className="font-bold">Scheduler</div>
                    <div className="text-slate-400">active {sched.active?.length ?? 0} · queued {sched.queued?.length ?? 0} · stale {sched.stale?.length ?? 0} · failed {sched.failed?.length ?? 0}</div>
                    <div className="mt-1 max-h-40 space-y-1 overflow-y-auto">
                        {(sched.jobs || []).slice(0, 15).map((j: any) => (
                            <div key={j.job_id} className="flex items-center justify-between rounded bg-white/5 px-2 py-1">
                                <span>{j.job_id} · {j.kind} · <b>{j.status}</b> · attempt {j.attempt ?? 0}</span>
                                <span className="flex gap-1">
                                    {j.status === "failed" && (
                                        <button onClick={async () => { try { await schedulerJobAction(j.job_id, "retry"); await refresh(); } catch (e: any) { setMsg(parseErr(e)); } }}
                                            className="rounded bg-white/10 px-2 py-0.5">Retry</button>
                                    )}
                                    {["queued", "running", "needs_review"].includes(j.status) && (
                                        <button onClick={async () => { try { await schedulerJobAction(j.job_id, "cancel"); await refresh(); } catch (e: any) { setMsg(parseErr(e)); } }}
                                            className="rounded bg-white/10 px-2 py-0.5">Cancel</button>
                                    )}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            )}
            {telemetry && (
                <div className="mt-2 rounded-lg bg-white/5 p-3 text-xs">
                    <div className="font-bold">Provider telemetry</div>
                    <div className="text-slate-400">requests {telemetry.aggregate?.requests ?? 0} · 429s {telemetry.aggregate?.count_429 ?? 0} · timeouts {telemetry.aggregate?.timeouts ?? 0} · 5xx {telemetry.aggregate?.count_5xx ?? 0} · unknown {telemetry.aggregate?.unknown_submissions ?? 0}</div>
                </div>
            )}
            <div className="mt-2 flex gap-2 text-xs">
                <button onClick={async () => {
                    try { const r = await retentionCleanup(true); setCleanup(r); setMsg(`Cleanup scan: ${(r.candidates || []).length} short-lived candidates`); }
                    catch (e: any) { setMsg(parseErr(e)); }
                }} className="rounded bg-white/10 px-3 py-1">Scan short-lived artifacts</button>
                <button onClick={async () => {
                    try { const r = await providerCheck("tiktok"); setMsg(`Provider check: ${r.state} — ${r.reason}`); await refresh(); }
                    catch (e: any) { setMsg(parseErr(e)); }
                }} className="rounded bg-white/10 px-3 py-1">Read-only provider check</button>
            </div>
            {cleanup && (cleanup.candidates || []).length > 0 && (
                <div className="mt-2 rounded bg-white/5 p-2 text-[11px] text-slate-400">
                    {(cleanup.candidates || []).slice(0, 10).map((c: any) => (
                        <div key={c.path}>{c.path} ({c.bytes}B)</div>
                    ))}
                </div>
            )}
        </div>
    );
}
