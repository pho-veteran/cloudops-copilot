import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/generated/schema";
import { download } from "@/lib/download";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { HandoffReferences } from "@/features/investigations/HandoffReferences";

type HandoffContent = components["schemas"]["HandoffPreview"];
type ClipboardFeedback =
  | { source: HandoffContent; status: "pending" | "copied" }
  | { source: HandoffContent; status: "failed"; error: unknown };

export function IncidentActivity({
  id, state, canMutate, selectedResolution, onSelectResolution,
}: {
  id: string;
  state: string;
  canMutate: boolean;
  selectedResolution: string;
  onSelectResolution: (resolution: string) => void;
}) {
  const cache = useQueryClient();
  const [text, setText] = useState("");
  const [clipboard, setClipboard] = useState<ClipboardFeedback | null>(null);
  const previewHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (selectedResolution) previewHeading.current?.focus();
  }, [id, selectedResolution]);
  const activity = useInfiniteQuery({
    queryKey: ["activity", id],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => unwrap(await api.GET("/api/incidents/{incident_id}/activity", {
      params: { path: { incident_id: id }, query: { cursor: pageParam ?? undefined } },
    })),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const preview = useQuery({
    queryKey: ["handoff-preview", id, selectedResolution],
    queryFn: async () => unwrap(await api.GET("/api/incidents/{incident_id}/handoff/preview", {
      params: { path: { incident_id: id }, query: { resolution_id: selectedResolution || undefined } },
    })),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const clipboardMatchesPreview = clipboard !== null && preview.data !== undefined
    && clipboard.source.incident_id === preview.data.incident_id
    && clipboard.source.resolution_id === preview.data.resolution_id
    && clipboard.source.markdown === preview.data.markdown;
  const copyHandoff = async () => {
    const source = preview.data;
    if (!source || clipboard?.status === "pending") return;
    setClipboard({ source, status: "pending" });
    try {
      await navigator.clipboard.writeText(source.markdown);
      setClipboard({ source, status: "copied" });
    } catch (error) {
      setClipboard({ source, status: "failed", error });
    }
  };
  const save = useMutation({
    mutationFn: async (action: "note" | "acknowledge" | "resolve" | "reopen") => {
      if (action === "note") return unwrap(await api.POST("/api/incidents/{incident_id}/notes", {
        params: { path: { incident_id: id } }, body: { text },
      }));
      return unwrap(await api.POST("/api/incidents/{incident_id}/lifecycle", {
        params: { path: { incident_id: id } }, body: { action, reason: text },
      }));
    },
    onSuccess: async () => {
      setText("");
      await cache.invalidateQueries({ queryKey: ["handoff-preview", id, ""], exact: true });
      await cache.invalidateQueries({ queryKey: ["activity", id] });
      await cache.invalidateQueries({ queryKey: ["incidents"] });
      await cache.invalidateQueries({ queryKey: ["plans", id] });
    },
  });
  const select = (resolution: string) => {
    if (clipboard?.status !== "pending") setClipboard(null);
    onSelectResolution(resolution);
    if (!resolution) previewHeading.current?.focus();
  };
  return (
    <section className="foundation-panel foundation-stack" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
      <h2>Activity and handoff</h2>
      {canMutate && (
        <>
          <Label htmlFor="incident-note">Note or transition reason</Label>
          <Textarea id="incident-note" rows={3} maxLength={2000} value={text} onChange={(event) => setText(event.target.value)} />
          <div className="foundation-actions">
            <Button disabled={!text.trim() || save.isPending} onClick={() => save.mutate("note")}>Save note</Button>
            {state === "open" && <Button disabled={!text.trim() || save.isPending} onClick={() => save.mutate("acknowledge")}>Acknowledge</Button>}
            <Button variant="outline" disabled={!text.trim() || save.isPending} onClick={() => save.mutate(state === "resolved" ? "reopen" : "resolve")}>
              {state === "resolved" ? "Reopen incident" : "Resolve with snapshot"}
            </Button>
          </div>
        </>
      )}
      {save.isError && <Failure error={save.error} />}
      <section className="foundation-stack" aria-label="Handoff preview" aria-busy={preview.isFetching} style={{ minWidth: 0 }}>
        <h3 ref={previewHeading} tabIndex={-1}>{selectedResolution ? "Saved resolution handoff" : "Current handoff"}</h3>
        {!selectedResolution && <p>Preview reflects the last successful load. Refresh to include newer notes and investigations.</p>}
        {selectedResolution && <p>Resolution: {selectedResolution}</p>}
        <div className="foundation-actions">
          {selectedResolution && <Button variant="outline" onClick={() => select("")}>Show current handoff</Button>}
          <Button variant="outline" disabled={preview.isFetching} onClick={() => { if (clipboard?.status !== "pending") setClipboard(null); void preview.refetch(); }}>
            {preview.isFetching ? "Refreshing preview…" : "Refresh preview"}
          </Button>
        </div>
        {preview.isPending ? <Loading /> : preview.isError ? (
          <Failure error={preview.error} retry={() => void preview.refetch()} />
        ) : (
          <>
            <div className="foundation-actions">
              <Button variant="outline" disabled={clipboard?.status === "pending"} onClick={() => void copyHandoff()}>
                {clipboard?.status === "pending" ? "Copying handoff…" : "Copy handoff"}
              </Button>
              <Button variant="outline" onClick={() => download(preview.data!.markdown, `incident-${id}${selectedResolution ? `-${selectedResolution}` : ""}.md`, "text/markdown")}>Download handoff</Button>
            </div>
            <pre role="region" aria-label="Handoff Markdown content" tabIndex={0} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", minWidth: 0, maxHeight: "28rem", overflow: "auto", fontFamily: "inherit", fontSize: "1rem", lineHeight: 1.6 }}>{preview.data.markdown}</pre>
            <HandoffReferences incidentId={id} resolutionId={selectedResolution} snapshot={preview.data.snapshot} />
            {clipboardMatchesPreview && clipboard?.status === "copied" && <p role="status">Handoff copied.</p>}
            {clipboardMatchesPreview && clipboard?.status === "failed" && <Failure error={clipboard.error} />}
          </>
        )}
      </section>
      <section className="foundation-stack" aria-label="Incident activity">
        <h3>Activity history</h3>
        {activity.isPending ? <Loading /> : activity.isError && !activity.data ? (
          <Failure error={activity.error} retry={() => void activity.refetch()} />
        ) : (
          <>
            <p>{activity.data?.pages[0].total ?? 0} activities recorded.</p>
            {activity.isRefetchError && <Failure error={activity.error} retry={() => void activity.refetch()} />}
            {activity.data?.pages.flatMap((page) => page.items).map((a) => (
              <article key={a.id}>
                <h4>{a.kind.replaceAll("_", " ")} · {new Date(a.created_at).toLocaleString()}</h4>
                <p>Actor: {a.actor_name || a.actor_id || "Not recorded"}</p>
                <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{a.text}</p>
                {a.kind === "resolve" && (
                  <Button variant="outline" aria-pressed={a.id === selectedResolution} onClick={() => select(a.id)}>View saved resolution</Button>
                )}
              </article>
            ))}
            {activity.isFetchNextPageError && <Failure error={activity.error} retry={() => void activity.fetchNextPage()} />}
            {activity.hasNextPage && !activity.isFetchNextPageError && (
              <Button variant="outline" disabled={activity.isFetchingNextPage} onClick={() => void activity.fetchNextPage()}>
                {activity.isFetchingNextPage ? "Loading older activity…" : "Load older activity"}
              </Button>
            )}
          </>
        )}
      </section>
    </section>
  );
}
