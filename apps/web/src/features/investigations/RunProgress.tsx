import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { Button } from "@/components/ui/button";
import { Failure } from "@/app/Feedback";
import { runKeys } from "./queries";

type Run = components["schemas"]["RunView"];
type DispatchReason = NonNullable<
  NonNullable<Run["dispatch"]>["reason"]
>;
type CancelReason = Run["cancel"]["reason"];

const DISPATCH_REASONS: Record<DispatchReason, string> = {
  recovery_mode: "trusted recovery mode",
  class_denied: "collection is not resumed by trusted authority",
  permit_missing: "no trusted dispatch permit is installed",
  permit_stale: "the trusted dispatch permit expired or was superseded",
  permit_invalid: "the trusted dispatch permit is unreadable",
};

export const CANCEL_REASONS: Record<CancelReason, string> = {
  queued:
    "Cancelling stops any new attempt. Work already recorded remains in the history.",
  waiting_without_provider_call:
    "No provider call is in flight. Cancelling stops it before analysis.",
  collection_started:
    "Collection already started, so it cannot be cancelled. Committed sources remain part of the history.",
  analysis_started: "Analysis already started and cannot be withdrawn.",
  provider_call_in_flight:
    "A provider call is already in flight and cannot be withdrawn.",
  finished: "This investigation already finished.",
  already_cancelled: "This investigation was cancelled.",
};

const TERMINAL = ["insufficient_evidence", "completed", "failed", "cancelled"];

export function CollectionProgress({
  run,
  onReinvestigate,
}: {
  run: Run;
  onReinvestigate?: () => void;
}) {
  const progress = run.progress;
  const paused = run.dispatch?.status === "paused";
  return (
    <section aria-label="Collection progress" className="foundation-stack mt-4">
      <h3>Collection progress</h3>
      {progress ? (
        <p>
          {progress.sources_committed} of {progress.sources_total} catalog
          sources saved
          {progress.attempt !== null &&
            ` · Attempt ${progress.attempt} of ${progress.max_attempts}`}
          {progress.sources_reused > 0 &&
            ` · ${progress.sources_reused} reused from an earlier attempt`}
        </p>
      ) : (
        <p className="muted">
          Collection progress was not captured for this earlier run.
        </p>
      )}
      {run.state === "collecting" && !paused && (
        <p className="muted">
          Saved sources survive a worker restart. Another worker resumes the
          remaining sources after the 30-second lease; obsolete attempts cannot
          publish. Slots without an installed adapter are recorded as
          unavailable when collection finishes.
        </p>
      )}
      {paused && run.dispatch?.reason && (
        <p role="status">
          Dispatch is paused: {DISPATCH_REASONS[run.dispatch.reason]}.{" "}
          {run.state === "collecting"
            ? "The current attempt cannot save more sources; saved sources stay readable."
            : "This request stays queued and starts only after a trusted host operator resumes collection."}
        </p>
      )}
      {run.state === "failed" && (
        <div className="foundation-stack">
          <p>
            Collection stopped after {run.attempts.length}{" "}
            {run.attempts.length === 1 ? "attempt" : "attempts"}. Saved sources
            stay readable; request a new investigation to collect again.
          </p>
          {onReinvestigate && (
            <div className="foundation-actions">
              <Button variant="outline" onClick={onReinvestigate}>
                Request a new investigation
              </Button>
            </div>
          )}
        </div>
      )}
      {(run.state === "analyzing" || run.state === "waiting_for_quota") && (
        <p className="muted">
          AI analysis is unavailable in this release; this state comes from a
          declared contract.
        </p>
      )}
    </section>
  );
}

export function CancelRun({
  incidentId,
  run,
  canMutate,
}: {
  incidentId: string;
  run: Run;
  canMutate: boolean;
}) {
  const cache = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [outcome, setOutcome] = useState<"cancelled" | "raced" | null>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const outcomeText = useRef<HTMLParagraphElement>(null);
  const restoreFocus = useRef(false);
  const reasonId = `cancel-reason-${run.id}`;
  const mutation = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/incidents/{incident_id}/runs/{run_id}/cancel", {
          params: { path: { incident_id: incidentId, run_id: run.id } },
          body: {},
        }),
      ),
    onSuccess: async () => {
      setConfirming(false);
      setOutcome("cancelled");
      await cache.invalidateQueries({ queryKey: runKeys.all(incidentId) });
      await cache.invalidateQueries({ queryKey: ["activity", incidentId] });
    },
    onError: async (error) => {
      setConfirming(false);
      if (error instanceof ApiError && error.code === "run_not_cancellable") {
        setOutcome("raced");
        await cache.invalidateQueries({ queryKey: runKeys.all(incidentId) });
      }
    },
  });
  useEffect(() => {
    if (confirming) confirmButton.current?.focus();
    else if (restoreFocus.current) {
      restoreFocus.current = false;
      cancelButton.current?.focus();
    }
  }, [confirming]);
  useEffect(() => {
    if (outcome) outcomeText.current?.focus();
  }, [outcome]);
  const terminal = TERMINAL.includes(run.state);
  if (terminal && !outcome) return null;
  return (
    <div className="foundation-stack">
      {outcome && (
        <p ref={outcomeText} tabIndex={-1} role="status">
          {outcome === "cancelled"
            ? "Investigation cancelled. No further attempt will start."
            : "The investigation started before the cancellation; it was not cancelled."}
        </p>
      )}
      {!terminal && (
        <>
          <p id={reasonId} className="muted">
            {CANCEL_REASONS[run.cancel.reason]}
          </p>
          {canMutate &&
            (confirming ? (
              <div className="foundation-actions">
                <Button
                  ref={confirmButton}
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate()}
                >
                  {mutation.isPending ? "Cancelling…" : "Confirm cancellation"}
                </Button>
                <Button
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={() => {
                    restoreFocus.current = true;
                    setConfirming(false);
                  }}
                >
                  Keep it
                </Button>
              </div>
            ) : (
              <div className="foundation-actions">
                <Button
                  ref={cancelButton}
                  variant="outline"
                  disabled={!run.cancel.allowed}
                  aria-describedby={reasonId}
                  onClick={() => {
                    setOutcome(null);
                    setConfirming(true);
                  }}
                >
                  Cancel investigation
                </Button>
              </div>
            ))}
        </>
      )}
      {mutation.isError && outcome !== "raced" && (
        <Failure error={mutation.error} />
      )}
    </div>
  );
}
