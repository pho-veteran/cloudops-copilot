import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";

type Candidate = components["schemas"]["CandidateView"];

export function Baselines({ id, canMutate, selectedBaseline }: {
  id: string; canMutate: boolean; selectedBaseline?: string;
}) {
  const cache = useQueryClient();
  const intent = useRef(0);
  const [draft, setDraft] = useState<{ value: Candidate; received: number }>();
  const [clock, setClock] = useState(() => performance.now());
  const select = (baseline?: string) => {
    intent.current += 1;
    setDraft(undefined);
    window.location.hash = `#/workloads?workload=${id}${baseline ? `&baseline=${baseline}` : ""}`;
  };
  useEffect(() => {
    const timer = window.setInterval(() => setClock(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => { intent.current += 1; setDraft(undefined); }, [selectedBaseline, canMutate]);
  const saved = useInfiniteQuery({
    queryKey: ["baselines", id], initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => unwrap(await api.GET("/api/workloads/{workload_id}/baselines", {
      params: { path: { workload_id: id }, query: { cursor: pageParam } },
    })),
    getNextPageParam: page => page.next_cursor || undefined,
  });
  const context = useQuery({
    queryKey: ["recovery-context", id, selectedBaseline],
    queryFn: async () => unwrap(await api.GET("/api/workloads/{workload_id}/recovery-context", {
      params: { path: { workload_id: id }, query: { baseline_id: selectedBaseline } },
    })), retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    staleTime: 30_000, refetchOnMount: true,
  });
  const preview = useMutation({
    mutationFn: async (_generation: number) => unwrap(await api.GET("/api/workloads/{workload_id}/baseline-candidate", {
      params: { path: { workload_id: id } },
    })),
    onMutate: () => { setDraft(undefined); confirm.reset(); },
    onSuccess: (value, generation) => { if (generation === intent.current) setDraft({ value, received: performance.now() }); },
  });
  const confirm = useMutation({
    mutationFn: async ({ candidate }: { candidate: Candidate; generation: number }) => unwrap(await api.POST("/api/workloads/{workload_id}/baseline", {
      params: { path: { workload_id: id } }, body: { candidate_id: candidate.id, digest: candidate.digest },
    })),
    onMutate: () => setDraft(undefined),
    onSuccess: (value, { generation }) => {
      void cache.invalidateQueries({ queryKey: ["baselines", id] });
      void cache.invalidateQueries({ queryKey: ["recovery-context", id] });
      if (generation === intent.current) select(value.id);
    },
  });
  // Use server time plus monotonic elapsed time, independent of the device clock.
  const now = draft ? Date.parse(draft.value.server_time) + Math.max(clock, draft.received) - draft.received : 0;
  const wait = draft ? Math.max(0, Math.ceil((Date.parse(draft.value.confirm_after) - now) / 1000)) : 0;
  const expired = !!draft && now >= Date.parse(draft.value.expires_at);
  return <div className="baseline-preparation foundation-stack min-w-0">
    <h3>Known-good preparation</h3>
    <p className="muted">Two fresh complete healthy observations, five seconds to five minutes apart. Save declared image and CPU/memory metadata with provenance. Replicas are context only. Referenced contents, unsupported template fields and application health remain unverified. Preparation creates no execution authority.</p>
    <Button variant="outline" disabled={preview.isPending || confirm.isPending} onClick={() => preview.mutate(++intent.current)}>Inspect live baseline candidate</Button>
    {preview.isError && <Failure error={preview.error} />}
    {draft && <>
      <p className="mono">Candidate digest: {draft.value.digest}</p>
      <details><summary>Inspect candidate and first observation</summary><pre>{JSON.stringify(draft.value.snapshot, null, 2)}</pre></details>
      {expired ? <p role="status">Candidate expired. Inspect a fresh candidate.</p> : canMutate ? <Button disabled={wait > 0 || confirm.isPending} onClick={() => confirm.mutate({ candidate: draft.value, generation: intent.current })}>Confirm unchanged healthy baseline{wait > 0 ? ` (wait ${wait}s)` : ""}</Button> : <p>Viewer access: an Operator must inspect and confirm their own candidate.</p>}
    </>}
    {confirm.isError && <><Failure error={confirm.error} /><p>Inspect a fresh candidate before trying again.</p></>}
    {confirm.isSuccess && <p role="status">Baseline confirmed and saved.</p>}
    <h4>Confirmed history</h4>
    {saved.isError && <Failure error={saved.error} />}
    {saved.data?.pages.flatMap(page => page.items).map(b => <div key={b.id}>
      <Button variant="outline" onClick={() => select(b.id)} aria-pressed={selectedBaseline === b.id}>Inspect {b.profile} · {new Date(b.confirmed_at).toLocaleString()}</Button>
      <p className="mono">{b.id} · {b.digest}</p><p>Confirmed by {b.confirmed_by}; age at read {Math.floor(b.age_seconds)}s.</p>
    </div>)}
    {saved.hasNextPage && <Button variant="outline" disabled={saved.isFetchingNextPage} onClick={() => void saved.fetchNextPage()}>Load older baselines</Button>}
    <h4>Recovery baseline context</h4>
    {selectedBaseline && <Button variant="outline" disabled={context.isFetching} onClick={() => void context.refetch()}>Refresh selected recovery context</Button>}
    {!selectedBaseline && <p>No baseline selected. Choose an exact confirmed record; none is selected automatically.</p>}
    {selectedBaseline && <><p className="mono">Selected: {selectedBaseline}</p><Button variant="outline" onClick={() => select()}>Clear selection</Button></>}
    {context.isPending && <p role="status">Checking recovery context…</p>}
    {context.isError && <Failure error={context.error} />}
    {context.data && !context.isError && <><p role="status">{context.data.state}: {context.data.reason.replaceAll("_", " ")}. Execution unavailable.</p><p className="muted">Context response received {new Date(context.dataUpdatedAt).toLocaleString()}. Refresh to check current live identity again.</p><p>{context.data.limitations}</p><p>Inspect owned health and related incidents above. Investigate or repair manually when this revision is broken or cannot be verified; inspect a fresh healthy candidate after recovery.</p>
      {context.data.baseline && <details><summary>Selected saved record and provenance</summary><pre>{JSON.stringify(context.data.baseline, null, 2)}</pre></details>}
      {!!context.data.supported_diff?.length && <details open><summary>Declared supported field differences</summary><pre>{JSON.stringify(context.data.supported_diff, null, 2)}</pre></details>}
    </>}
  </div>;
}
