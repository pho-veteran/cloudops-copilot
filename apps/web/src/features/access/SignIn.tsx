import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import type { components } from "@/lib/api/generated/schema";
import { api, unwrap } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Failure } from "@/app/Feedback";

export function SignIn({
  reason,
  onSuccess,
}: {
  reason: "expired" | "signed_out" | null;
  onSuccess: (value: components["schemas"]["SessionView"]) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/login", { body: { username, password } })),
    onSuccess,
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }
  return (
    <main className="foundation-login">
      <div className="eyebrow">CLOUDOPS COPILOT · LOCAL FOUNDATION</div>
      <h1>Sign in to your workspace</h1>
      <p>Inspect saved incidents and development evidence.</p>
      {reason === "signed_out" && (
        <p role="status">You signed out. Sign in to return to your workspace.</p>
      )}
      {reason === "expired" && (
        <p role="status">
          Your session ended. Sign in again; saved work remains available. No
          action will be replayed.
        </p>
      )}
      <form onSubmit={submit} className="foundation-stack">
        <Label htmlFor="username">Username</Label>
        <Input
          id="username"
          autoComplete="username"
          required
          maxLength={64}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={256}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {mutation.isError && <Failure error={mutation.error} />}
        <Button disabled={mutation.isPending} type="submit">
          {mutation.isPending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <p className="muted">
        Use an account provisioned with the local account command.
      </p>
      <a href="/?simulation=1">Explore the fixture simulation</a>
    </main>
  );
}
