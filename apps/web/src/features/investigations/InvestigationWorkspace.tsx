import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { incidentKeys, useIncident } from "@/features/incidents/queries";
import { runKeys, useRun, useRuns } from "./queries";
import {
  defaultWindow,
  describeOrigin,
  describeQuestion,
  describeWindow,
  requestKey,
  runStateLabel,
  settleKey,
  toSpec,
  windowProblem,
  WindowFields,
  type WindowChoice,
} from "./request";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Failure, Loading } from "@/app/Feedback";
import { IncidentActivity } from "@/features/incidents/IncidentActivity";
import { AlertSignal } from "@/features/incidents/AlertSignal";
import { RecoveryPlans } from "@/features/recovery/RecoveryPlans";
import { WorkloadIdentity } from "@/features/workloads/WorkloadIdentity";
import { buildIncidentHash, incidentReturnHash, safeRoute } from "@/app/routes";
import { EvidenceCatalog } from "./EvidenceCatalog";
import { RunComparison } from "./RunComparison";
import { CancelRun, CollectionProgress } from "./RunProgress";
import { RunGuidance } from "@/features/knowledge/RunGuidance";
import { AnalysisPanel } from "@/features/diagnosis/Analysis";

function RequestInvestigation({
  incidentId,
  workloadId,
}: {
  incidentId: string;
  workloadId: string;
}) {
  const cache = useQueryClient();
  const [reason, setReason] = useState("");
  const [timeWindow, setTimeWindow] = useState<WindowChoice>(defaultWindow);
  const [reviewing, setReviewing] = useState(false);
  const scope = `run:${incidentId}`;
  const body = { workload_id: workloadId, reason, window: toSpec(timeWindow) };
  const mutation = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/incidents/{incident_id}/runs", {
          params: {
            path: { incident_id: incidentId },
            header: { "idempotency-key": requestKey(scope, body) },
          },
          body,
        }),
      ),
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: runKeys.all(incidentId) }),
    onSettled: (_data, error) => settleKey(scope, error ?? undefined),
  });
  const problem = windowProblem(timeWindow);
  if (reviewing)
    return (
      <section
        className="foundation-panel foundation-stack"
        aria-labelledby="review-request"
      >
        <h2 id="review-request">Review the investigation request</h2>
        <dl className="foundation-metadata">
          <dt>Reason</dt>
          <dd className="whitespace-pre-wrap">{reason}</dd>
          <dt>Window</dt>
          <dd>
            {timeWindow.mode === "preset"
              ? `Last ${timeWindow.preset} minutes, fixed in UTC when the server accepts the request`
              : `${new Date(timeWindow.start).toLocaleString()} → ${new Date(timeWindow.end).toLocaleString()}`}
          </dd>
        </dl>
        <p className="muted">
          A new Run keeps the incident text and earlier investigations. If the
          collection queue is full the request is refused; nothing is replayed
          automatically.
        </p>
        {mutation.isError && <Failure error={mutation.error} />}
        <div className="foundation-actions">
          <Button
            disabled={mutation.isPending}
            onClick={() =>
              // Navigation only while this view is mounted; leaving it never pulls the user back.
              mutation.mutate(undefined, {
                onSuccess: (result) => {
                  setReviewing(false);
                  setReason("");
                  window.location.hash = buildIncidentHash(
                    incidentId,
                    result.run_id,
                    incidentReturnHash(window.location.hash),
                    safeRoute(window.location.hash).resolution,
                    "", safeRoute(window.location.hash).selection,
                  );
                },
              })
            }
          >
            {mutation.isPending ? "Submitting…" : "Submit request"}
          </Button>
          <Button
            variant="outline"
            disabled={mutation.isPending}
            onClick={() => setReviewing(false)}
          >
            Edit request
          </Button>
        </div>
      </section>
    );
  return (
    <form
      className="foundation-panel foundation-stack"
      aria-labelledby="request-investigation"
      onSubmit={(event) => {
        event.preventDefault();
        setReviewing(true);
      }}
    >
      <h2 id="request-investigation">Request a new investigation</h2>
      <Label htmlFor="run-reason">Why is a new investigation needed?</Label>
      <Textarea
        id="run-reason"
        required
        maxLength={2000}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <WindowFields id="run" value={timeWindow} onChange={setTimeWindow} />
      <div className="foundation-actions">
        <Button type="submit" disabled={!reason.trim() || Boolean(problem)}>
          Review request
        </Button>
      </div>
    </form>
  );
}

