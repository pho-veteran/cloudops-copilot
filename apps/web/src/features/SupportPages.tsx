import { useState, type Dispatch } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Database,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { type DemoState, type DemoAction, workloadOptions } from "@/demo/model";

type Props = {
  page: "workloads" | "knowledge" | "connections";
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
  onInvestigate: (workloadId: string) => void;
  onOpenIncident: (id: string) => void;
};
type Recommendation = {
  id: string;
  name: string;
  current: string;
  observed: string;
  recommended: string;
  coverage: string;
  status: string;
  reason: string;
};
const recommendations: Recommendation[] = [
  {
    id: "checkout",
    name: "checkout-api",
    current: "CPU 500m · RAM 256 / 512 MiB",
    observed: "CPU p95 180m · RAM max 468 MiB",
    recommended: "No recommendation",
    coverage: "96%",
    status: "Excluded · recent OOM",
    reason:
      "OOM occurred within this window. Do not lower memory requests or limits; monitor after recovery.",
  },
  {
    id: "catalog",
    name: "catalog-api",
    current: "CPU 500m · RAM 256 / 512 MiB",
    observed: "CPU p95 110m · RAM max 180 MiB",
    recommended: "CPU 150m · RAM 240 / 240 MiB",
    coverage: "98%",
    status: "Advisory",
    reason:
      "Based on six hours of demo data, not a full weekly load cycle. Export only; no automatic changes.",
  },
  {
    id: "media",
    name: "media-api",
    current: "CPU 600m · RAM 512 / 768 MiB",
    observed: "CPU p95 380m · RAM max 330 MiB",
    recommended: "No recommendation",
    coverage: "97%",
    status: "Excluded · HPA",
    reason:
      "HPA controls this workload. Review the interaction between requests and autoscaling before right-sizing.",
  },
  {
    id: "worker",
    name: "worker-api",
    current: "CPU 250m · RAM 256 / 512 MiB",
    observed: "Insufficient samples",
    recommended: "No recommendation",
    coverage: "22%",
    status: "Insufficient",
    reason:
      "Pod is Pending and usage samples are insufficient. Collect telemetry once the workload is stable.",
  },
  {
    id: "search",
    name: "search-api",
    current: "CPU 250m · RAM 128 / 256 MiB",
    observed: "Last sample: 09:00",
    recommended: "No recommendation",
    coverage: "61%",
    status: "Stale",
    reason:
      "Telemetry has stopped updating. This old report is not suitable for a new decision.",
  },
];
const runbooks = [
  {
    id: "oom",
    title: "OOM: distinguish a low limit from a memory leak",
    version: "v3",
    tags: ["OOM", "memory"],
    source: "runbooks/oom.md",
    body: "1. Correlate OOMKilled, restart count and the memory limit within the same window.\n2. Check the working set before the restart; OOM alone does not prove a memory leak.\n3. Review server-defined profiles, headroom and quota. Insufficient evidence requires manual guidance.\n4. Monitor restarts and memory during verification after a change.\nGuard: do not automatically lower memory after a recent OOM.",
  },
  {
    id: "rollout",
    title: "CrashLoop after rollout: investigate and recover",
    version: "v3",
    tags: ["CrashLoop", "rollout"],
    source: "runbooks/failed-rollout.md",
    body: "1. Compare the rollout time with the first failure.\n2. Read previous logs, exit codes and events; bind them to the correct Deployment UID.\n3. Restore only a known-good snapshot confirmed before the incident.\n4. Review the diff, preconditions and approval expiry. No baseline means manual guidance.\n5. Verify rollout readiness and no new restarts.",
  },
  {
    id: "load",
    title: "High latency: check load and capacity",
    version: "v3",
    tags: ["Load", "capacity"],
    source: "runbooks/traffic-spike.md",
    body: "1. Compare RPS, latency, errors and CPU throttling.\n2. Check replicas, node headroom and HPA.\n3. Scale only within bounds and without conflicting with a controller.\n4. Keep load constant during verification; record when the load generator stops.\n5. If a dependency causes the failure, scaling is not a proven fix.",
  },
  {
    id: "pending",
    title: "Pending / ImagePull: gather evidence and hand off",
    version: "v3",
    tags: ["Pending", "ImagePull"],
    source: "runbooks/pod-startup.md",
    body: "1. Read scheduling and image-pull events and container status.\n2. Compare requests, quota, node capacity and the image reference.\n3. Report only evidence-backed causes; a read permission failure means Not checked.\n4. These patterns are investigation-only in this release. Do not create a write action.\n5. Record the next checks and handoff owner.",
  },
];
const integrations = [
  {
    id: "worker",
    name: "CloudOps worker",
    subtitle: "Durable workflow · demo-worker-01",
    status: "Ready",
    detail:
      "One worker with retryable jobs and retained history. No Akmatori or Aurora runtime.",
  },
  {
    id: "kubernetes",
    name: "Kubernetes",
    subtitle: "lab-k3s · namespace cloudops-lab",
    status: "Ready",
    detail:
      "The collector is read-only; the executor has separate write permissions. This demo does not access a real cluster.",
  },
  {
    id: "prometheus",
    name: "Prometheus",
    subtitle: "metrics.internal · six-hour window",
    status: "Stale",
    detail:
      "The search-api sample is stale. Demo retry updates connection status only, not historical reports.",
  },
  {
    id: "model",
    name: "Model endpoint",
    subtitle: "OpenAI-compatible · ops-model",
    status: "Not checked",
    detail:
      "Server-configured base URL: https://model.example/v1 · credential reference: MODEL_API_KEY. Keys are never stored in the browser.",
  },
  {
    id: "embedding",
    name: "Runbook retrieval",
    subtitle: "Embedding optional · catalog fallback",
    status: "Degraded",
    detail:
      "The index is out of date. Saved runbooks remain readable; retrieval may miss new documents.",
  },
];

