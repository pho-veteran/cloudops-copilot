import { useEffect, useState } from "react";
import type { components } from "@/lib/api/generated/schema";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function RecoveryPlans({
  id,
  canMutate,
  live,
}: {
  id: string;
  canMutate: boolean;
  live: boolean;
}) {
  const cache = useQueryClient();
  const [replicas, setReplicas] = useState(2);
  const [rationale, setRationale] = useState("");
  const [approval, setApproval] = useState<
    components["schemas"]["ReviewedPlan"] | null
  >(null);
  const plans = useQuery({
    queryKey: ["plans", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/plans", {
          params: { path: { incident_id: id } },
        }),
      ),
    refetchInterval: 5000,
  });
  const planId = plans.data?.[0]?.id || "";
  const review = useQuery({
    queryKey: ["plan-review", planId],
    enabled: Boolean(planId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/plans/{plan_id}", {
          params: { path: { plan_id: planId } },
        }),
      ),
    refetchInterval: 30000,
  });
  const executions = useQuery({
    queryKey: ["executions", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/executions", {
          params: { path: { incident_id: id } },
        }),
      ),
    refetchInterval: 2000,
  });
  const refresh = async () => {
    await cache.invalidateQueries({ queryKey: ["plans", id] });
    await cache.invalidateQueries({ queryKey: ["plan-review"] });
    await cache.invalidateQueries({ queryKey: ["executions", id] });
  };
  const propose = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/incidents/{incident_id}/plans", {
          params: { path: { incident_id: id } },
          body: { replicas, rationale },
        }),
      ),
    onSuccess: refresh,
  });
  const decide = useMutation({
    mutationFn: async ({
      decision,
      receipt: value,
    }: {
      decision: "approve" | "reject";
      receipt: components["schemas"]["ReviewedPlan"];
    }) => {
      return unwrap(
        await api.POST("/api/plans/{plan_id}/decision", {
          params: { path: { plan_id: value.plan.id } },
          body: {
            version: value.plan.version,
            digest: value.plan.digest,
            review_token: value.review_token,
            decision,
          },
        }),
      );
    },
    onSettled: () => setApproval(null),
    onSuccess: refresh,
  });
  const plan = review.data?.plan;
  useEffect(
    () => setApproval(null),
    [
      plan?.id,
      plan?.version,
      plan?.digest,
      plan?.state,
      review.data?.valid,
      review.isError,
    ],
  );
  const available =
    review.data?.valid &&
    plan?.state === "proposed" &&
    new Date(plan.expires_at).getTime() > Date.now() &&
    !review.isFetching &&
    !review.isError;
  return (
    <section className="foundation-panel foundation-stack">
      <h2>Recovery review</h2>
      <p>
        Operator-proposed absolute scale · live Kubernetes prerequisites · no
        model diagnosis. Other recovery templates remain unavailable.
      </p>
      {canMutate && live && (
        <form
          className="foundation-stack"
          onSubmit={(event) => {
            event.preventDefault();
            propose.mutate();
          }}
        >
          <Label htmlFor="recovery-replicas">Desired replicas (1–3)</Label>
          <Input
            id="recovery-replicas"
            type="number"
            min={1}
            max={3}
            value={replicas}
            onChange={(event) => setReplicas(Number(event.target.value))}
            required
          />
          <Label htmlFor="recovery-rationale">
            Reason for the proposed change
          </Label>
          <Input
            id="recovery-rationale"
            maxLength={2000}
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            required
          />
          <Button disabled={propose.isPending || !rationale.trim()}>
            Prepare live scale plan
          </Button>
        </form>
      )}
      {!live && (
        <p>
          Synthetic investigation evidence cannot establish an executable
          target. Configure a live allowlisted workload.
        </p>
      )}
      {!canMutate && (
        <p>
          Viewer access: inspect plans and execution history. An Operator must
          propose or decide a change.
        </p>
      )}
      {propose.isError && <Failure error={propose.error} />}
      {plans.isError && <Failure error={plans.error} />}
      {review.isError && (
        <Failure error={review.error} retry={() => void review.refetch()} />
      )}
      {plan && (
        <>
          <h3>
            Plan version {plan.version} · {plan.state}
          </h3>
          <p className="mono">
            {plan.id} · {plan.digest}
          </p>
          <p>Expires {new Date(plan.expires_at).toLocaleString()}</p>
          <pre>{JSON.stringify(plan.content, null, 2)}</pre>
          <p>{review.data?.reason}</p>
          {canMutate && (
            <div className="foundation-actions">
              <Button
                disabled={!available || decide.isPending}
                onClick={() => setApproval(review.data!)}
              >
                Review approval
              </Button>
              <Button
                variant="outline"
                disabled={!available || decide.isPending}
                onClick={() =>
                  decide.mutate({ decision: "reject", receipt: review.data! })
                }
              >
                Reject exact plan
              </Button>
            </div>
          )}
          {!available && (
            <p>
              Read the current validity before acting. Expired, stale or
              invalidated plans require a new investigation and a new explicit
              proposal.
            </p>
          )}
        </>
      )}
      {decide.isError && <Failure error={decide.error} />}
      {executions.isError && <Failure error={executions.error} />}
      {executions.data?.map((e) => (
        <article key={e.id}>
          <h3>Execution · {e.state.replaceAll("_", " ")}</h3>
          <p>
            {e.holds_lock
              ? "Target lock retained. Trusted deployment maintenance and safety proof are required; no application role can override it."
              : "No active application target lock."}
          </p>
          <pre>{JSON.stringify(e.result, null, 2)}</pre>
        </article>
      ))}
      <Dialog
        open={Boolean(approval)}
        onOpenChange={(open) => {
          if (!open) setApproval(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Approve the exact plan</DialogTitle>
            <DialogDescription>
              Approve only the displayed version and digest. A session change
              discards this intent; after sign-in the plan must be read again
              and approved explicitly.
            </DialogDescription>
          </DialogHeader>
          <p className="mono">{approval?.plan.digest}</p>
          <Button
            disabled={
              !available ||
              decide.isPending ||
              approval?.plan.id !== plan?.id ||
              approval?.plan.digest !== plan?.digest
            }
            onClick={() => {
              if (approval)
                decide.mutate({ decision: "approve", receipt: approval });
            }}
          >
            Approve this version and dispatch
          </Button>
        </DialogContent>
      </Dialog>
    </section>
  );
}
