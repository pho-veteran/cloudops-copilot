import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { useWorkloads, useWorkload } from "@/features/workloads/queries";
import { incidentKeys } from "./queries";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Failure, Loading } from "@/app/Feedback";

export function CreateIncident({
  initialWorkload,
  onCreated,
}: {
  initialWorkload?: string;
  onCreated: (id: string, run: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const workloads = useWorkloads(offset);
  const [workloadId, setWorkloadId] = useState(initialWorkload ?? "");
  const selected = useWorkload(workloadId || undefined);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  // Keep the same key on an uncertain network retry; edited input gets a new key.
  const attempt = useRef<{ input: string; key: string } | null>(null);
  const cache = useQueryClient();
  const mutation = useMutation({
    mutationFn: async () => {
      const body = { workload_id: workloadId, title, description };
      const input = JSON.stringify(body);
      if (attempt.current?.input !== input)
        attempt.current = { input, key: crypto.randomUUID() };
      return unwrap(
        await api.POST("/api/incidents", {
          body,
          params: { header: { "idempotency-key": attempt.current.key } },
        }),
      );
    },
    onSuccess: async (result) => {
      await cache.invalidateQueries({ queryKey: incidentKeys.all });
      onCreated(result.incident_id, result.run_id);
    },
  });
  if (workloads.isPending) return <Loading />;
  if (workloads.isError)
    return (
      <Failure error={workloads.error} retry={() => void workloads.refetch()} />
    );
  if (!workloads.data.items.length && !workloadId)
    return (
      <p>
        No workloads are configured. Run the documented workload
        sync command.
      </p>
    );
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }
  return (
    <form onSubmit={submit} className="foundation-stack">
      <p>
        The investigation worker saves labeled synthetic evidence. Live workload
        observations remain separate; AI diagnosis is unavailable.
      </p>
      <Label htmlFor="workload">Configured workload</Label>
      <select
        id="workload"
        required
        value={workloadId}
        onChange={(e) => setWorkloadId(e.target.value)}
        disabled={mutation.isPending}
      >
        <option value="">Select a workload</option>
        {selected.data && !workloads.data.items.some(w => w.id === selected.data.id) && <option value={selected.data.id}>
          {selected.data.namespace}/{selected.data.name} · {selected.data.source === "live_kubernetes" ? "live" : "fixture"}{selected.data.active ? "" : " · retired"}
        </option>}
        {workloads.data.items.map((w) => (
          <option key={w.id} value={w.id}>
            {w.namespace}/{w.name} · {w.source === "live_kubernetes" ? "live" : "fixture"}
          </option>
        ))}
      </select>
      <div className="foundation-actions">
        <Button type="button" variant="outline" disabled={offset === 0 || mutation.isPending} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous workloads</Button>
        <Button type="button" variant="outline" disabled={workloads.data.next_offset === null || mutation.isPending} onClick={() => setOffset(workloads.data.next_offset!)}>Next workloads</Button>
      </div>
      {workloadId && selected.isPending && <Loading />}
      {selected.isError && <Failure error={selected.error} retry={() => void selected.refetch()} />}
      {selected.data && !selected.data.active && <p role="alert">This identity has left configured scope. Choose an active workload; its history remains available.</p>}
      <Label htmlFor="incident-title">Incident title</Label>
      <Input
        id="incident-title"
        required
        maxLength={160}
        value={title}
        disabled={mutation.isPending}
        onChange={(e) => setTitle(e.target.value)}
      />
      <Label htmlFor="description">Description (optional)</Label>
      <Textarea
        id="description"
        maxLength={4000}
        value={description}
        disabled={mutation.isPending}
        onChange={(e) => setDescription(e.target.value)}
      />
      {mutation.isError && <Failure error={mutation.error} />}
      <Button
        type="submit"
        disabled={mutation.isPending || !title.trim() || !workloadId || selected.isPending || selected.isError || !selected.data?.active}
      >
        {mutation.isPending
          ? "Creating…"
          : "Create incident and collect evidence"}
      </Button>
    </form>
  );
}