function download(
  name: string,
  text: string,
  type = "text/plain;charset=utf-8",
) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function Header({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <header className="space-y-2">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-muted-foreground">
        {eyebrow}
        <Badge variant="outline" className="normal-case tracking-normal">
          Interactive demo
        </Badge>
      </div>
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
        {description}
      </p>
    </header>
  );
}

export function SupportPages({
  page,
  state,
  dispatch,
  onInvestigate,
  onOpenIncident,
}: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [tag, setTag] = useState("All");
  const [indexDegraded, setIndexDegraded] = useState(true);
  const [connectionStates, setConnectionStates] = useState<
    Record<string, string>
  >({});
  const [testMode, setTestMode] = useState<
    "ok" | "transport" | "contract" | "unauthorized" | "rate_limit"
  >("ok");
  const [testResult, setTestResult] = useState<{
    transport: string;
    contract: string;
  } | null>(null);
  const [setup, setSetup] = useState<string | null>(null);
  const [report, setReport] = useState("current");
  const canWrite = state.role !== "viewer" && state.sessionActive;
  const chosen = workloadOptions.find((w) => w.id === selected);
  const activeRunbook = runbooks.find((r) => r.id === selected);
  const filteredWorkloads = workloadOptions.filter((w) =>
    `${w.name} ${w.namespace} ${w.pattern}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  const filteredBooks = runbooks.filter(
    (r) =>
      `${r.title} ${r.tags.join(" ")} ${r.body}`
        .toLowerCase()
        .includes(query.toLowerCase()) &&
      (tag === "All" || r.tags.includes(tag)),
  );
  const feedback = notice ? (
    <Alert>
      <CheckCircle2 className="h-4 w-4" />
      <AlertDescription>{notice}</AlertDescription>
    </Alert>
  ) : null;
  const confirmBaseline = () => {
    if (!confirm) return;
    if (!state.healthyCandidates[confirm] || !canWrite) {
      setNotice(
        "Cannot confirm: the candidate is no longer healthy or this session lacks permission.",
      );
      setConfirm(null);
      return;
    }
    dispatch({ type: "confirm_known_good", workloadId: confirm });
    setNotice(
      "Known-good metadata confirmed in the demo. No Kubernetes write was sent.",
    );
    setConfirm(null);
  };

  if (page === "workloads")
    return (
      <div className="space-y-6">
        <Header
          eyebrow="Resources & optimization"
          title="Workloads"
          description="Review workload health, prepare a known-good baseline and start a scoped investigation. Recommendations support decisions without changing resources automatically."
        />
        {feedback}
        <Tabs defaultValue="inventory">
          <TabsList>
            <TabsTrigger value="inventory">Inventory</TabsTrigger>
            <TabsTrigger value="recommendations">Recommendations</TabsTrigger>
          </TabsList>
          <TabsContent value="inventory" className="space-y-4 pt-4">
            <div className="relative max-w-md">
              <Search className="absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
              <Input
                aria-label="Search workloads"
                className="pl-9"
                placeholder="Search workload, namespace or symptom…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <Card className="overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Workload</TableHead>
                    <TableHead>Health</TableHead>
                    <TableHead>Controller</TableHead>
                    <TableHead>Known-good</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredWorkloads.map((w) => (
                    <TableRow key={w.id}>
                      <TableCell>
                        <button
                          className="text-left font-medium underline-offset-4 hover:underline"
                          onClick={() => setSelected(w.id)}
                        >
                          {w.name}
                        </button>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {w.namespace}
                        </p>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            state.healthyCandidates[w.id]
                              ? "secondary"
                              : "outline"
                          }
                        >
                          {state.healthyCandidates[w.id]
                            ? "Healthy · stable for 30 minutes"
                            : w.pattern}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {w.controller === "HPA" ? "HPA" : "Deployment"}
                      </TableCell>
                      <TableCell>
                        {state.knownGood[w.id]
                          ? "Confirmed"
                          : "No baseline"}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSelected(w.id)}
                        >
                          View details
                          <ArrowRight className="ml-1 h-3 w-3" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {!filteredWorkloads.length && (
                <p className="p-8 text-center text-muted-foreground">
                  No workloads match this search.{" "}
                  <button className="underline" onClick={() => setQuery("")}>
                    Clear filters
                  </button>
                </p>
              )}
            </Card>
            <p className="text-xs text-muted-foreground">
              Demo scope: one cluster · cloudops-lab. Health comes from the
              candidate fixture, not the absence of alerts.
            </p>
          </TabsContent>
          <TabsContent value="recommendations" className="space-y-4 pt-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold">Resource recommendations</h2>
                <p className="text-sm text-muted-foreground">
                  KRR adapter demo · simple strategy · CPU: millicores (m),
                  memory: MiB
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <select
                  aria-label="Select report"
                  className="rounded-md border bg-background px-3 py-2 text-sm"
                  value={report}
                  onChange={(e) => setReport(e.target.value)}
                >
                  <option value="current">Report 10:00–16:00 · 6 hours</option>
                  <option value="old">Previous-day report · stale</option>
                </select>
                <Button
                  variant="outline"
                  onClick={() => {
                    download(
                      `cloudops-recommendations-${report}.json`,
                      JSON.stringify(
                        {
                          demo: true,
                          report:
                            report === "current"
                              ? "10:00–16:00"
                              : "previous-day",
                          requestedWindowHours: 6,
                          strategy: "KRR simple (fixture)",
                          billingSavings: "unknown",
                          recommendations: recommendations.map((r) => ({
                            ...r,
                            status: report === "old" ? "Stale" : r.status,
                            recommended:
                              report === "old"
                                ? "No recommendation: stale report"
                                : r.recommended,
                          })),
                        },
                        null,
                        2,
                      ),
                      "application/json",
                    );
                    setNotice(
                      "Exported a JSON report with coverage, units and exclusions. Export does not apply changes.",
                    );
                  }}
                >
                  <ArrowDownToLine className="mr-2 h-4 w-4" />
                  Export JSON
                </Button>
              </div>
            </div>
            <Alert>
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>
                {report === "old"
                  ? "This report is stale — do not use it to apply changes"
                  : "Advisory only: six hours of demo data"}
              </AlertTitle>
              <AlertDescription>
                These are not billing savings. Lower requests on a fixed EC2
                instance do not automatically reduce the bill. There is no
                auto-apply or exclusion override.
              </AlertDescription>
            </Alert>
            <Card>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Workload / coverage</TableHead>
                    <TableHead>Current · request / limit</TableHead>
                    <TableHead>Observed</TableHead>
                    <TableHead>Recommended</TableHead>
                    <TableHead>Result</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recommendations.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>
                        <button
                          className="font-medium hover:underline"
                          onClick={() => setSelected(r.id)}
                        >
                          {r.name}
                        </button>
                        <div className="mt-1 text-xs text-muted-foreground">
                          {r.coverage} coverage · 6 hours
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">{r.current}</TableCell>
                      <TableCell className="text-xs">{r.observed}</TableCell>
                      <TableCell className="text-xs">
                        {report === "old"
                          ? "No recommendation: stale"
                          : r.recommended}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {report === "old" ? "Stale" : r.status}
                        </Badge>
                        <p className="mt-2 max-w-xs text-xs leading-5 text-muted-foreground">
                          {report === "old"
                            ? "A fresh report is required before review."
                            : r.reason}
                        </p>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          </TabsContent>
        </Tabs>
        <Dialog
          open={!!chosen}
          onOpenChange={(open) => {
            if (!open) setSelected(null);
          }}
        >
          <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
            <DialogHeader>
              <DialogTitle>{chosen?.name}</DialogTitle>
              <DialogDescription>
                Snapshot demo · namespace cloudops-lab · Deployment
              </DialogDescription>
            </DialogHeader>
            {chosen && (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4 rounded-lg border p-4 text-sm">
                  <div className="text-muted-foreground">
                    Health / stability
                  </div>
                  <div>
                    {state.healthyCandidates[chosen.id]
                      ? "Healthy · 30 minutes"
                      : `Unhealthy · ${chosen.pattern}`}
                  </div>
                  <div className="text-muted-foreground">Revision</div>
                  <code>
                    {chosen.id === "catalog"
                      ? state.healthyCandidates.catalog
                        ? "rev-17 / APP_MODE=production / 1 of 1 Ready"
                        : "rev-18 / APP_MODE empty"
                      : "rev-7 / image@sha256:9f42c8"}
                  </code>
                  <div className="text-muted-foreground">Candidate hash</div>
                  <code className="break-all">
                    sha256:{chosen.id}-template-
                    {chosen.id === "catalog"
                      ? "17-app-mode-production"
                      : "7-9f42"}
                  </code>
                  <div className="text-muted-foreground">Known-good</div>
                  <div>
                    {state.knownGood[chosen.id]
                      ? `Baseline rev-${chosen.id === "catalog" ? "17 / APP_MODE=production" : "7"}`
                      : "Not confirmed"}
                  </div>
                </div>
                <Alert>
                  <ShieldCheck className="h-4 w-4" />
                  <AlertDescription>
                    A baseline stores metadata only when healthy. Ineligible
                    snapshots cannot be restored. A real backend must recheck
                    UID, hash and health before saving.
                  </AlertDescription>
                </Alert>
                <div className="flex flex-wrap gap-2">
                  <Button
                    disabled={!canWrite}
                    onClick={() => {
                      setSelected(null);
                      onInvestigate(chosen.id);
                    }}
                  >
                    Start investigation
                  </Button>
                  <Button
                    variant="outline"
                    disabled={
                      !canWrite ||
                      !state.healthyCandidates[chosen.id] ||
                      !!state.knownGood[chosen.id]
                    }
                    onClick={() => setConfirm(chosen.id)}
                  >
                    {state.knownGood[chosen.id]
                      ? "Known-good saved"
                      : "Confirm known-good"}
                  </Button>
                </div>
                {!state.healthyCandidates[chosen.id] && (
                  <p className="text-xs text-muted-foreground">
                    A baseline cannot be confirmed while the workload is unhealthy.
                  </p>
                )}
                {chosen.id === "catalog" &&
                  state.knownGood.catalog &&
                  state.healthyCandidates.catalog && (
                    <Button
                      variant="outline"
                      disabled={!canWrite}
                      onClick={() => {
                        dispatch({
                          type: "inject_failure",
                          workloadId: "catalog",
                        });
                        setNotice(
                          "Simulated a failed catalog rollout. Open the incident to continue investigating.",
                        );
                        setSelected(null);
                      }}
                    >
                      <Activity className="mr-2 h-4 w-4" />
                      Demo: inject failed rollout
                    </Button>
                  )}
                <div className="border-t pt-4">
                  <h3 className="mb-2 text-sm font-medium">
                    Related incidents
                  </h3>
                  {state.incidents
                    .filter((i) => i.workloadId === chosen.id)
                    .map((i) => (
                      <Button
                        key={i.id}
                        variant="ghost"
                        className="mb-1 w-full justify-between"
                        onClick={() => {
                          setSelected(null);
                          onOpenIncident(i.id);
                        }}
                      >
                        {i.id}
                        <ArrowRight className="h-4 w-4" />
                      </Button>
                    ))}
                  {!state.incidents.some((i) => i.workloadId === chosen.id) && (
                    <p className="text-sm text-muted-foreground">
                      No incidents yet. Start a manual investigation for this workload.
                    </p>
                  )}
                </div>
              </div>
            )}
          </DialogContent>
        </Dialog>
        <Dialog
          open={!!confirm}
          onOpenChange={(open) => {
            if (!open) setConfirm(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Confirm this known-good candidate?</DialogTitle>
              <DialogDescription>
                Confirm only a snapshot you have reviewed. This is demo metadata;
                resources are not changed.
              </DialogDescription>
            </DialogHeader>
            <div className="rounded-md border p-4 text-sm">
              <p>
                Revision:{" "}
                {confirm === "catalog"
                  ? "rev-17 / APP_MODE=production / 1 of 1 Ready"
                  : "rev-7 / image@sha256:9f42c8"}
              </p>
              <p className="mt-2 break-all font-mono">
                sha256:{confirm}-template-
                {confirm === "catalog" ? "17-app-mode-production" : "7-9f42"}
              </p>
              <p className="mt-2">
                Health:{" "}
                {confirm && state.healthyCandidates[confirm]
                  ? "Healthy · 30 minutes"
                  : "Not eligible"}
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirm(null)}>
                Cancel
              </Button>
              <Button
                disabled={
                  !canWrite || !confirm || !state.healthyCandidates[confirm]
                }
                onClick={confirmBaseline}
              >
                Confirm snapshot
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    );

  if (page === "knowledge")
    return (
      <div className="space-y-6">
        <Header
          eyebrow="Knowledge sources"
          title="Knowledge"
          description="Runbooks have explicit sources and versions. They guide investigation; live telemetry provides evidence for incident findings."
        />
        {feedback}
        <Alert>
          <Database className="h-4 w-4" />
          <AlertTitle>
            {indexDegraded
              ? "Retrieval index degraded"
              : "Demo index is up to date"}
          </AlertTitle>
          <AlertDescription>
            {indexDegraded
              ? "New versions may not be searchable through retrieval. Saved documents remain available in the catalog."
              : "Simulated reindexing four runbooks. No embedding provider was called."}
            <Button
              className="ml-3"
              size="sm"
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                setIndexDegraded((v) => !v);
                setNotice(
                  indexDegraded
                    ? "Demo reindex completed; four versions are marked as indexed."
                    : "Simulated an index failure to review the degraded experience.",
                );
              }}
            >
              {indexDegraded ? "Retry demo reindex" : "Simulate degraded index"}
            </Button>
          </AlertDescription>
        </Alert>
        <div className="flex flex-wrap gap-3">
          <div className="relative min-w-56 flex-1">
            <Search className="absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
            <Input
              aria-label="Search runbooks"
              className="pl-9"
              placeholder="Search content, title or tag…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select
            aria-label="Filter runbook tags"
            className="rounded-md border bg-background px-3 py-2 text-sm"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
          >
            {[
              "All",
              "OOM",
              "memory",
              "CrashLoop",
              "rollout",
              "Load",
              "capacity",
              "Pending",
              "ImagePull",
            ].map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          {filteredBooks.map((r) => (
            <Card key={r.id}>
              <CardHeader>
                <div className="mb-2 flex gap-2">
                  {r.tags.map((t) => (
                    <Badge key={t} variant="secondary">
                      {t}
                    </Badge>
                  ))}
                </div>
                <CardTitle className="text-base leading-6">{r.title}</CardTitle>
                <CardDescription className="break-all font-mono text-xs">
                  {r.source} · {r.version}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  Read-only · curated
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSelected(r.id)}
                >
                  <BookOpen className="mr-2 h-4 w-4" />
                  Read runbook
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
        {!filteredBooks.length && (
          <Card>
            <CardContent className="p-8 text-center text-sm text-muted-foreground">
              No runbooks found.{" "}
              <button
                className="underline"
                onClick={() => {
                  setQuery("");
                  setTag("All");
                }}
              >
                Clear filters
              </button>
            </CardContent>
          </Card>
        )}
        <Dialog
          open={!!activeRunbook}
          onOpenChange={(open) => {
            if (!open) setSelected(null);
          }}
        >
          <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>{activeRunbook?.title}</DialogTitle>
              <DialogDescription>
                {activeRunbook?.source} · {activeRunbook?.version} · current
                version in the demo catalog
              </DialogDescription>
            </DialogHeader>
            <div className="whitespace-pre-wrap text-sm leading-8">
              {activeRunbook?.body}
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  if (activeRunbook)
                    download(
                      `${activeRunbook.id}-${activeRunbook.version}.md`,
                      `# ${activeRunbook.title}\n\nSource: ${activeRunbook.source}\nVersion: ${activeRunbook.version}\n\n${activeRunbook.body}`,
                    );
                }}
              >
                <ArrowDownToLine className="mr-2 h-4 w-4" />
                Download Markdown
              </Button>
              <Button onClick={() => setSelected(null)}>Close</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    );

  return (
    <div className="space-y-6">
      <Header
        eyebrow="System operations"
        title="Connections"
        description="Distinguish transport, the RCA contract and data freshness. All checks below are local simulations; no endpoint is contacted or real secret read."
      />
      {feedback}
      <div className="grid gap-3 sm:grid-cols-3">
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Runtime</p>
            <p className="mt-2 font-medium">CloudOps workflow</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Token usage</p>
            <p className="mt-2 font-medium">Unavailable</p>
            <p className="mt-1 text-xs text-muted-foreground">
              The provider has not returned usage; do not assume zero.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Remaining quota</p>
            <p className="mt-2 font-medium">Unknown</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Quota is not inferred from local request counts.
            </p>
          </CardContent>
        </Card>
      </div>
      <div className="space-y-3">
        {integrations.map((c) => (
          <Card key={c.id}>
            <CardContent className="flex flex-wrap items-start justify-between gap-4 p-5">
              <div className="flex min-w-0 flex-1 gap-3">
                <Server className="mt-1 h-5 w-5 shrink-0 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-medium">{c.name}</h2>
                    <Badge variant="outline">
                      {connectionStates[c.id] ||
                        (c.id === "embedding" && !indexDegraded
                          ? "Ready"
                          : c.status)}
                    </Badge>
                  </div>
                  <p className="mt-1 break-all text-xs text-muted-foreground">
                    {c.subtitle}
                  </p>
                  <p className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground">
                    {c.detail}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {connectionStates[c.id]
                      ? "Last attempt: just simulated locally"
                      : "Last attempt: 16:00 fixture · last success: 15:58 fixture"}
                  </p>
                </div>
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setSetup(c.id)}
                >
                  Setup guide
                </Button>
                {c.id !== "model" && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!canWrite}
                    onClick={() => {
                      setConnectionStates((v) => ({
                        ...v,
                        [c.id]: "Ready · demo",
                      }));
                      if (c.id === "embedding") setIndexDegraded(false);
                      setNotice(
                        `${c.name}: simulated retry completed. No network request was sent.`,
                      );
                    }}
                  >
                    <RefreshCw className="mr-2 h-3 w-3" />
                    Retry demo
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Test model endpoint</CardTitle>
          <CardDescription>
            Transport OK confirms reachability only. RCA contract: structured JSON,
            evidence IDs, scope and abstention. Evidence is supplied by the
            fixed workflow; model tool calling is not required.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <select
              aria-label="Endpoint test scenario"
              className="min-w-0 max-w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={testMode}
              onChange={(e) => {
                setTestMode(e.target.value as typeof testMode);
                setTestResult(null);
              }}
            >
              <option value="ok">Demo: transport + contract pass</option>
              <option value="transport">Demo: transport timeout</option>
              <option value="contract">
                Demo: transport OK, contract fail
              </option>
              <option value="unauthorized">
                Demo: 401 credential rejected
              </option>
              <option value="rate_limit">Demo: 429 rate limited</option>
            </select>
            <Button
              disabled={!canWrite}
              onClick={() => {
                setTestResult(
                  testMode === "unauthorized"
                    ? {
                        transport: "Reachable / HTTP 401 / credential rejected",
                        contract:
                          "Not checked / repair credential reference on server",
                      }
                    : testMode === "rate_limit"
                      ? {
                          transport:
                            "Reachable / HTTP 429 / Retry-After 30s (fixture)",
                          contract:
                            "Waiting / bounded retry; no paid endpoint fallback",
                        }
                      : testMode === "transport"
                        ? {
                            transport: "Failed · timeout fixture",
                            contract: "Not checked · transport failed",
                          }
                        : testMode === "contract"
                          ? {
                              transport: "OK · HTTP 200 fixture",
                              contract:
                                "Failed: missing evidence_ids / RCA schema",
                            }
                          : {
                              transport: "OK · HTTP 200 fixture",
                              contract:
                                "OK: structured RCA + evidence citation fixture",
                            },
                );
                setConnectionStates((v) => ({
                  ...v,
                  model:
                    testMode === "ok"
                      ? "Contract verified · demo"
                      : testMode === "transport"
                        ? "Unavailable / demo"
                        : testMode === "unauthorized"
                          ? "Credential rejected / 401 demo"
                          : testMode === "rate_limit"
                            ? "Rate limited / 429 demo"
                            : "Contract incompatible / demo",
                }));
              }}
            >
              Run demo test
            </Button>
          </div>
          {testResult && (
            <div
              role="status"
              aria-live="polite"
              className="grid gap-3 sm:grid-cols-2"
            >
              <div className="rounded-lg border p-4 text-sm">
                <p className="mb-2 font-medium">1. Transport</p>
                {testResult.transport}
              </div>
              <div className="rounded-lg border p-4 text-sm">
                <p className="mb-2 font-medium">2. RCA contract</p>
                {testResult.contract}
              </div>
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            Capabilities are marked only after the contract passes. Demo checks
            do not grant execution permissions or switch models.
          </p>
        </CardContent>
      </Card>
      <Dialog
        open={!!setup}
        onOpenChange={(open) => {
          if (!open) setSetup(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Configure {integrations.find((c) => c.id === setup)?.name}
            </DialogTitle>
            <DialogDescription>
              Scoped setup instructions; there is no secret or endpoint editor
              in the browser.
            </DialogDescription>
          </DialogHeader>
          <ol className="list-decimal space-y-3 pl-5 text-sm leading-6">
            {setup === "model" ? (
              <>
                <li>
                  Set the OpenAI-compatible base URL and model ID in server
                  configuration. The URL shown here is an example fixture.
                </li>
                <li>
                  Supply credentials through the MODEL_API_KEY secret or
                  environment reference. Never paste keys into the UI or runbooks.
                </li>
                <li>
                  Configure request and token budgets, timeouts, retries and
                  concurrency. Do not fall back automatically to a paid endpoint.
                </li>
                <li>
                  Run the transport test, then the RCA contract test. A failed
                  contract keeps the integration degraded. Missing provider
                  usage remains unavailable.
                </li>
              </>
            ) : setup === "kubernetes" ? (
              <>
                <li>Connect only to cluster lab-k3s and namespace cloudops-lab.</li>
                <li>
                  Grant read-only credentials to the collector. Use separate
                  executor credentials with an action allowlist.
                </li>
                <li>
                  Check read access to pods, logs, events and Deployments. Do not
                  grant cluster-admin to bypass errors.
                </li>
              </>
            ) : setup === "embedding" ? (
              <>
                <li>
                  Embeddings are optional. Configure the provider and model on
                  the server if using semantic retrieval.
                </li>
                <li>
                  Index by source checksum and version. Do not mix embedding
                  dimensions or models.
                </li>
                <li>
                  Keep the catalog readable if indexing fails. Retrying must not
                  change old evidence snapshots.
                </li>
              </>
            ) : (
              <>
                <li>
                  Check the internal deployment service and health endpoint
                  using the setup guide.
                </li>
                <li>
                  Verify source timestamps, timeouts and access permissions.
                  Keep missing and stale states explicit.
                </li>
                <li>
                  Prototype retries simulate state only. They do not demonstrate
                  recovery of historical telemetry.
                </li>
              </>
            )}
          </ol>
          <DialogFooter>
            <Button onClick={() => setSetup(null)}>Got it</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
