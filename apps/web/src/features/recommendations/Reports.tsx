import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { download } from "@/lib/download";
import { useWorkloads } from "@/features/workloads/queries";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

export function Reports({ canMutate }: { canMutate: boolean }) {
  const cache = useQueryClient();
  const workloads = useWorkloads();
  const [selected, setSelected] = useState("");
  const reports = useQuery({
    queryKey: ["recommendation-reports"],
    queryFn: async () => unwrap(await api.GET("/api/recommendation-reports")),
    refetchInterval: 2000,
  });
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/recommendation-reports", {
          body: { workload_id: selected, requested_hours: 6 },
        }),
      ),
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: ["recommendation-reports"] }),
  });
  const exporting = useMutation({
    mutationFn: async (id: string) => {
      const content = unwrap(
        await api.GET("/api/recommendation-reports/{report_id}/export", {
          params: { path: { report_id: id } },
          parseAs: "text",
        }),
      );
      download(content, `resource-report-${id}.json`, "application/json");
    },
  });
  return (
    <section className="foundation-stack">
      <h1>Resource reports</h1>
      <p>
        Read-only KRR results. Missing or unqualified representative history
        produces explicit unavailable advice; reports never apply a change.
      </p>
      {canMutate ? (
        <>
          <Label htmlFor="report-workload">Configured live workload</Label>
          <select
            id="report-workload"
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
          >
            <option value="">Choose a workload</option>
            {workloads.data?.items
              .filter((w) => w.source === "live_kubernetes")
              .map((w) => (
                <option key={w.id} value={w.id}>
                  {w.namespace}/{w.name}
                </option>
              ))}
          </select>
          <Button
            disabled={!selected || create.isPending}
            onClick={() => create.mutate()}
          >
            Generate six-hour KRR report
          </Button>
        </>
      ) : (
        <p>
          Viewer access: inspect and export existing reports. An Operator must
          start generation.
        </p>
      )}
      {create.isError && <Failure error={create.error} />}
      {reports.isError && <Failure error={reports.error} />}
      {exporting.isError && <Failure error={exporting.error} />}
      {reports.data?.map((r) => (
        <article className="foundation-panel" key={r.id}>
          <h2>{r.state.replaceAll("_", " ")}</h2>
          <p>{r.reason}</p>
          <p>
            {r.tool_version} · requested {r.requested_hours} hours ·{" "}
            {new Date(r.created_at).toLocaleString()}
          </p>
          <pre>{JSON.stringify(r.result, null, 2)}</pre>
          <Button
            variant="outline"
            disabled={exporting.isPending}
            onClick={() => exporting.mutate(r.id)}
          >
            Export report {r.id.slice(0, 8)}
          </Button>
        </article>
      ))}
    </section>
  );
}
