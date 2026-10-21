import { useEffect, useState, type Dispatch } from "react";
import {
  Activity,
  BookOpen,
  Boxes,
  ChevronRight,
  CircleHelp,
  FlaskConical,
  ListTodo,
  LogOut,
  Moon,
  Plug,
  Plus,
  RotateCcw,
  ShieldCheck,
  Sun,
  UserRound,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  SidebarProvider,
  Sidebar,
  SidebarHeader,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarInset,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { StatusBadge } from "@/components/domain";
import {
  createInitialState,
  createScenario,
  reducer,
  scenarioOptions,
  workloadOptions,
  type DemoState,
  type DemoAction,
  type Incident,
} from "@/demo/model";
import { SupportPages } from "@/features/SupportPages";
import { IncidentWorkspace } from "@/features/IncidentWorkspace";

type Route = {
  page: string;
  id?: string;
  tab: string;
  run?: string;
  filter: string;
  search: string;
};
function readRoute(): Route {
  const [path, query = ""] = window.location.hash
      .replace(/^#\/?/, "")
      .split("?"),
    parts = path.split("/"),
    p = new URLSearchParams(query);
  return {
    page: parts[0] || "queue",
    id: parts[1],
    tab: p.get("tab") || "findings",
    run: p.get("run") || undefined,
    filter: p.get("filter") || "all",
    search: p.get("search") || "",
  };
}
function go(path: string, params: Record<string, string> = {}) {
  const q = new URLSearchParams(params);
  window.location.hash = `/${path}${q.size ? "?" + q : ""}`;
}
function loadState(): DemoState {
  try {
    const v = JSON.parse(
      localStorage.getItem("cloudops-experience-v1") || "null",
    );
    if (
      v?.schema === 1 &&
      Array.isArray(v.state?.incidents) &&
      v.state.incidents[0]?.run
    )
      return v.state;
  } catch {}
  return createInitialState();
}
const navigation = [
  { id: "queue", label: "Work queue", icon: ListTodo },
  { id: "workloads", label: "Workloads", icon: Boxes },
  { id: "knowledge", label: "Knowledge", icon: BookOpen },
  { id: "connections", label: "Connections", icon: Plug },
];
const activeStatuses = [
  "queued",
  "executing",
  "verifying",
  "uncertain",
  "rolling_back",
];

export default function App() {
  const [state, setState] = useState<DemoState>(loadState),
    [route, setRoute] = useState(readRoute);
  const [stories, setStories] = useState(false),
    [manual, setManual] = useState<string | null>(null),
    [storyGeneration, setStoryGeneration] = useState(0);
  const [theme, setTheme] = useState(
    () => localStorage.getItem("cloudops-theme") || "dark",
  );
  const dispatch: Dispatch<DemoAction> = (action) =>
    setState((s) => reducer(s, action));
  useEffect(() => {
    const listener = () => setRoute(readRoute());
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  useEffect(() => {
    localStorage.setItem(
      "cloudops-experience-v1",
      JSON.stringify({ schema: 1, state }),
    );
  }, [state]);
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [route.page, route.id, storyGeneration]);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("cloudops-theme", theme);
  }, [theme]);
  useEffect(() => {
    if (!state.sessionActive) {
      setManual(null);
      setStories(false);
    }
  }, [state.sessionActive]);
  const incident = state.incidents.find((i) => i.id === route.id);
  const story = scenarioOptions.find((s) => s.id === state.scenarioId);
  const openIncident = (id: string) => {
    dispatch({ type: "select_incident", id });
    go(`incidents/${id}`);
  };
  const loadStory = (id: string) => {
    const next = createScenario(id);
    setState(next);
    setStoryGeneration((n) => n + 1);
    setStories(false);
    setManual(null);
    go(
      id.includes("restore")
        ? "workloads"
        : `incidents/${next.selectedIncidentId}`,
    );
  };
  if (!state.sessionActive)
    return (
      <main className="login-shell">
        <Card className="login-card">
          <CardContent className="space-y-6 pt-6">
            <div className="brand">
              <Activity size={22} />
              <span>CloudOps</span>
            </div>
            <div>
              <Badge variant="outline">DEMO · no real account required</Badge>
              <h1>Your session has expired</h1>
              <p className="muted">
                Sign in again to return to your previous screen. Pending decisions will not be submitted automatically.
              </p>
            </div>
            <Button
              className="w-full"
              onClick={() => dispatch({ type: "set_session", active: true })}
            >
              <UserRound />
              Continue with demo account
            </Button>
            <Button
              variant="outline"
              className="w-full"
              onClick={() => {
                dispatch({ type: "set_role", role: "viewer" });
                dispatch({ type: "set_session", active: true });
              }}
            >
              Continue as Viewer
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  return (
    <SidebarProvider>
      <Sidebar collapsible="offcanvas">
        <SidebarHeader>
          <button className="brand" onClick={() => go("queue")}>
            <Activity size={23} />
            <span>CloudOps</span>
            <Badge variant="secondary">LAB</Badge>
          </button>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <div className="nav-caption">WORKSPACE</div>
            <SidebarMenu>
              {navigation.map(({ id, label, icon: Icon }) => (
                <SidebarMenuItem key={id}>
                  <SidebarMenuButton
                    isActive={
                      route.page === id ||
                      (id === "queue" && route.page === "incidents")
                    }
                    onClick={() => go(id)}
                  >
                    <Icon />
                    <span>{label}</span>
                    {id === "queue" && (
                      <span className="nav-count">
                        {
                          state.incidents.filter((i) => i.status !== "resolved")
                            .length
                        }
                      </span>
                    )}
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup className="mt-auto">
            <div className="scope-note">
              <ShieldCheck size={16} />
              <span>
                cloudops-lab
                <br />
                <small>1 cluster · scoped Deployments</small>
              </span>
            </div>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <Button variant="outline" onClick={() => setStories(true)}>
            <FlaskConical />
            Choose a story
          </Button>
          <div className="text-xs text-muted-foreground px-2 py-2">
            Interactive prototype · simulated data
          </div>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset>
        <header className="app-header">
          <div className="flex items-center gap-2 min-w-0">
            <SidebarTrigger />
            <span className="muted">Workspace</span>
            <ChevronRight size={14} />
            <span>
              {navigation.find((n) => n.id === route.page)?.label || "Incident"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              aria-label="Toggle light and dark theme"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            >
              {theme === "dark" ? <Sun /> : <Moon />}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost">
                  <UserRound />
                  <span className="account-label">
                    Alex · {state.role === "operator" ? "Operator" : "Viewer"}
                  </span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() =>
                    dispatch({
                      type: "set_role",
                      role: state.role === "operator" ? "viewer" : "operator",
                    })
                  }
                >
                  Switch to {state.role === "operator" ? "Viewer" : "Operator"}
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() =>
                    dispatch({ type: "set_session", active: false })
                  }
                >
                  <LogOut />
                  Simulate session expiry
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>
        <main className="workspace-main">
          <section className="story-strip">
            <FlaskConical size={18} />
            <div className="min-w-0">
              <strong>{story?.label || "Explore freely"}</strong>
              <p>
                {story?.goal || "Explore the investigation and decision workflow."}
              </p>
            </div>
            <div className="flex gap-1 shrink-0">
              <Button
                variant="ghost"
                size="icon"
                aria-label="Restart story"
                onClick={() => loadStory(state.scenarioId)}
              >
                <RotateCcw size={15} />
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setStories(true)}
              >
                Change story
              </Button>
            </div>
          </section>
          {state.role === "viewer" && (
            <Alert className="mb-4">
              <ShieldCheck />
              <AlertTitle>Read-only access</AlertTitle>
              <AlertDescription>
                You can view evidence, history and exports. Operator access is required to make changes or approve a plan.
              </AlertDescription>
            </Alert>
          )}
          {state.error && (
            <Alert variant="destructive" className="mb-4" role="alert">
              <AlertTitle>Action unavailable</AlertTitle>
              <AlertDescription>{state.error}</AlertDescription>
            </Alert>
          )}
          {state.notice && (
            <div className="notice" role="status" aria-live="polite">
              {state.notice}
            </div>
          )}
          {route.page === "queue" ? (
            <Queue
              state={state}
              filter={route.filter}
              search={route.search}
              onOpen={openIncident}
              onInvestigate={() => setManual(workloadOptions[0].id)}
              onFilter={(filter, search) => go("queue", { filter, search })}
            />
          ) : route.page === "incidents" ? (
            incident ? (
              <IncidentWorkspace
                key={`${storyGeneration}:${incident.id}`}
                state={state}
                incident={incident}
                dispatch={dispatch}
                tab={route.tab}
                runId={route.run}
                onTab={(tab, run) =>
                  go(`incidents/${incident.id}`, {
                    tab,
                    ...(run ? { run } : {}),
                  })
                }
                onBack={() => window.history.back()}
                onOpenWorkload={() => go("workloads")}
                onOpenIncident={openIncident}
              />
            ) : (
              <Card>
                <CardContent className="p-8">
                  <h1>Incident not found</h1>
                  <p className="muted">
                    This incident is not in the current fixture. Choose a story to load its sample data.
                  </p>
                  <Button onClick={() => setStories(true)}>
                    Choose a story
                  </Button>
                </CardContent>
              </Card>
            )
          ) : ["workloads", "knowledge", "connections"].includes(route.page) ? (
            <SupportPages
              key={`${storyGeneration}:${route.page}`}
              page={route.page as "workloads" | "knowledge" | "connections"}
              state={state}
              dispatch={dispatch}
              onInvestigate={setManual}
              onOpenIncident={openIncident}
            />
          ) : (
            <Button onClick={() => go("queue")}>Back to Work queue</Button>
          )}
        </main>
        <Dialog open={stories} onOpenChange={setStories}>
          <DialogContent className="story-dialog">
            <DialogHeader>
              <DialogTitle>
                Explore operator workflows
              </DialogTitle>
              <DialogDescription>
                Choose a scenario to start with a fresh fixture. All actions affect only the demo data in this browser.
              </DialogDescription>
            </DialogHeader>
            <div className="story-grid">
              {scenarioOptions.map((s, index) => (
                <button
                  key={s.id}
                  className="story-card"
                  onClick={() => loadStory(s.id)}
                >
                  <span className="story-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <div>
                    <strong>{s.label}</strong>
                    <p>{s.goal}</p>
                  </div>
                  <ChevronRight size={16} />
                </button>
              ))}
            </div>
          </DialogContent>
        </Dialog>
        <ManualDialog
          workloadId={manual}
          state={state}
          onClose={() => setManual(null)}
          onSubmit={(action) => {
            const next = reducer(state, action);
            setState(next);
            if (!next.error) {
              setManual(null);
              go(`incidents/${next.selectedIncidentId}`);
            }
          }}
        />
      </SidebarInset>
    </SidebarProvider>
  );
}

function Queue({
  state,
  filter,
  search,
  onOpen,
  onInvestigate,
  onFilter,
}: {
  state: DemoState;
  filter: string;
  search: string;
  onOpen: (id: string) => void;
  onInvestigate: () => void;
  onFilter: (filter: string, search: string) => void;
}) {
  const [queueView, setQueueView] = useState("ready"),
    [signal, setSignal] = useState(""),
    [duplicates, setDuplicates] = useState(1);
  const filters = [
    ["all", "All"],
    ["attention", "Needs attention"],
    ["approval", "Awaiting approval"],
    ["verifying", "Verifying"],
    ["blocked", "Blocked"],
    ["resolved", "Resolved"],
  ];
  const matches = (i: Incident) =>
    filter === "all" ||
    (filter === "resolved" && i.status === "resolved") ||
    (i.status !== "resolved" &&
      ((filter === "attention" &&
        !activeStatuses.includes(i.execution.status)) ||
        (filter === "approval" && i.plan?.status === "awaiting_approval") ||
        (filter === "verifying" &&
          ["executing", "verifying", "rolling_back"].includes(
            i.execution.status,
          )) ||
        (filter === "blocked" &&
          (["waiting_for_quota", "failed", "insufficient_evidence"].includes(
            i.run.status,
          ) ||
            ["uncertain", "manual_intervention"].includes(i.execution.status) ||
            Boolean(state.targetLocks[i.workloadId])))));
  const rows = (queueView === "empty" ? [] : state.incidents)
    .filter(matches)
    .filter((i) =>
      (i.title + " " + i.workload + " " + i.id)
        .toLowerCase()
        .includes(search.toLowerCase()),
    );
  return (
    <>
      <div className="page-title">
        <div>
          <div className="eyebrow">OPERATIONS</div>
          <h1>Work queue</h1>
          <p className="muted">What needs your decision next?</p>
        </div>
        <Button disabled={state.role === "viewer"} onClick={onInvestigate}>
          <Plus />
          Investigate workload
        </Button>
      </div>
      <div className="queue-toolbar">
        <div className="filter-list" aria-label="Filter incidents">
          {filters.map(([id, label]) => (
            <Button
              key={id}
              size="sm"
              variant={filter === id ? "secondary" : "ghost"}
              aria-pressed={filter === id}
              onClick={() => onFilter(id, search)}
            >
              {label}
            </Button>
          ))}
        </div>
        <Input
          aria-label="Search incidents or workloads"
          placeholder="Search incidents or workloads…"
          value={search}
          onChange={(e) => onFilter(filter, e.target.value)}
        />
      </div>
      <div className="queue-demo-controls">
        <Label htmlFor="queue-state">Preview data states</Label>
        <select
          id="queue-state"
          className="native-select"
          value={queueView}
          onChange={(e) => setQueueView(e.target.value)}
        >
          <option value="ready">Loaded</option>
          <option value="loading">Loading</option>
          <option value="error">Queue load error</option>
          <option value="empty">No incidents yet</option>
          <option value="capacity">Analysis deferred</option>
        </select>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setDuplicates((n) => n + 1);
            setSignal(
              "Duplicate alert fingerprint for INC-042: grouped into the existing incident without creating a new one.",
            );
          }}
        >
          Simulate duplicate alert ({duplicates})
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            setSignal(
              "The alert source reports recovery. This signal does not automatically resolve the incident. An operator must still review the evidence and decide.",
            )
          }
        >
          Simulate resolved alert
        </Button>
      </div>
      {signal && (
        <Alert className="mb-4">
          <AlertTitle>Intake signal · fixture</AlertTitle>
          <AlertDescription>
            {signal}
            <Button variant="link" onClick={() => onOpen("INC-042")}>
              Open INC-042
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {queueView === "capacity" && (
        <Alert className="mb-4">
          <AlertTitle>Analysis deferred · capacity</AlertTitle>
          <AlertDescription>
            The alert was received. Analysis is queued until a worker slot becomes available. Existing data remains readable.
            <Button
              size="sm"
              variant="outline"
              className="mt-3"
              onClick={() => setQueueView("ready")}
            >
              Simulate available worker slot
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {queueView === "loading" ? (
        <div className="content-panel space-y-4">
          <p>Loading work queue…</p>
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Button variant="outline" onClick={() => setQueueView("ready")}>
            Complete simulated load
          </Button>
        </div>
      ) : queueView === "error" ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load work queue</AlertTitle>
          <AlertDescription>
            Simulated timeout. An error is not displayed as an empty queue.
            <Button
              className="mt-3"
              variant="outline"
              onClick={() => setQueueView("ready")}
            >
              Retry loading
            </Button>
          </AlertDescription>
        </Alert>
      ) : (
        <section className="incident-list" aria-label="Incident list">
          {rows.length ? (
            rows.map((i) => (
              <button
                className="incident-row"
                key={i.id}
                onClick={() => onOpen(i.id)}
              >
                <div className="severity-mark" data-severity={i.severity} />
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="mono text-xs muted">{i.id}</span>
                    <strong>{i.title}</strong>
                  </div>
                  <div className="row-meta">
                    <span className="mono">
                      {i.namespace}/{i.workload}
                    </span>
                    <span>{i.impact}</span>
                  </div>
                  <div className="row-meta">
                    <StatusBadge status={i.status} />
                    <StatusBadge
                      status={
                        i.execution.status === "none"
                          ? i.run.status
                          : i.execution.status
                      }
                    />
                    {i.plan?.status === "awaiting_approval" && (
                      <StatusBadge status="awaiting_approval" />
                    )}
                    {state.targetLocks[i.workloadId] && (
                      <Badge variant="outline">Target locked</Badge>
                    )}
                  </div>
                </div>
                <ChevronRight size={18} />
              </button>
            ))
          ) : (
            <div className="empty-state">
              <ListTodo size={28} />
              <h2>No matching incidents</h2>
              <p className="muted">
                Clear the filters or start a workload investigation.
              </p>
              <Button variant="outline" onClick={() => onFilter("all", "")}>
                Clear filters
              </Button>
            </div>
          )}
        </section>
      )}
    </>
  );
}

function ManualDialog({
  workloadId,
  state,
  onClose,
  onSubmit,
}: {
  workloadId: string | null;
  state: DemoState;
  onClose: () => void;
  onSubmit: (action: DemoAction) => void;
}) {
  const [workload, setWorkload] = useState(workloadId || workloadOptions[0].id),
    [question, setQuestion] = useState(""),
    [window, setWindow] = useState("30 minutes"),
    [symptom, setSymptom] = useState("OOM");
  useEffect(() => {
    if (workloadId) {
      setWorkload(workloadId);
      setQuestion("");
    }
  }, [workloadId]);
  return (
    <Dialog
      open={workloadId !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Investigate a workload</DialogTitle>
          <DialogDescription>
            Choose a scope and ask a question. You do not need to wait for an alert.
          </DialogDescription>
        </DialogHeader>
        <form
          className="form-stack"
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit({
              type: "create_investigation",
              workloadId: workload,
              question,
              symptom,
              window,
            });
          }}
        >
          <Label htmlFor="manual-workload">Deployment in cloudops-lab</Label>
          <select
            id="manual-workload"
            className="native-select"
            value={workload}
            onChange={(e) => setWorkload(e.target.value)}
          >
            {workloadOptions.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
          <div className="two-cols">
            <div>
              <Label htmlFor="manual-symptom">Symptom</Label>
              <select
                id="manual-symptom"
                className="native-select"
                value={symptom}
                onChange={(e) => setSymptom(e.target.value)}
              >
                {["OOM", "CrashLoop", "Load", "Pending", "ImagePull"].map(
                  (p) => (
                    <option key={p}>{p}</option>
                  ),
                )}
              </select>
            </div>
            <div>
              <Label htmlFor="manual-window">Evidence window</Label>
              <select
                id="manual-window"
                className="native-select"
                value={window}
                onChange={(e) => setWindow(e.target.value)}
              >
                {["15 minutes", "30 minutes", "1 hour"].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </div>
          </div>
          <Label htmlFor="manual-question">Investigation question</Label>
          <Textarea
            id="manual-question"
            required
            minLength={3}
            placeholder="Why does this workload keep restarting?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <div className="dialog-actions">
            <Button variant="outline" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={state.role === "viewer"}>
              Start investigation
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