export function InvestigationWorkspace({
  id,
  selectedRun,
  selectedEvidence = "",
  selectedResolution,
  canMutate,
}: {
  id: string;
  selectedRun: string;
  selectedEvidence?: string;
  selectedResolution: string;
  canMutate: boolean;
}) {
  const incident = useIncident(id);
  const [offset, setOffset] = useState(0);
  const latest = useRuns(id);
  const history = useRuns(id, offset);
  const runId = selectedRun || latest.data?.items[0]?.id || "";
  const current = useRun(id, runId);
  if (incident.isPending || latest.isPending) return <Loading />;
  if (incident.isError)
    return (
      <Failure error={incident.error} retry={() => void incident.refetch()} />
    );
  if (latest.isError)
    return <Failure error={latest.error} retry={() => void latest.refetch()} />;
  const busy = latest.data.items.some(
    (r) =>
      !["insufficient_evidence", "completed", "failed", "cancelled"].includes(
        r.state,
      ),
  );
  const resolved = incident.data.state === "resolved";
  const canRequest =
    canMutate && !busy && !resolved && incident.data.workload.active;
  return (
    <section>
      <a href={incidentReturnHash(window.location.hash)}>← Work queue</a>
      <div className="eyebrow mt-5">
        INCIDENT · {incident.data.state.toUpperCase()} ·{" "}
        {describeOrigin(incident.data.origin).toUpperCase()}
      </div>
      <h1>{incident.data.title}</h1>
      <p className="muted">
        {incident.data.workload.namespace}/{incident.data.workload.name} ·{" "}
        {incident.data.id}
      </p>
      {incident.data.description && (
        <p className="whitespace-pre-wrap">{incident.data.description}</p>
      )}
      <AlertSignal incident={incident.data} />
      <p className="muted">
        Workload rollout observations are tracked separately from this
        investigation evidence.{" "}
        <a href={`#/workloads?workload=${incident.data.workload.id}`}>
          View workload context
        </a>
      </p>
      <WorkloadIdentity workload={incident.data.workload} />
      {canRequest ? (
        <RequestInvestigation
          key={latest.data.items[0]?.id ?? "first"}
          incidentId={id}
          workloadId={incident.data.workload.id}
        />
      ) : (
        <div className="foundation-actions">
          {!canMutate && <p className="muted">Viewer access · read-only</p>}
          {canMutate && busy && (
            <p className="muted">
              An investigation is still active. Cancel it while that is still
              safe, or wait for its result before requesting another.
            </p>
          )}
          {canMutate && resolved && (
            <p className="muted">
              Reopen this incident before requesting a new investigation.
            </p>
          )}
          {!incident.data.workload.active && (
            <p className="muted">
              This workload identity no longer admits new work. Its history
              remains readable; inspect the identity for its retirement status.
            </p>
          )}
        </div>
      )}
      <div className="foundation-workspace">
        <aside className="foundation-panel">
          <h2>Run history</h2>
          {history.isError ? (
            <Failure
              error={history.error}
              retry={() => void history.refetch()}
            />
          ) : history.isPending ? (
            <Loading />
          ) : (
            <>
              {!history.data.items.length && <p>No investigations saved.</p>}
              <ol className="foundation-stack">
                {history.data.items.map((r) => (
                  <li key={r.id}>
                    <a
                      aria-current={r.id === runId ? "page" : undefined}
                      href={buildIncidentHash(
                        id,
                        r.id,
                        incidentReturnHash(window.location.hash),
                        selectedResolution,
                        "", safeRoute(window.location.hash).selection,
                      )}
                    >
                      <span>{runStateLabel(r)}</span>
                      <br />
                      <small>{new Date(r.created_at).toLocaleString()}</small>
                      <br />
                      <small className="mono">{r.id}</small>
                    </a>
                  </li>
                ))}
              </ol>
              <div className="foundation-actions">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - 25))}
                >
                  Newer
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={history.data.next_offset === null}
                  onClick={() => setOffset(history.data!.next_offset!)}
                >
                  Older
                </Button>
              </div>
            </>
          )}
        </aside>
        <div className="foundation-stack" aria-live="polite">
          {!runId ? (
            <p>No investigation selected.</p>
          ) : current.isPending ? (
            <Loading />
          ) : current.isError ? (
            <Failure
              error={current.error}
              retry={() => void current.refetch()}
            />
          ) : (
            <>
              <article className="foundation-panel">
                <Badge variant="outline">{runStateLabel(current.data)}</Badge>
                <h2>Investigation</h2>
                <p className="mono">{current.data.id}</p>
                <p>{current.data.reason}</p>
                <dl className="foundation-metadata">
                  <dt>Question</dt>
                  <dd className="whitespace-pre-wrap">
                    {describeQuestion(current.data)}
                  </dd>
                  <dt>Window</dt>
                  <dd>{describeWindow(current.data.window)}</dd>
                  <dt>Requested by</dt>
                  <dd>{describeOrigin(current.data.origin)}</dd>
                </dl>
                <CancelRun
                  key={current.data.id}
                  incidentId={id}
                  run={current.data}
                  canMutate={canMutate}
                />
                <CollectionProgress
                  run={current.data}
                  onReinvestigate={
                    canRequest
                      ? () => document.getElementById("run-reason")?.focus()
                      : undefined
                  }
                />
                <p className="muted">
                  Stored diagnosis and live manual recovery are reviewed
                  separately.
                </p>
                <p className="muted">
                  Created: {new Date(current.data.created_at).toLocaleString()}
                  <br />
                  Finished:{" "}
                  {current.data.finished_at
                    ? new Date(current.data.finished_at).toLocaleString()
                    : "Not finished"}
                </p>
              </article>
              <AnalysisPanel
                key={`analysis:${current.data.id}`}
                incidentId={id}
                runId={current.data.id}
                canMutate={canMutate}
                onRecovery={() => document.getElementById("recovery-review")?.focus()}
              />
              {current.data.state !== "completed" &&
                latest.data.items.some(
                  (item) =>
                    item.id !== current.data.id && item.state === "completed",
                ) && (
                  <aside className="foundation-panel">
                    <h3>Earlier findings remain available</h3>
                    <p>
                      A newer waiting or failed investigation does not replace
                      an earlier result.
                    </p>
                    <ul>
                      {latest.data.items
                        .filter(
                          (item) =>
                            item.id !== current.data.id &&
                            item.state === "completed",
                        )
                        .map((item) => (
                          <li key={item.id}>
                            <a
                              href={buildIncidentHash(
                                id,
                                item.id,
                                incidentReturnHash(window.location.hash),
                                selectedResolution, "", safeRoute(window.location.hash).selection,
                              )}
                            >
                              Open historical findings · {item.id}
                            </a>
                          </li>
                        ))}
                    </ul>
                  </aside>
                )}
              <EvidenceCatalog
                incidentId={id}
                run={current.data}
                selectedEvidence={selectedEvidence}
                selectedResolution={selectedResolution}
              />
              <RunGuidance
                key={`guidance:${current.data.id}`}
                incidentId={id}
                runId={current.data.id}
                canMutate={canMutate}
              />
              <RunComparison
                key={`${runId}:${JSON.stringify(safeRoute(window.location.hash).selection)}`}
                incidentId={id}
                runId={current.data.id}
              />
              <article className="foundation-panel">
                <h2>Collection attempts</h2>
                <p className="muted">
                  At most 3 attempts per run, including source retries. A new
                  investigation preserves all earlier records.
                </p>
                {!current.data.attempts.length ? (
                  <p>
                    {current.data.state === "cancelled"
                      ? "Cancelled before collection; no attempt was made."
                      : "Waiting for the worker to claim this run."}
                  </p>
                ) : (
                  <ol className="foundation-stack">
                    {current.data.attempts.map((a) => (
                      <li key={a.number}>
                        <strong>
                          Attempt {a.number} · {a.state}
                        </strong>
                        <p className="muted">
                          {new Date(a.started_at).toLocaleString()}
                          {a.finished_at &&
                            ` → ${new Date(a.finished_at).toLocaleString()}`}
                          {a.sources_committed !== null &&
                            ` · ${a.sources_committed} ${a.sources_committed === 1 ? "source" : "sources"} saved`}
                        </p>
                        {a.error && <p>{a.error}</p>}
                      </li>
                    ))}
                  </ol>
                )}
              </article>
            </>
          )}
        </div>
      </div>
      <IncidentActivity
        id={id}
        state={incident.data.state}
        canMutate={canMutate}
        selectedResolution={selectedResolution}
        onSelectResolution={(resolution) => {
          window.location.hash = buildIncidentHash(
            id,
            selectedRun,
            incidentReturnHash(window.location.hash),
            resolution,
            selectedEvidence,
            safeRoute(window.location.hash).selection,
          );
        }}
      />
      <RecoveryPlans
        id={id}
        canMutate={canMutate}
        live={incident.data.workload.source === "live_kubernetes"}
        workloadId={incident.data.workload.id}
        runId={runId}
        latestRunId={latest.data.items[0]?.id}
        resolved={resolved}
        runBusy={busy}
        canInvestigate={canRequest}
      />
    </section>
  );
}
