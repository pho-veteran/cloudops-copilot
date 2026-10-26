import { useEffect, useRef, useState } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  ListTodo,
  LogOut,
  Moon,
  Plug,
  Sun,
} from "lucide-react";
import { ApiError, api, transport, unwrap } from "@/lib/api/client";
import { SignIn } from "@/features/access/SignIn";
import { WorkQueue } from "@/features/incidents/WorkQueue";
import { CreateIncident } from "@/features/incidents/CreateIncident";
import { Workloads } from "@/features/workloads/Workloads";
import { InvestigationWorkspace } from "@/features/investigations/InvestigationWorkspace";
import { Connections } from "@/features/connections/Connections";
import { Failure, Loading } from "./Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { readRoute } from "./routes";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) =>
        !(
          error instanceof ApiError &&
          [401, 403, 404, 409].includes(error.status)
        ) && count < 1,
    },
    mutations: { retry: false },
  },
});

export function Workspace() {
  const cache = useQueryClient();
  const [signInReason, setSignInReason] = useState<
    "expired" | "signed_out" | null
  >(null);
  const [route, setRoute] = useState(readRoute);
  const [creating, setCreating] = useState<{ workload?: string } | null>(null);
  const [authorityReady, setAuthorityReady] = useState(false);
  const active = useRef(true);
  const identity = useRef<string | null>(null);
  const clearProtected = () => {
    void cache.cancelQueries({ predicate: (q) => q.queryKey[0] !== "session" });
    cache.removeQueries({ predicate: (q) => q.queryKey[0] !== "session" });
    cache.getMutationCache().clear();
    logout.reset();
    setCreating(null);
  };
  const [dark, setDark] = useState(
    () => localStorage.getItem("cloudops-theme") === "dark",
  );
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async ({ signal }) => {
      const result = unwrap(await api.GET("/api/session", { signal }));
      transport.setCsrfToken(result.csrf_token);
      const next = `${result.principal.id}:${result.principal.role}:${result.csrf_token}`;
      if (identity.current !== null && identity.current !== next)
        clearProtected();
      identity.current = next;
      setAuthorityReady(active.current);
      return result;
    },
    retry: false,
    refetchInterval: 60000,
    refetchOnWindowFocus: false,
    enabled: signInReason === null,
  });
  useEffect(() => {
    const listener = () => {
      setRoute(readRoute());
      setCreating(null);
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  useEffect(() => {
    const listener = () => {
      transport.clearSession();
      setAuthorityReady(false);
      setCreating(null);
      setSignInReason("expired");
      void cache.cancelQueries();
      cache.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
      cache.removeQueries({ queryKey: ["session"] });
      cache.getMutationCache().clear();
      logout.reset();
    };
    window.addEventListener("cloudops:session-expired", listener);
    return () =>
      window.removeEventListener("cloudops:session-expired", listener);
  }, [cache]);
  useEffect(() => {
    if (signInReason !== null) return;
    const suspend = () => {
      active.current = false;
      transport.clearSession();
      setAuthorityReady(false);
      setCreating(null);
      void cache.cancelQueries();
      cache.removeQueries({ predicate: (q) => q.queryKey[0] !== "session" });
      cache.getMutationCache().clear();
      logout.reset();
    };
    const resume = () => {
      if (document.visibilityState === "hidden") return;
      active.current = true;
      setAuthorityReady(false);
      void session.refetch();
    };
    const visibility = () =>
      document.visibilityState === "hidden" ? suspend() : resume();
    window.addEventListener("blur", suspend);
    window.addEventListener("focus", resume);
    window.addEventListener("pageshow", resume);
    window.addEventListener("pagehide", suspend);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("blur", suspend);
      window.removeEventListener("focus", resume);
      window.removeEventListener("pageshow", resume);
      window.removeEventListener("pagehide", suspend);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [cache, signInReason, session.refetch]);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("cloudops-theme", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.page, route.id]);
  const logout = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/logout")),
    onSuccess: () => {
      transport.clearSession();
      setAuthorityReady(false);
      setSignInReason("signed_out");
      clearProtected();
      cache.removeQueries({ queryKey: ["session"] });
    },
  });
  if (
    signInReason !== null ||
    (session.isError &&
      session.error instanceof ApiError &&
      session.error.status === 401)
  ) {
    return (
      <SignIn
        reason={signInReason ?? (identity.current ? "expired" : null)}
        onSuccess={(result) => {
          clearProtected();
          transport.setCsrfToken(result.csrf_token);
          identity.current = `${result.principal.id}:${result.principal.role}:${result.csrf_token}`;
          cache.setQueryData(["session"], result);
          active.current = true;
          setAuthorityReady(true);
          setSignInReason(null);
        }}
      />
    );
  }
  if (session.isPending)
    return (
      <main className="foundation-login">
        <Loading />
      </main>
    );
  if (session.isError)
    return (
      <main className="foundation-login">
        <Failure error={session.error} retry={() => void session.refetch()} />
        <p>
          <a href="/?simulation=1">Open the fixture simulation</a>
        </p>
      </main>
    );
  if (
    !authorityReady ||
    identity.current !==
      `${session.data.principal.id}:${session.data.principal.role}:${session.data.csrf_token}`
  )
    return (
      <main className="foundation-login">
        <p role="status">Checking your session before showing saved work…</p>
      </main>
    );
  const canMutate = session.data.principal.role === "operator";
  return (
    <div className="foundation-app">
      <header className="foundation-header">
        <a href="#/queue" className="brand">
          <Activity size={22} /> CloudOps Copilot
        </a>
        <Badge variant="outline">Local foundation</Badge>
        <div className="foundation-header-actions">
          <span>
            {session.data.principal.username} · {session.data.principal.role}
          </span>
          <Button
            variant="ghost"
            size="icon"
            aria-label={dark ? "Use light theme" : "Use dark theme"}
            onClick={() => setDark((v) => !v)}
          >
            {dark ? <Sun /> : <Moon />}
          </Button>
          <Button
            variant="outline"
            disabled={logout.isPending}
            onClick={() => logout.mutate()}
          >
            <LogOut size={16} /> Sign out
          </Button>
        </div>
      </header>
      <div className="foundation-layout">
        <aside className="foundation-sidebar">
          <nav aria-label="Main navigation">
            {[
              { path: "queue", label: "Work queue", icon: ListTodo },
              { path: "workloads", label: "Workloads", icon: Boxes },
              { path: "connections", label: "Connections", icon: Plug },
            ].map((n) => (
              <a
                key={n.path}
                href={`#/${n.path}`}
                aria-current={
                  route.page === n.path ||
                  (n.path === "queue" && route.page === "incidents")
                    ? "page"
                    : undefined
                }
              >
                <n.icon size={18} />
                {n.label}
              </a>
            ))}
          </nav>
          <p className="muted">
            Saved application data.
            <br />
            Synthetic external evidence.
          </p>
          <a href="/?simulation=1">Open fixture simulation ↗</a>
        </aside>
        <main className="foundation-main" id="main-content">
          <div className="foundation-notice">
            <span>
              Development collector · AI diagnosis and recovery unavailable
            </span>
            {canMutate && (
              <Button size="sm" onClick={() => setCreating({})}>
                New incident
              </Button>
            )}
          </div>
          {!canMutate && (
            <p className="muted">
              Viewer access: inspect saved work. An Operator account is required
              to create incidents or start investigations.
            </p>
          )}
          {logout.isError && <Failure error={logout.error} />}
          {route.page === "queue" ? (
            <WorkQueue />
          ) : route.page === "workloads" ? (
            <Workloads
              canMutate={canMutate}
              onInvestigate={(workload) => setCreating({ workload })}
            />
          ) : route.page === "connections" ? (
            <Connections />
          ) : route.page === "incidents" && route.id ? (
            <InvestigationWorkspace
              key={route.id}
              id={route.id}
              selectedRun={route.run}
              canMutate={canMutate}
            />
          ) : (
            <section>
              <h1>Page unavailable</h1>
              <a href="#/queue">Return to work queue</a>
            </section>
          )}
        </main>
      </div>
      <Dialog
        open={Boolean(creating) && canMutate}
        onOpenChange={(open) => {
          if (!open) setCreating(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Start a manual investigation</DialogTitle>
            <DialogDescription>
              Create a saved incident for a configured development workload.
            </DialogDescription>
          </DialogHeader>
          {creating && (
            <CreateIncident
              initialWorkload={creating.workload}
              onCreated={(id, run) => {
                setCreating(null);
                window.location.hash = `/incidents/${id}?run=${run}`;
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
export default function FoundationApp() {
  return (
    <QueryClientProvider client={queryClient}>
      <Workspace />
    </QueryClientProvider>
  );
}
