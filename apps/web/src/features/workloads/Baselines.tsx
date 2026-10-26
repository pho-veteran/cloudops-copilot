import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";

export function Baselines({
  id,
  canMutate,
}: {
  id: string;
  canMutate: boolean;
}) {
  const cache = useQueryClient();
  const saved = useQuery({
    queryKey: ["baselines", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workloads/{workload_id}/baselines", {
          params: { path: { workload_id: id } },
        }),
      ),
  });
  const preview = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.GET("/api/workloads/{workload_id}/baseline-candidate", {
          params: { path: { workload_id: id } },
        }),
      ),
  });
  const confirm = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workloads/{workload_id}/baseline", {
          params: { path: { workload_id: id } },
          body: {
            candidate_id: preview.data!.id,
            digest: preview.data!.digest,
          },
        }),
      ),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["baselines", id] }),
  });
  return (
    <div className="foundation-stack">
      <h3>Known-good preparation</h3>
      <p className="muted">
        Two fresh healthy observations at least five seconds apart. Confirmation
        saves selected image/resource metadata and causes no cluster write.
      </p>
      <Button
        variant="outline"
        disabled={preview.isPending}
        onClick={() => preview.mutate()}
      >
        Inspect live baseline candidate
      </Button>
      {preview.isError && <Failure error={preview.error} />}
      {preview.data && (
        <>
          <p className="mono">Candidate digest: {preview.data.digest}</p>
          <pre>{JSON.stringify(preview.data.snapshot, null, 2)}</pre>
          {canMutate ? (
            <Button
              disabled={confirm.isPending}
              onClick={() => confirm.mutate()}
            >
              Confirm unchanged healthy baseline
            </Button>
          ) : (
            <p>Viewer access: an Operator must confirm the baseline.</p>
          )}
        </>
      )}
      {confirm.isError && <Failure error={confirm.error} />}
      {confirm.isSuccess && <p role="status">Baseline confirmed and saved.</p>}
      {saved.isError && <Failure error={saved.error} />}
      {saved.data?.map((b) => (
        <p key={b.id} className="mono">
          Confirmed {new Date(b.confirmed_at).toLocaleString()} · {b.digest}
        </p>
      ))}
    </div>
  );
}
