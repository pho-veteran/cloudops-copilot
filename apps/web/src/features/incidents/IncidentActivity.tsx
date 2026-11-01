import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { download } from "@/lib/download";
import { Failure } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function IncidentActivity({
  id,
  state,
  canMutate,
}: {
  id: string;
  state: string;
  canMutate: boolean;
}) {
  const cache = useQueryClient();
  const [text, setText] = useState("");
  const activity = useQuery({
    queryKey: ["activity", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/incidents/{incident_id}/activity", {
          params: { path: { incident_id: id } },
        }),
      ),
  });
  const save = useMutation({
    mutationFn: async (
      action: "note" | "acknowledge" | "resolve" | "reopen",
    ) => {
      if (action === "note")
        return unwrap(
          await api.POST("/api/incidents/{incident_id}/notes", {
            params: { path: { incident_id: id } },
            body: { text },
          }),
        );
      return unwrap(
        await api.POST("/api/incidents/{incident_id}/lifecycle", {
          params: { path: { incident_id: id } },
          body: { action, reason: text },
        }),
      );
    },
    onSuccess: async () => {
      setText("");
      await cache.invalidateQueries({ queryKey: ["activity", id] });
      await cache.invalidateQueries({ queryKey: ["incidents"] });
      await cache.invalidateQueries({ queryKey: ["plans", id] });
    },
  });
  const exporting = useMutation({
    mutationFn: async (resolution?: string) => {
      const content = unwrap(
        await api.GET("/api/incidents/{incident_id}/handoff", {
          params: {
            path: { incident_id: id },
            query: { resolution_id: resolution },
          },
          parseAs: "text",
        }),
      );
      download(content, `incident-${id}.md`, "text/markdown");
    },
  });
  return (
    <section className="foundation-panel foundation-stack">
      <h2>Activity and handoff</h2>
      {canMutate && (
        <>
          <Label htmlFor="incident-note">Note or transition reason</Label>
          <Input
            id="incident-note"
            maxLength={2000}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="foundation-actions">
            <Button
              disabled={!text.trim() || save.isPending}
              onClick={() => save.mutate("note")}
            >
              Save note
            </Button>
            {state === "open" && (
              <Button
                disabled={!text.trim() || save.isPending}
                onClick={() => save.mutate("acknowledge")}
              >
                Acknowledge
              </Button>
            )}
            <Button
              variant="outline"
              disabled={!text.trim() || save.isPending}
              onClick={() =>
                save.mutate(state === "resolved" ? "reopen" : "resolve")
              }
            >
              {state === "resolved"
                ? "Reopen incident"
                : "Resolve with snapshot"}
            </Button>
          </div>
        </>
      )}
      <Button
        variant="outline"
        disabled={exporting.isPending}
        onClick={() => exporting.mutate(undefined)}
      >
        Export current handoff
      </Button>
      {save.isError && <Failure error={save.error} />}
      {exporting.isError && <Failure error={exporting.error} />}
      {activity.isError && <Failure error={activity.error} />}
      {activity.data?.map((a) => (
        <article key={a.id}>
          <h3>
            {a.kind.replaceAll("_", " ")} · {new Date(a.created_at).toLocaleString()}
          </h3>
          <p className="whitespace-pre-wrap">{a.text}</p>
          {a.kind === "resolve" && (
            <Button variant="outline" onClick={() => exporting.mutate(a.id)}>
              Export saved resolution
            </Button>
          )}
        </article>
      ))}
    </section>
  );
}
