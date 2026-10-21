import { useState, type Dispatch } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  Check,
  Copy,
  Download,
  FileText,
  Play,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { StatusBadge, downloadText } from "@/components/domain";
import {
  handoffMarkdown,
  type DemoState,
  type DemoAction,
  type Incident,
  type Evidence,
  type InvestigationRun,
} from "@/demo/model";

type Props = {
  state: DemoState;
  incident: Incident;
  dispatch: Dispatch<DemoAction>;
  tab: string;
  runId?: string;
  onTab: (tab: string, run?: string) => void;
  onBack: () => void;
  onOpenWorkload: () => void;
  onOpenIncident: (id: string) => void;
};
type Decision =
  | "approve"
  | "reject"
  | "resolve"
  | "reopen"
  | "reinvestigate"
  | "request_rollback"
  | "manual_handoff"
  | "verify_manually";
const labels: Record<Decision, string> = {
  approve: "Approve change",
  reject: "Reject proposal",
  resolve: "Resolve incident",
  reopen: "Reopen incident",
  reinvestigate: "Re-investigate",
  request_rollback: "Confirm rollback",
  manual_handoff: "Record manual handoff",
  verify_manually: "Verify manually and unlock",
};

export function IncidentWorkspace({
  state,
  incident: i,
  dispatch,
  tab,
  runId,
  onTab,
  onBack,
  onOpenWorkload,
  onOpenIncident,
}: Props) {
  const [evidence, setEvidence] = useState<Evidence | null>(null),
    [decision, setDecision] = useState<Decision | null>(null),
    [reason, setReason] = useState(""),
    [manualEvidence, setManualEvidence] = useState(""),
    [stopped, setStopped] = useState(false),
    [outcome, setOutcome] = useState<"applied" | "not_applied">("applied"),
    [note, setNote] = useState(""),
    [copied, setCopied] = useState(false),
    [runbook, setRunbook] = useState(false),
    [resolutionId, setResolutionId] = useState(""),
    [compareId, setCompareId] = useState("");
  const readonly = state.role === "viewer",
    active = ["queued", "executing", "verifying", "rolling_back"].includes(
      i.execution.status,
    ),
    working = ["queued", "collecting", "analyzing"].includes(i.run.status);
  const historicalDefault = !["completed", "insufficient_evidence"].includes(
    i.run.status,
  )
    ? [...i.runs].reverse().find((r) => r.status === "completed")
    : undefined;
  const run = i.runs.find((r) => r.id === runId) || historicalDefault || i.run,
    old = run.id !== i.run.id;
  const other =
    i.runs.find((r) => r.id === compareId) ||
    i.runs.find((r) => r.id !== run.id) ||
    run;
  const plan = i.plan,
    locked = state.targetLocks[i.workloadId],
    md = handoffMarkdown(i, resolutionId || undefined);
  const executionPlan = i.plans.find(
    (p) => `${p.id}-execution` === i.execution.id,
  );
  const decisionPlan = decision === "request_rollback" ? executionPlan : plan;
  const act = (action: DemoAction) =>
    dispatch({
      ...action,
      ...("workloadId" in action ? {} : { incidentId: i.id }),
    } as DemoAction);
  const open = (d: Decision) => {
    setReason("");
    setManualEvidence("");
    setStopped(false);
    setDecision(d);
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  const submit = () => {
    if (!decision) return;
    if (decision === "verify_manually")
      act({
        type: decision,
        reason,
        evidence: manualEvidence,
        oldRequestStopped: stopped,
        outcome,
      });
    else act({ type: decision, reason });
    setDecision(null);
  };
  const canAdvance = working || active;
  return (
    <>
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft />
        Back
      </Button>
      <div className="incident-heading">
        <div>
          <div className="flex gap-2 items-center flex-wrap">
            <span className="mono muted text-xs">{i.id}</span>
            <Badge variant="outline">
              {i.runs[0]?.reason.startsWith("Manual investigation")
                ? "Manual investigation"
                : i.severity}
            </Badge>
            <StatusBadge status={i.status} />
          </div>
          <h1>{i.title}</h1>
          <button className="workload-link" onClick={onOpenWorkload}>
            {i.namespace} / {i.workload}
            <ArrowUpRight size={14} />
          </button>
          <p className="muted mt-2">{i.impact}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={readonly || i.status !== "open"}
            onClick={() => act({ type: "acknowledge" })}
          >
            {i.acknowledgedBy ? "Acknowledged" : "Acknowledge"}
          </Button>
          <Button
            variant="outline"
            disabled={
              readonly ||
              active ||
              i.execution.status === "uncertain" ||
              working ||
              i.run.status === "waiting_for_quota" ||
              i.status === "resolved"
            }
            onClick={() => open("reinvestigate")}
          >
            <RotateCcw />
            Re-investigate
          </Button>
          <Button
            disabled={
              readonly ||
              active ||
              i.execution.status === "uncertain" ||
              ["collecting", "analyzing"].includes(i.run.status)
            }
            onClick={() => open(i.status === "resolved" ? "reopen" : "resolve")}
          >
            {i.status === "resolved" ? "Reopen" : "Resolve incident"}
          </Button>
        </div>
      </div>
      <div className="run-meta">
        <span>Evidence: {i.freshness}</span>
        <span className="mono">{i.run.configurationVersion}</span>
        <span>Retrieval: full text</span>
      </div>
      {(canAdvance ||
        i.run.status === "waiting_for_quota" ||
        i.run.status === "failed" ||
        i.run.status === "insufficient_evidence") && (
        <Alert className="my-4">
          <Play />
          <AlertTitle>
            {active
              ? "Executing approved decision"
              : "Latest investigation progress"}{" "}
            ·{" "}
            <StatusBadge status={active ? i.execution.status : i.run.status} />
          </AlertTitle>
          <AlertDescription>
            <p>{active ? i.execution.summary : i.run.summary}</p>
            <div className="flex gap-2 flex-wrap mt-3">
              {canAdvance && (
                <Button
                  size="sm"
                  disabled={readonly}
                  onClick={() => act({ type: "advance" })}
                >
                  Advance simulation
                </Button>
              )}
              {[
                "waiting_for_quota",
                "failed",
                "insufficient_evidence",
              ].includes(i.run.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={readonly}
                  onClick={() => act({ type: "retry" })}
                >
                  Retry investigation
                </Button>
              )}
              {["queued", "waiting_for_quota"].includes(i.run.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={readonly}
                  onClick={() => act({ type: "cancel_run" })}
                >
                  Cancel queued run
                </Button>
              )}
            </div>
          </AlertDescription>
        </Alert>
      )}
      {locked && (
        <Alert className="my-4">
          <ShieldCheck />
          <AlertTitle>Target locked · {locked.incidentId}</AlertTitle>
          <AlertDescription>
            {locked.reason} Do not resend a write while its outcome is unknown.
            {locked.incidentId !== i.id && (
              <Button
                variant="link"
                onClick={() => onOpenIncident(locked.incidentId)}
              >
                Open incident holding the lock
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}
      <div className="incident-layout">
        <section className="investigation-main">
          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <Label htmlFor="run-select">Investigation snapshot</Label>
            <select
              id="run-select"
              className="native-select max-w-80"
              value={run.id}
              onChange={(e) => onTab(tab, e.target.value)}
            >
              {i.runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.status}
                </option>
              ))}
            </select>
          </div>
          {old && (
            <Alert className="mb-4">
              <AlertTitle>Viewing a previous result · {run.id}</AlertTitle>
              <AlertDescription>
                Latest run {i.run.id}: {i.run.status}. This finding does not yet reflect
                the latest run.{" "}
                <Button variant="link" onClick={() => onTab(tab, i.run.id)}>
                  View latest run
                </Button>
              </AlertDescription>
            </Alert>
          )}
          <Tabs value={tab} onValueChange={(t) => onTab(t, run.id)}>
            <div className="tabs-scroll">
              <TabsList>
                <TabsTrigger value="findings">Findings</TabsTrigger>
                <TabsTrigger value="evidence">
                  Evidence ({run.evidence.length})
                </TabsTrigger>
                <TabsTrigger value="activity">Activity</TabsTrigger>
                <TabsTrigger value="compare">Compare runs</TabsTrigger>
                <TabsTrigger value="handoff">Handoff</TabsTrigger>
              </TabsList>
            </div>
            <TabsContent value="findings">
              <div className="content-panel">
                <div className="panel-heading">
                  <h2>Investigation findings</h2>
                  <StatusBadge status={run.status} />
                </div>
                <p className="prose-block">{run.summary}</p>
                <div className="flex flex-wrap gap-2 my-4">
                  {run.supportingEvidenceIds.map((id) => (
                    <Button
                      key={id}
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setEvidence(
                          run.evidence.find((e) => e.id === id) || null,
                        )
                      }
                    >
                      <FileText size={13} />
                      {id}
                    </Button>
                  ))}
                </div>
                <MetricOverview run={run} pattern={i.pattern} />
                <div className="two-cols mt-6">
                  <div>
                    <h3>Ruled out</h3>
                    <BulletList
                      items={
                        run.ruledOut.length
                          ? run.ruledOut
                          : ["No additional causes have been ruled out with sufficient evidence."]
                      }
                    />
                  </div>
                  <div>
                    <h3>Unverified</h3>
                    <BulletList items={run.unverified} />
                  </div>
                </div>
                {run.missingEvidence.length > 0 && (
                  <Alert className="mt-5">
                    <AlertTitle>Missing evidence</AlertTitle>
                    <AlertDescription>
                      <BulletList items={run.missingEvidence} />
                    </AlertDescription>
                  </Alert>
                )}
                <Button
                  variant="outline"
                  className="mt-5"
                  onClick={() => setRunbook(true)}
                >
                  <BookOpen />
                  Referenced runbook · {i.pattern} v3
                </Button>
              </div>
            </TabsContent>
            <TabsContent value="evidence">
              <div className="content-panel">
                <h2>Evidence for {run.id}</h2>
                <p className="muted mb-5">
                  Sources, timestamps and original data are attached to each investigation run.
                </p>
                <div className="evidence-grid">
                  {run.evidence.map((e) => (
                    <button
                      key={e.id}
                      className="evidence-card"
                      onClick={() => setEvidence(e)}
                    >
                      <div className="flex justify-between gap-2">
                        <FileText size={18} />
                        <StatusBadge status={e.status} />
                      </div>
                      <h3>{e.title}</h3>
                      <p className="mono text-xs">{e.id}</p>
                      <p className="muted text-xs mt-2">{e.source}</p>
                      <p className="muted text-xs">{e.observedAt}</p>
                    </button>
                  ))}
                </div>
                {!run.evidence.length && (
                  <p className="empty-state">
                    No snapshot exists for this run yet. Continue evidence collection.
                  </p>
                )}
              </div>
            </TabsContent>
            <TabsContent value="activity">
              <div className="content-panel">
                <h2>Activity and notes</h2>
                <form
                  className="note-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    act({ type: "add_note", reason: note });
                    setNote("");
                  }}
                >
                  <Textarea
                    aria-label="Incident note"
                    placeholder="Record observations or actions taken outside the application…"
                    required
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    disabled={readonly}
                  />
                  <Button type="submit" disabled={readonly || !note.trim()}>
                    Add note
                  </Button>
                </form>
                <div className="timeline">
                  {[...i.activity].reverse().map((a) => (
                    <div className="timeline-item" key={a.id}>
                      <span className="text-xs muted">
                        {a.at} · {a.actor}
                      </span>
                      <h3>{a.title}</h3>
                      <p>{a.detail}</p>
                    </div>
                  ))}
                </div>
                <h3 className="mt-5">Saved notes</h3>
                {i.notes.map((n) => (
                  <div key={n.id} className="prose-block">
                    <p>{n.text}</p>
                    <small className="muted">
                      {n.actor} · {n.at}
                    </small>
                  </div>
                ))}
              </div>
            </TabsContent>
            <TabsContent value="compare">
              <div className="content-panel">
                <h2>Compare two investigation runs</h2>
                <Label htmlFor="compare-run">Compare with</Label>
                <select
                  id="compare-run"
                  className="native-select my-3"
                  value={other.id}
                  onChange={(e) => setCompareId(e.target.value)}
                >
                  {i.runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.id} · {r.status}
                    </option>
                  ))}
                </select>
                {i.runs.length < 2 && (
                  <p className="muted mb-4">
                    Re-investigate to create a second snapshot. The earlier run remains
                    unchanged.
                  </p>
                )}
                <div className="compare-grid">
                  {[run, other].map((r, index) => (
                    <div key={index}>
                      <div className="panel-heading">
                        <h3>{r.id}</h3>
                        <StatusBadge status={r.status} />
                      </div>
                      <p>{r.summary}</p>
                      <p className="muted my-3">
                        {r.reason} · {r.createdAt}
                      </p>
                      {r.evidence.map((e) => (
                        <Button
                          className="mb-2 mr-2"
                          key={e.id}
                          variant="outline"
                          size="sm"
                          onClick={() => setEvidence(e)}
                        >
                          {e.id}
                        </Button>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </TabsContent>
            <TabsContent value="handoff">
              <div className="content-panel">
                <div className="panel-heading">
                  <h2>Handoff and resolution history</h2>
                  <Badge variant="outline">
                    {i.resolutions.length} snapshots
                  </Badge>
                </div>
                <Label htmlFor="resolution">Report version</Label>
                <select
                  id="resolution"
                  className="native-select my-3"
                  value={resolutionId}
                  onChange={(e) => {
                    setResolutionId(e.target.value);
                    setCopied(false);
                  }}
                >
                  <option value="">Current state</option>
                  {i.resolutions.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.id} · {r.at}
                    </option>
                  ))}
                </select>
                {resolutionId && (
                  <p className="muted mb-3">
                    This snapshot was saved at resolution and does not change after reopening or
                    further verification.
                  </p>
                )}
                <div className="flex gap-2 mb-4">
                  <Button variant="outline" onClick={() => copy(md)}>
                    {copied ? <Check /> : <Copy />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                  <Button
                    onClick={() =>
                      downloadText(
                        `${i.id}-${resolutionId || "handoff"}.md`,
                        md,
                      )
                    }
                  >
                    <Download />
                    Download Markdown
                  </Button>
                </div>
                <pre className="handoff-preview">{md}</pre>
              </div>
            </TabsContent>
          </Tabs>
        </section>
        <aside className="decision-rail">
          <div className="content-panel">
            <div className="eyebrow">NEXT STEP</div>
            <h2>
              {i.status === "resolved"
                ? "Incident resolved"
                : plan
                  ? "Review proposed change"
                  : "Manual guidance"}
            </h2>
            {plan ? (
              <>
                <div className="flex flex-wrap gap-2 my-3">
                  <StatusBadge status={plan.status} />
                  <Badge variant="secondary">{plan.template}</Badge>
                </div>
                <p className="mono text-xs break-all muted">{plan.hash}</p>
                <Diff rows={plan.diff} />
                <details className="my-4">
                  <summary>Prerequisites, risks and verification</summary>
                  <h3 className="mt-3">Prerequisites</h3>
                  <BulletList items={plan.prerequisites} />
                  <h3 className="mt-3">Risks</h3>
                  <BulletList items={plan.risks} />
                  <h3 className="mt-3">Verification</h3>
                  <BulletList items={plan.verification} />
                  <p className="text-xs muted mt-3">
                    UID: {plan.targetUid}
                    <br />
                    Expires: {plan.expiresAt}
                    <br />
                    Run: {plan.runId}
                  </p>
                </details>
                {plan.status === "awaiting_approval" &&
                i.status !== "resolved" ? (
                  <div className="flex gap-2">
                    <Button
                      className="flex-1"
                      disabled={readonly || Boolean(locked)}
                      onClick={() => open("approve")}
                    >
                      Review and approve
                    </Button>
                    <Button
                      variant="outline"
                      disabled={readonly}
                      onClick={() => open("reject")}
                    >
                      Reject
                    </Button>
                  </div>
                ) : (
                  <p className="muted text-sm">
                    {i.status === "resolved"
                      ? "Reopen the incident if a new decision is needed."
                      : [
                            "stale",
                            "expired",
                            "rejected",
                            "invalidated",
                          ].includes(plan.status)
                        ? "This proposal is no longer actionable. Re-investigate to create a fresh plan."
                        : "Decisions and outcomes are recorded in Activity."}
                  </p>
                )}
              </>
            ) : (
              <>
                <p className="prose-block">
                  {i.manualReason ||
                    "Wait for the investigation result to determine the next step."}
                </p>
                <Button variant="outline" onClick={() => setRunbook(true)}>
                  <BookOpen />
                  Read runbook
                </Button>
              </>
            )}
          </div>
          <div className="content-panel">
            <h2>Execution outcome</h2>
            <StatusBadge status={i.execution.status} />
            <p className="prose-block">{i.execution.summary}</p>
            {i.execution.id && (
              <p className="mono text-xs muted break-all my-3">
                Execution: {i.execution.id}
              </p>
            )}
            <BulletList items={i.execution.verification} />
            {i.execution.status === "none" && (
              <p className="muted text-sm">
                Approve a proposal to begin. Each simulated stage advances when you choose
                to continue.
              </p>
            )}
            {i.status !== "resolved" &&
              ["failed", "succeeded"].includes(i.execution.status) &&
              i.execution.rollbackEligible && (
                <Button
                  variant="outline"
                  disabled={readonly}
                  onClick={() => open("request_rollback")}
                >
                  <RotateCcw />
                  Review rollback
                </Button>
              )}
            {i.execution.outcomeUnknown && (
              <>
                <p className="text-sm mt-3">
                  Reconciliation: {i.execution.reconcileAttempts}/2. Do not infer
                  success from Ready status alone.
                </p>
                <Button
                  className="mt-3"
                  variant="outline"
                  disabled={readonly || i.execution.reconcileAttempts >= 2}
                  onClick={() => act({ type: "reconcile" })}
                >
                  Reconcile outcome
                </Button>
                <Button
                  className="mt-3 w-full"
                  variant="outline"
                  disabled={readonly || i.execution.reconcileAttempts < 2}
                  onClick={() => open("verify_manually")}
                >
                  Verify manually and unlock
                </Button>
              </>
            )}
            <p className="muted text-xs mt-4">{i.expectedOutcome}</p>
          </div>
          <Button
            variant="outline"
            disabled={readonly || active || i.execution.status === "uncertain"}
            onClick={() => open("manual_handoff")}
          >
            Manual handoff
          </Button>
          <Button variant="ghost" onClick={() => onTab("handoff", run.id)}>
            <FileText />
            View handoff report
          </Button>
        </aside>
      </div>
      <Sheet
        open={!!evidence}
        onOpenChange={(v) => {
          if (!v) setEvidence(null);
        }}
      >
        <SheetContent className="evidence-sheet">
          <SheetHeader>
            <SheetTitle>{evidence?.title}</SheetTitle>
            <SheetDescription>
              {evidence?.id} · saved snapshot
            </SheetDescription>
          </SheetHeader>
          {evidence && (
            <div className="p-4 space-y-5">
              <StatusBadge status={evidence.status} />
              <dl>
                <dt className="muted">Source</dt>
                <dd>{evidence.source}</dd>
                <dt className="muted mt-3">Observed at</dt>
                <dd>{evidence.observedAt}</dd>
              </dl>
              <pre className="handoff-preview">{evidence.excerpt}</pre>
              <Button variant="outline" onClick={() => copy(evidence.excerpt)}>
                <Copy />
                {copied ? "Copied" : "Copy evidence"}
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>
      <Dialog open={runbook} onOpenChange={setRunbook}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{i.pattern} · runbook v3</DialogTitle>
            <DialogDescription>
              Version referenced by {run.id} · full-text retrieval
            </DialogDescription>
          </DialogHeader>
          <ol className="check-list">
            <li>Check the workload UID, owner and observation window.</li>
            <li>
              Compare events, previous logs and telemetry; identify missing information.
            </li>
            <li>
              {i.pattern === "OOM"
                ? "Compare OOMKilled with the memory limit; a single restart does not establish a memory leak."
                : i.pattern === "Load"
                  ? "Check RPS, latency, throttling, capacity and HPA ownership before scaling."
                  : i.pattern === "CrashLoop"
                    ? "Restore only a confirmed healthy template baseline; referenced ConfigMap and Secret contents are not restored."
                    : "Hand evidence to the resource owner; this pattern has no automated recovery action."}
            </li>
            <li>
              Verify using fresh data and adequate coverage; hand off if the outcome
              remains unknown.
            </li>
          </ol>
          <p className="muted text-xs">
            Illustrative content for UX review. Knowledge contains the current
            runbooks.
          </p>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!decision}
        onOpenChange={(v) => {
          if (!v) setDecision(null);
        }}
      >
        <DialogContent className="decision-dialog">
          <DialogHeader>
            <DialogTitle>{decision && labels[decision]}</DialogTitle>
            <DialogDescription>
              {decision === "resolve"
                ? "Save a snapshot of the evidence, decisions and current outcome. Resolving an incident does not prove the change succeeded."
                : decision === "approve"
                  ? "You are approving this exact plan diff and target. All actions here are simulated."
                  : decision === "request_rollback"
                    ? "Rollback is a separate decision. Preconditions and safety are checked again."
                    : "The reason is recorded in the incident history."}
            </DialogDescription>
          </DialogHeader>
          <form
            className="form-stack"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            {(decision === "approve" || decision === "request_rollback") &&
              decisionPlan && (
                <>
                  <Diff
                    rows={decisionPlan.diff}
                    inverse={decision === "request_rollback"}
                  />
                  <p className="text-xs mono break-all">
                    {decisionPlan.hash} · {decisionPlan.targetUid}
                  </p>
                  {decision === "request_rollback" && (
                    <p className="muted">
                      Do not roll back after expiry, when the target has changed, or when the old configuration is
                      unsafe.
                    </p>
                  )}
                  {decision === "approve" && (
                    <details>
                      <summary>Simulate a conflict before approval</summary>
                      <div className="flex gap-2 mt-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => act({ type: "stale_plan" })}
                        >
                          Simulate target change
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => act({ type: "expire_plan" })}
                        >
                          Simulate expiry
                        </Button>
                      </div>
                      <StatusBadge status={decisionPlan.status} />
                    </details>
                  )}
                </>
              )}
            {decision === "resolve" && (
              <div className="content-panel">
                <p>{i.title}</p>
                <p>
                  Run: {i.run.id} · {i.run.status}
                </p>
                <p>
                  Outcome: <StatusBadge status={i.execution.status} />
                </p>
                <p>
                  {i.execution.outcomeUnknown
                    ? "Write outcome unknown. The target remains locked after resolution."
                    : i.execution.summary}
                </p>
                <p>
                  {i.notes.length} notes · {i.run.evidence.length} evidence
                </p>
              </div>
            )}
            {decision === "verify_manually" && (
              <>
                <Label htmlFor="manual-evidence">
                  Fresh verification evidence
                </Label>
                <Textarea
                  id="manual-evidence"
                  required
                  value={manualEvidence}
                  onChange={(e) => setManualEvidence(e.target.value)}
                  placeholder="UID/resourceVersion, observed values, source and timestamp…"
                />
                <Label htmlFor="manual-outcome">Observed outcome</Label>
                <select
                  id="manual-outcome"
                  className="native-select"
                  value={outcome}
                  onChange={(e) => setOutcome(e.target.value as typeof outcome)}
                >
                  <option value="applied">Desired change is present</option>
                  <option value="not_applied">
                    Desired change is not present
                  </option>
                </select>
                <label className="flex gap-3 items-start">
                  <Checkbox
                    checked={stopped}
                    onCheckedChange={(v) => setStopped(v === true)}
                  />
                  I verified that the old write request has stopped and cannot write late.
                </label>
              </>
            )}
            <Label htmlFor="decision-reason">
              Reason {decision === "approve" ? "(optional)" : "(required)"}
            </Label>
            <Textarea
              id="decision-reason"
              value={reason}
              required={decision !== "approve"}
              minLength={decision === "approve" ? undefined : 3}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Describe the decision and next steps…"
            />
            <div className="dialog-actions">
              <Button
                type="button"
                variant="outline"
                onClick={() => setDecision(null)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  readonly || (decision === "verify_manually" && !stopped)
                }
              >
                {decision && labels[decision]}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="check-list">
      {items.map((s, index) => (
        <li key={index}>{s}</li>
      ))}
    </ul>
  );
}
function Diff({
  rows,
  inverse = false,
}: {
  rows: { field: string; before: string; after: string }[];
  inverse?: boolean;
}) {
  return (
    <div className="diff-table">
      <table>
        <thead>
          <tr>
            <th>Field</th>
            <th>Current</th>
            <th>{inverse ? "Restore" : "Proposed"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.field}>
              <td>{d.field}</td>
              <td className="muted">{inverse ? d.after : d.before}</td>
              <td>{inverse ? d.before : d.after}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function MetricOverview({
  run,
  pattern,
}: {
  run: InvestigationRun;
  pattern: string;
}) {
  const metric = run.evidence.find((e) => e.id.endsWith("E3"));
  if (!metric || metric.status !== "available")
    return (
      <div className="metric-card">
        <h3>Insufficient telemetry</h3>
        <p className="muted">
          Missing data is not shown as a zero line. Open Evidence to see
          what could not be collected.
        </p>
      </div>
    );
  const series =
    pattern === "OOM"
      ? [
          {
            name: "Memory",
            unit: "MiB",
            values: [170, 215, 255, 180],
            max: 300,
          },
          {
            name: "Container restarts",
            unit: "count",
            values: [0, 0, 1, 2],
            max: 3,
          },
        ]
      : pattern === "Load"
        ? [
            {
              name: "Traffic",
              unit: "req/s",
              values: [40, 90, 160, 180],
              max: 200,
            },
            {
              name: "p95 latency",
              unit: "ms",
              values: [110, 180, 560, 820],
              max: 1000,
            },
          ]
        : [
            {
              name: "Container restarts",
              unit: "count",
              values: [0, 2, 4, 6],
              max: 8,
            },
            {
              name: "Ready replicas",
              unit: "count",
              values: [1, 1, 0, 0],
              max: 2,
            },
          ];
  return (
    <>
      <div className="metric-grid">
        {series.map((s) => (
          <div key={s.name} className="metric-card">
            <div className="panel-heading">
              <h3>{s.name}</h3>
              <span className="muted text-xs">{s.unit}</span>
            </div>
            <div className="text-2xl font-medium">
              {s.values.at(-1)}{" "}
              <small className="text-xs muted">{s.unit}</small>
            </div>
            <svg
              className="metric-chart"
              viewBox="0 0 280 90"
              role="img"
              aria-label={`${s.name}: ${s.values.join(", ")} ${s.unit}, 10:30 to 10:45 UTC+7`}
            >
              <path
                d="M0 75H280 M0 40H280 M0 5H280"
                stroke="currentColor"
                opacity=".12"
              />
              <polyline
                points={s.values
                  .map((v, n) => `${n * 90 + 5},${75 - (v / s.max) * 65}`)
                  .join(" ")}
                fill="none"
                stroke="var(--chart-2)"
                strokeWidth="2.5"
              />
              {s.values.map((v, n) => (
                <circle
                  key={n}
                  cx={n * 90 + 5}
                  cy={75 - (v / s.max) * 65}
                  r="3"
                  fill="var(--chart-2)"
                />
              ))}
            </svg>
            <div className="flex justify-between muted text-xs">
              <span>10:30</span>
              <span>10:45 UTC+7</span>
            </div>
          </div>
        ))}
      </div>
      <p className="text-xs muted mt-2">
        Illustrative demo fixture data · {metric.id}. Snapshot window: {run.window}.
      </p>
    </>
  );
}
