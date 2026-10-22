import { useEffect, useState } from "react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

function readRoute() {
  const [path, search = ""] = window.location.hash
    .replace(/^#\/?/, "")
    .split("?");
  const parts = path.split("/");
  return {
    page: parts[0] || "queue",
    id: parts[1] || "",
    run: new URLSearchParams(search).get("run") || "",
  };
}
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) =>
        !(
          error instanceof ApiError && [401, 403, 404].includes(error.status)
        ) && count < 1,
    },
    mutations: { retry: false },
  },
});

export function Workspace() {
  const cache = useQueryClient();
  const [expired, setExpired] = useState(false);
  const [route, setRoute] = useState(readRoute);
  const [creating, setCreating] = useState<{ workload?: string } | null>(null);
  const [dark, setDark] = useState(
    () => localStorage.getItem("cloudops-theme") === "dark",
  );
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      const result = unwrap(await api.GET("/api/session"));
      transport.setCsrfToken(result.csrf_token);
      return result;
    },
    retry: false,
    refetchInterval: 60000,
    enabled: !expired,
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
      setCreating(null);
      setExpired(true);
      void cache.cancelQueries();
      cache.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
    };
    window.addEventListener("cloudops:session-expired", listener);
    return () =>
      window.removeEventListener("cloudops:session-expired", listener);
  }, [cache]);
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
      transport.setCsrfToken(null);
      setExpired(true);
      setCreating(null);
      cache.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
    },
  });
  if (
    expired ||
    (session.isError &&
      session.error instanceof ApiError &&
      session.error.status === 401)
  ) {
    return (
      <SignIn
        expired={expired}
        onSuccess={(result) => {
          cache.removeQueries({
            predicate: (query) => query.queryKey[0] !== "session",
          });
          transport.setCsrfToken(result.csrf_token);
          cache.setQueryData(["session"], result);
          setExpired(false);
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
