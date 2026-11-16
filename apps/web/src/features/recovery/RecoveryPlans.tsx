import { useEffect, useMemo, useRef, useState } from "react";
import type { components } from "@/lib/api/generated/schema";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, ApiError } from "@/lib/api/client";
import { Failure, Loading } from "@/app/Feedback";
import { buildIncidentHash, incidentReturnHash, safeRoute } from "@/app/routes";
import { analysisKey } from "@/features/diagnosis/Analysis";
import { useBaselines } from "@/features/workloads/queries";
import { AuthoritySummary, ExecutionOutcome, VerificationDetails, words, useRecoveryAvailability } from "./RecoveryEvidence";
import { PlanDiff } from "./PlanDiff";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

type Reviewed = components["schemas"]["ReviewedPlan"];
type Template = components["schemas"]["PlanView"]["template"];

export function RecoveryPlans({ id, canMutate, live, workloadId, runId, latestRunId, resolved = false, runBusy = false, canInvestigate = false }: {
  id: string; canMutate: boolean; live: boolean; workloadId?: string; runId?: string; latestRunId?: string; resolved?: boolean; runBusy?: boolean; canInvestigate?: boolean;
}) {
  const cache = useQueryClient(), feedback = useRef<HTMLDivElement>(null), heading = useRef<HTMLHeadingElement>(null);
  const route = safeRoute(window.location.hash), selection = route.id === id ? route.selection ?? {} : {};
  const [replicas, setReplicas] = useState(2), [rationale, setRationale] = useState("");
  const [baselineId, setBaselineId] = useState(""), [profileId, setProfileId] = useState("");
  const [offset, setOffset] = useState(0), [executionOffset, setExecutionOffset] = useState(0);
  const [approval, setApproval] = useState<Reviewed | null>(null), [manualReason, setManualReason] = useState("");
  const [clock, setClock] = useState(() => performance.now());
  useEffect(() => { setBaselineId(""); setProfileId(""); setApproval(null); }, [runId, workloadId]);
  useEffect(() => { const timer = window.setInterval(() => setClock(performance.now()), 1000); return () => window.clearInterval(timer); }, []);
  const hash = (plan?: string, execution?: string) => buildIncidentHash(id, route.id === id ? route.run : runId, incidentReturnHash(window.location.hash), route.resolution, route.evidence, { ...selection, plan, execution });
  const navigate = (plan?: string, execution?: string) => { setApproval(null); window.location.hash = hash(plan, execution); };
  const plans = useQuery({ queryKey: ["plans", id, offset], queryFn: async () => unwrap(await api.GET("/api/incidents/{incident_id}/plans", { params: { path: { incident_id: id }, query: { offset } } })), refetchInterval: 5000 });
  const executions = useQuery({ queryKey: ["executions", id, executionOffset], queryFn: async () => unwrap(await api.GET("/api/incidents/{incident_id}/executions", { params: { path: { incident_id: id }, query: { offset: executionOffset } } })), refetchInterval: 2000 });
  const exactExecution = useQuery({ queryKey: ["execution", selection.execution], enabled: !!selection.execution, queryFn: async () => unwrap(await api.GET("/api/executions/{execution_id}", { params: { path: { execution_id: selection.execution! } } })), refetchInterval: 2000, retry: false });
  const planId = selection.plan || (selection.execution ? exactExecution.data?.plan_id || "" : plans.data?.[0]?.id || "");
  const review = useQuery({ queryKey: ["plan-review", planId], enabled: !!planId, queryFn: async () => unwrap(await api.GET("/api/plans/{plan_id}", { params: { path: { plan_id: planId } } })), refetchInterval: 30000, retry: false });
  const plan = review.data?.plan;
  const execution = selection.execution ? exactExecution.data : executions.data?.find(row => row.plan_id === planId);
  const executionId = execution?.id;
  const inScope = !!plan && plan.incident_id === id && (!execution || execution.plan_id === plan.id && execution.workload_id === plan.workload_id);
  const verification = useQuery({ queryKey: ["recovery-verification", executionId], enabled: !!executionId && inScope, retry: false, refetchInterval: 3000,
    queryFn: async () => { try { return unwrap(await api.GET("/api/executions/{execution_id}/verification", { params: { path: { execution_id: executionId! } } })); } catch (error) { if (error instanceof ApiError && error.status === 404 && error.code === "verification_not_found") return null; throw error; } },
  });
  const forwardId = plan?.forward_execution_id || executionId;
  const rollback = useQuery({ queryKey: ["rollback", forwardId], enabled: !!forwardId && inScope, queryFn: async () => unwrap(await api.GET("/api/executions/{execution_id}/rollback", { params: { path: { execution_id: forwardId! } } })), refetchInterval: 5000, retry: false });
  const availability = useRecoveryAvailability(workloadId), baselines = useBaselines(workloadId);
  const rules = useQuery({ queryKey: ["recovery-policy"], queryFn: async () => unwrap(await api.GET("/api/recovery-policy")), retry: false });
  const analysis = useQuery({ queryKey: analysisKey(id, runId || ""), enabled: !!runId, queryFn: async () => unwrap(await api.GET("/api/incidents/{incident_id}/runs/{run_id}/analysis", { params: { path: { incident_id: id, run_id: runId! } } })), refetchInterval: 5000, retry: false });
  const candidate = analysis.data?.result?.status === "supported" ? analysis.data.result.candidate_template_id : null;
  const sourceCurrent = !!runId && runId === latestRunId && analysis.data?.status === "completed" && !!analysis.data.analysis_id;
  const memory = rules.data?.policy?.memory_profiles.filter(value => value.workload_uid === availability.data?.uid) ?? [];
  const saved = baselines.data?.pages.flatMap(page => page.items) ?? [];
  const ready = canMutate && live && !resolved && !runBusy && availability.data?.target_authority === "enabled" && availability.data.application_state === "ready_for_preflight" && !availability.isError;
  const refresh = async () => { await Promise.all([cache.invalidateQueries({ queryKey: ["plans", id] }), cache.invalidateQueries({ queryKey: ["plan-review"] }), cache.invalidateQueries({ queryKey: ["executions", id] }), cache.invalidateQueries({ queryKey: ["recovery-availability", workloadId] }), cache.invalidateQueries({ queryKey: ["rollback"] })]); };
  const propose = useMutation({ retry: false, onMutate: () => { setManualReason(""); decide.reset(); return window.location.hash; }, mutationFn: async (template?: Template) => {
    const body = template ? { origin: "analysis_advice" as const, run_id: runId!, analysis_id: analysis.data!.analysis_id!, template, rationale,
      ...(template === "restore_deployment_template" ? { baseline_id: baselineId } : template === "set_memory_profile" ? { profile_id: profileId } : { replicas }) } : { origin: "operator_manual" as const, replicas, rationale };
    return unwrap(await api.POST("/api/incidents/{incident_id}/plans", { params: { path: { incident_id: id } }, body }));
  }, onSuccess: async (value, _template, originHash) => { await refresh(); if (originHash !== window.location.hash) return; if ("id" in value) navigate(value.id); else setManualReason(value.reason + " " + value.next_step); } });
  const decide = useMutation({ retry: false, mutationFn: async ({ decision, receipt }: { decision: "approve" | "reject"; receipt: Reviewed }) => unwrap(await api.POST("/api/plans/{plan_id}/decision", { params: { path: { plan_id: receipt.plan.id } }, body: { version: receipt.plan.version, digest: receipt.plan.digest, review_token: receipt.review_token, decision } })), onSettled: () => setApproval(null), onSuccess: refresh });
  const inverse = useMutation({ retry: false, onMutate: () => { setManualReason(""); decide.reset(); return window.location.hash; }, mutationFn: async () => unwrap(await api.POST("/api/executions/{execution_id}/inverse-plans", { params: { path: { execution_id: forwardId! } }, body: { rationale } })), onSuccess: async (value, _input, originHash) => { await refresh(); if (originHash === window.location.hash) navigate(value.id); } });
  const received = useMemo(() => ({ at: performance.now(), server: Date.parse(review.data?.server_time || "") }), [review.data]);
  const now = received.server + Math.max(0, clock - received.at);
  const listed = plans.data?.find(row => row.id === planId);
  const changed = !!plan && !!listed && (listed.digest !== plan.digest || listed.version !== plan.version || listed.state !== plan.state);
  const available = ready && inScope && review.data?.valid && plan?.state === "proposed" && now < Date.parse(plan.expires_at) && now < Date.parse(review.data.review_expires_at) && !review.isFetching && !review.isError && !changed && !propose.isPending && !inverse.isPending;
  useEffect(() => setApproval(null), [planId, plan?.digest, plan?.state, review.data?.review_token, review.data?.valid, review.isError, canMutate, changed]);
  useEffect(() => { setManualReason(""); propose.reset(); decide.reset(); inverse.reset(); }, [selection.plan, selection.execution, runId, workloadId]);
  useEffect(() => { if (approval && !available && !decide.isPending) { setApproval(null); setManualReason("The exact review expired or changed. Read it again before a new explicit decision."); } }, [available, approval, decide.isPending]);
  useEffect(() => { if (changed) void review.refetch(); }, [changed]);
  useEffect(() => { if (propose.isError || decide.isError || inverse.isError || manualReason) feedback.current?.focus(); }, [propose.isError, decide.isError, inverse.isError, manualReason]);
  useEffect(() => { if (selection.plan || selection.execution) heading.current?.focus(); }, [selection.plan, selection.execution]);
  const validReplicas = Number.isInteger(replicas) && replicas >= 1 && replicas <= 3;
  const selectorValid = candidate === "restore_deployment_template" ? saved.some(row => row.id === baselineId && row.profile === "deployment-images-resources/v1") : candidate === "set_memory_profile" ? memory.some(row => row.id === profileId) : candidate === "scale_deployment" && validReplicas;
  return <section className="foundation-panel foundation-stack min-w-0 break-words" aria-label="Recovery review">
    <h2 id="recovery-review" ref={heading} tabIndex={-1}>Recovery review</h2>
    <p>Stored advice proposes one template. Server policy derives exact fields; an independent explicit approval is always required. Manual scale has no model attribution.</p>
    {!canMutate && <p>Viewer access: inspect exact history, evidence and exports. An Operator must propose or decide a change.</p>}
    {!live && <p>Synthetic evidence cannot establish an executable target. Configure an allowlisted live workload.</p>}
    {resolved && <p>Reopen this saved incident explicitly before new recovery work.</p>}{runBusy && <p>Investigation is active. Wait for its recorded result before approval.</p>}
    {availability.isPending && workloadId && <Loading />}{availability.isError && <Failure error={availability.error} retry={() => void availability.refetch()} />}{availability.data && <AuthoritySummary value={availability.data} />}
    {canMutate && live && <section className="foundation-stack" aria-label="Prepare a recovery plan">
      <Label htmlFor="recovery-rationale">Reason for the proposed change</Label><Input id="recovery-rationale" maxLength={2000} value={rationale} onChange={event => setRationale(event.target.value)} />
      <Label htmlFor="recovery-replicas">Desired replicas (1–3)</Label><Input id="recovery-replicas" type="number" min={1} max={3} value={replicas} onChange={event => setReplicas(Number(event.target.value))} />
      <Button disabled={!ready || !rationale.trim() || !validReplicas || propose.isPending} onClick={() => propose.mutate(undefined)}>Prepare live scale plan</Button>
      <h3>Selected Run advice</h3><p className="mono break-all">Run: {runId || "None selected"}</p>
      {analysis.isError && <Failure error={analysis.error} retry={() => void analysis.refetch()} />}
      {!sourceCurrent && <p>Only the exact latest completed operational analysis can source a new plan. Historical findings remain readable; request a new investigation for fresh evidence.</p>}
      {!candidate && <p>No supported recovery template is recorded for this Run. Inspect missing evidence and guidance; manual scale remains a separate explicit intent.</p>}
      {candidate && <><p>Advisory template: {words(candidate)}. Preparing a preview does not call the model.</p>
        {candidate === "restore_deployment_template" && <><Label htmlFor="recovery-baseline">Exact confirmed baseline</Label><select id="recovery-baseline" value={baselineId} onChange={event => setBaselineId(event.target.value)}><option value="">Choose a confirmed baseline</option>{saved.map(row => <option key={row.id} value={row.id} disabled={row.profile !== "deployment-images-resources/v1"}>{row.id} · {row.profile} · {row.confirmed_at}</option>)}</select>{!saved.length && <p>No confirmed baseline is available. Inspect and confirm a healthy original-UID baseline in Workloads.</p>}{baselines.hasNextPage && <Button variant="outline" onClick={() => void baselines.fetchNextPage()}>Load older baselines</Button>}</>}
        {candidate === "set_memory_profile" && <><Label htmlFor="recovery-memory">Qualified memory profile</Label><select id="recovery-memory" value={profileId} onChange={event => setProfileId(event.target.value)}><option value="">Choose a qualified profile</option>{memory.map(row => <option key={row.id} value={row.id}>{row.id} / {row.version} · request {row.request} · limit {row.limit}</option>)}</select>{!memory.length && <p>No qualified memory profile exists for the original UID. Use manual guidance.</p>}</>}
        <Button variant="outline" disabled={!ready || !sourceCurrent || !selectorValid || !rationale.trim() || propose.isPending} onClick={() => propose.mutate(candidate)}>Prepare exact advice plan</Button>
      </>}
      {workloadId && <a href={`#/workloads?workload=${workloadId}`}>Inspect original workload and baseline context</a>}{rules.isError && <Failure error={rules.error} retry={() => void rules.refetch()} />}{baselines.isError && <Failure error={baselines.error} />}
    </section>}
    <div ref={feedback} tabIndex={-1} aria-label="Recovery action feedback">{manualReason && <p role="status">{manualReason}</p>}{propose.isError && <Failure error={propose.error} />}{decide.isError && <><Failure error={decide.error} /><p>Confirmation was discarded. Read a fresh exact plan and make a new explicit decision. No mutation was replayed.</p></>}{inverse.isError && <Failure error={inverse.error} />}</div>
    <h3>Plan history</h3>{plans.isPending ? <Loading /> : plans.isError ? <Failure error={plans.error} retry={() => void plans.refetch()} /> : <>
      {!plans.data.length && <p>No retained plans.</p>}{plans.data.map(row => <a key={row.id} className="break-all" aria-current={row.id === planId ? "true" : undefined} href={hash(row.id)}>{words(row.template)} · {words(row.state)} · {row.id}</a>)}
      <div className="foundation-actions"><Button variant="outline" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous plans</Button><Button variant="outline" disabled={plans.data.length < 25} onClick={() => setOffset(offset + 25)}>Older plans</Button></div>
    </>}
    {review.isFetching && planId && <p role="status">Reading exact plan and current validity…</p>}{review.isError && <Failure error={review.error} retry={() => void review.refetch()} />}
    {plan && !inScope && <p role="alert">This record belongs to another incident. Choose an exact record from this incident; no action is available.</p>}
    {plan && inScope && <><PlanDiff plan={plan} /><p role="status">{review.data?.reason}</p><p>Review receipt expires {review.data?.review_expires_at}. Changing or refreshing the review discards confirmation.</p><Button variant="outline" disabled={review.isFetching} onClick={() => { setManualReason(""); decide.reset(); void review.refetch(); }}>Read exact plan again</Button>
      {canMutate && <div className="foundation-actions"><Button disabled={!available || decide.isPending} onClick={() => { setManualReason(""); decide.reset(); setApproval(review.data!); }}>Review approval</Button><Button variant="outline" disabled={!available || decide.isPending} onClick={() => decide.mutate({ decision: "reject", receipt: review.data! })}>Reject exact plan</Button></div>}
      {!available && plan.state === "proposed" && <p>Stale, expired or unavailable authority requires fresh evidence and a new exact review. Start a new investigation in this workspace; no approval is reused.</p>}
      {canInvestigate && <Button variant="outline" onClick={() => document.getElementById("run-reason")?.focus()}>Start a new investigation</Button>}
    </>}
    <h3>Execution history</h3>{executions.isPending ? <Loading /> : executions.isError ? <Failure error={executions.error} retry={() => void executions.refetch()} /> : <>
      {!executions.data.length && <p>No retained executions. A preview or rejection makes no write.</p>}{executions.data.map(row => <a key={row.id} className="break-all" aria-current={row.id === executionId ? "true" : undefined} href={hash(row.plan_id, row.id)}>{words(row.state)} · {row.id}</a>)}
      <div className="foundation-actions"><Button variant="outline" disabled={!executionOffset} onClick={() => setExecutionOffset(Math.max(0, executionOffset - 25))}>Previous executions</Button><Button variant="outline" disabled={executions.data.length < 25} onClick={() => setExecutionOffset(executionOffset + 25)}>Older executions</Button></div>
    </>}{exactExecution.isError && <Failure error={exactExecution.error} />}
    {execution && inScope && <article className="foundation-panel foundation-stack min-w-0"><p className="mono break-all">Selected execution: {execution.id}</p><ExecutionOutcome state={execution.state} outcome={execution.outcome} />
      {verification.isPending ? <Loading /> : verification.isError ? <Failure error={verification.error} retry={() => void verification.refetch()} /> : verification.data ? <VerificationDetails value={verification.data} /> : <p>No qualified verification record exists. Legacy acknowledgement does not establish application recovery.</p>}
      {rollback.isError && <Failure error={rollback.error} retry={() => void rollback.refetch()} />}
      {rollback.data && <section className="foundation-stack"><h4>Recorded inverse eligibility</h4><p>{rollback.data.reason}</p><p>Eligible from: {rollback.data.eligible_from ?? "Not recorded"}<br />Independent deadline: {rollback.data.deadline ?? "Not recorded"}</p>
        {plan?.forward_execution_id && <a href={hash(undefined, plan.forward_execution_id)}>Inspect original forward operation</a>}{rollback.data.inverse_execution_id && <a href={hash(undefined, rollback.data.inverse_execution_id)}>Inspect separately approved inverse operation</a>}
        {canMutate && !plan?.forward_execution_id && <Button variant="outline" disabled={!ready || !rollback.data.can_propose || !rollback.data.deadline || !(now < Date.parse(rollback.data.deadline)) || !rationale.trim() || inverse.isPending} onClick={() => inverse.mutate()}>Prepare captured inverse for separate review</Button>}
      </section>}
    </article>}
    <Dialog open={!!approval} onOpenChange={open => { if (!open) setApproval(null); }}><DialogContent onCloseAutoFocus={event => { if (decide.isError || review.isError || manualReason) { event.preventDefault(); feedback.current?.focus(); } }}><DialogHeader><DialogTitle>Approve the exact plan</DialogTitle><DialogDescription>Approve only this displayed version and digest. Session change or expiry discards confirmation; sign-in never replays the decision.</DialogDescription></DialogHeader><p className="mono break-all">{approval?.plan.id}<br />{approval?.plan.digest}</p><Button className="h-auto w-full whitespace-normal py-2" disabled={!available || decide.isPending || approval?.review_token !== review.data?.review_token} onClick={() => { if (approval) decide.mutate({ decision: "approve", receipt: approval }); }}>Approve this version and dispatch</Button></DialogContent></Dialog>
  </section>;
}
