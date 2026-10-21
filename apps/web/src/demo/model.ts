/** Deterministic, local-only product simulation. No network, timers or credentials. */
export type Role = "viewer" | "operator";
export type Pattern = "OOM" | "CrashLoop" | "Load" | "Pending" | "ImagePull";
export type IncidentStatus = "open" | "acknowledged" | "resolved";
export type RunStatus =
  | "queued"
  | "collecting"
  | "analyzing"
  | "waiting_for_quota"
  | "completed"
  | "insufficient_evidence"
  | "failed"
  | "cancelled";
export type PlanStatus =
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "stale"
  | "expired"
  | "invalidated";
export type ExecutionStatus =
  | "none"
  | "queued"
  | "executing"
  | "verifying"
  | "succeeded"
  | "failed"
  | "no_op"
  | "uncertain"
  | "rolling_back"
  | "rolled_back"
  | "manual_intervention";
export type Template =
  | "set_memory_profile"
  | "restore_deployment_template"
  | "scale_deployment";
export interface Evidence {
  id: string;
  title: string;
  source: string;
  observedAt: string;
  status: "available" | "partial" | "unavailable";
  excerpt: string;
}
export interface InvestigationRun {
  id: string;
  status: RunStatus;
  summary: string;
  evidence: Evidence[];
  supportingEvidenceIds: string[];
  missingEvidence: string[];
  ruledOut: string[];
  unverified: string[];
  window: string;
  question: string;
  reason: string;
  createdAt: string;
  configurationVersion: string;
  retrievalMode: "full_text";
  attempt: number;
}
export interface ActionPlan {
  id: string;
  template: Template;
  hash: string;
  status: PlanStatus;
  runId: string;
  targetUid: string;
  diff: { field: string; before: string; after: string }[];
  prerequisites: string[];
  verification: string[];
  risks: string[];
  rollbackEligible: boolean;
  expiresAt: string;
  configurationVersion: string;
}
export interface Execution {
  id: string;
  status: ExecutionStatus;
  outcomeUnknown: boolean;
  reconcileAttempts: number;
  rollbackEligible: boolean;
  summary: string;
  verification: string[];
  manualEvidence: string | null;
}
export interface Activity {
  id: string;
  at: string;
  actor: string;
  title: string;
  detail: string;
}
export interface Note {
  id: string;
  at: string;
  actor: string;
  text: string;
}
export interface Resolution {
  id: string;
  at: string;
  actor: string;
  reason: string;
  run: InvestigationRun;
  plan: ActionPlan | null;
  execution: Execution;
  notes: Note[];
  impact: string;
  title: string;
  workload: string;
  namespace: string;
}
export interface Incident {
  id: string;
  title: string;
  workloadId: string;
  workload: string;
  namespace: string;
  pattern: Pattern;
  severity: "warning" | "critical";
  status: IncidentStatus;
  acknowledgedBy: string | null;
  impact: string;
  run: InvestigationRun;
  runs: InvestigationRun[];
  plan: ActionPlan | null;
  plans: ActionPlan[];
  execution: Execution;
  executions: Execution[];
  notes: Note[];
  activity: Activity[];
  resolutions: Resolution[];
  expectedOutcome: string;
  manualReason: string | null;
  freshness: string;
  demoOutcome:
    | "success"
    | "verification_failed"
    | "rollback_failed"
    | "uncertain"
    | "no_op"
    | "inconclusive";
  applicability: {
    headroom: boolean;
    hpa: boolean;
    restoreDiff: boolean;
    externalConfig: boolean;
    rollbackExpired: boolean;
    rollbackStale: boolean;
    rollbackUnsafe: boolean;
  };
}
export interface DemoState {
  scenarioId: string;
  role: Role;
  sessionActive: boolean;
  incidents: Incident[];
  selectedIncidentId: string;
  knownGood: Record<string, boolean>;
  healthyCandidates: Record<string, boolean>;
  targetLocks: Record<
    string,
    { incidentId: string; executionId: string; reason: string }
  >;
  notice: string;
  error: string;
  clock: number;
}
type ForIncident = { incidentId?: string };
export type DemoAction =
  | { type: "select_incident"; id: string }
  | { type: "set_role"; role: Role }
  | { type: "set_session"; active: boolean }
  | { type: "load_scenario"; id: string }
  | { type: "clear_feedback" }
  | ({
      type:
        | "acknowledge"
        | "advance"
        | "retry"
        | "cancel_run"
        | "expire_plan"
        | "stale_plan"
        | "reconcile";
    } & ForIncident)
  | ({
      type:
        | "add_note"
        | "reinvestigate"
        | "reject"
        | "resolve"
        | "reopen"
        | "request_rollback"
        | "manual_handoff";
      reason: string;
    } & ForIncident)
  | ({ type: "approve"; reason?: string } & ForIncident)
  | ({
      type: "verify_manually";
      reason: string;
      evidence: string;
      oldRequestStopped: boolean;
      outcome?: "applied" | "not_applied";
    } & ForIncident)
  | {
      type: "create_investigation";
      workloadId: string;
      question: string;
      symptom?: string;
      window: string;
    }
  | { type: "confirm_known_good" | "inject_failure"; workloadId: string };

export const workloadOptions = [
  {
    id: "checkout",
    name: "checkout-api",
    namespace: "cloudops-lab",
    pattern: "OOM" as Pattern,
    ready: 1,
    total: 2,
    controller: "none",
  },
  {
    id: "catalog",
    name: "catalog-api",
    namespace: "cloudops-lab",
    pattern: "CrashLoop" as Pattern,
    ready: 0,
    total: 1,
    controller: "none",
  },
  {
    id: "search",
    name: "search-api",
    namespace: "cloudops-lab",
    pattern: "Load" as Pattern,
    ready: 1,
    total: 1,
    controller: "none",
  },
  {
    id: "worker",
    name: "worker-api",
    namespace: "cloudops-lab",
    pattern: "Pending" as Pattern,
    ready: 0,
    total: 1,
    controller: "none",
  },
  {
    id: "media",
    name: "media-api",
    namespace: "cloudops-lab",
    pattern: "ImagePull" as Pattern,
    ready: 1,
    total: 2,
    controller: "HPA",
  },
  {
    id: "payments",
    name: "payments-api",
    namespace: "cloudops-lab",
    pattern: "OOM" as Pattern,
    ready: 2,
    total: 2,
    controller: "none",
  },
] as const;

export const scenarioOptions = [
  {
    id: "success",
    label: "Phục hồi OOM",
    goal: "Xem bằng chứng, duyệt profile bộ nhớ trong giới hạn, xác minh và đóng sự cố.",
  },
  {
    id: "restore",
    label: "Chuẩn bị bản tốt để khôi phục",
    goal: "Xác nhận template đang khỏe, tạo lỗi demo rồi khôi phục bản tốt.",
  },
  {
    id: "scale",
    label: "Lưu lượng tăng đột biến",
    goal: "Kiểm tra capacity và metrics ứng dụng trước khi duyệt tăng replica trong giới hạn.",
  },
  {
    id: "pending",
    label: "Chẩn đoán Pending",
    goal: "Xem bằng chứng scheduling và bàn giao thủ công, không tạo lệnh thực thi.",
  },
  {
    id: "imagepull",
    label: "Chẩn đoán lỗi kéo image",
    goal: "Xem bằng chứng image và registry; không đọc credential hoặc ghi vào cluster.",
  },
  {
    id: "partial",
    label: "Thiếu bằng chứng",
    goal: "Xác định dữ liệu còn thiếu, thu thập lại và giữ kết quả điều tra trước.",
  },
  {
    id: "quota",
    label: "Hết quota model",
    goal: "Xem evidence đã thu, hủy hoặc thử lại lượt chờ; không tự chuyển sang trả phí.",
  },
  {
    id: "stale",
    label: "Target đã thay đổi",
    goal: "Không duyệt plan cũ; tạo điều tra mới với snapshot hiện tại.",
  },
  {
    id: "expired",
    label: "Plan hết hạn",
    goal: "Xem lý do không thể thực thi plan hết hạn và điều tra lại.",
  },
  {
    id: "rollback",
    label: "Xác minh và rollback",
    goal: "Theo dõi post-check thất bại, xem inverse diff và xác nhận rollback.",
  },
  {
    id: "failed-rollback",
    label: "Rollback không hoàn tất",
    goal: "Giữ nguyên kết quả thất bại và bàn giao để xử lý thủ công.",
  },
  {
    id: "rollback-expired",
    label: "Hết cửa sổ rollback",
    goal: "Xem thời hạn rollback riêng ngăn thao tác khôi phục nghịch đảo.",
  },
  {
    id: "rollback-stale",
    label: "Target rollback đã đổi",
    goal: "Không ghi đè các trường đã được thay đổi sau lần thực thi ban đầu.",
  },
  {
    id: "rollback-unsafe",
    label: "Profile cũ không an toàn",
    goal: "Không khôi phục profile bộ nhớ cũ có thể gây OOM trở lại.",
  },
  {
    id: "scale-hpa",
    label: "HPA quản lý replica",
    goal: "Nhận hướng dẫn thủ công khi controller sở hữu trường replica.",
  },
  {
    id: "scale-no-headroom",
    label: "Node không đủ headroom",
    goal: "Chặn đề xuất tăng replica khi capacity không đủ.",
  },
  {
    id: "restore-no-baseline",
    label: "Chưa có bản tốt",
    goal: "Giải thích vì sao chưa thể restore; không đánh dấu template lỗi là khỏe.",
  },
  {
    id: "restore-external-config",
    label: "Cấu hình ngoài template đổi",
    goal: "Không khẳng định restore template sẽ phục hồi nội dung ConfigMap hoặc Secret được tham chiếu.",
  },
  {
    id: "failed-rerun",
    label: "Điều tra lại thất bại",
    goal: "Giữ và đối chiếu kết luận đã hoàn tất khi lần điều tra mới nhất thất bại.",
  },
  {
    id: "uncertain",
    label: "Chưa rõ kết quả ghi",
    goal: "Reconcile có giới hạn, bàn giao giữ nguyên unknown, rồi xác minh trước khi mở khóa target.",
  },
  {
    id: "no-op",
    label: "Workload đã tự hồi phục",
    goal: "Nhận kết quả no-op; không tính tự hồi phục thành thành công của action.",
  },
  {
    id: "inconclusive",
    label: "Chưa đủ kết luận phục hồi",
    goal: "Rollout Ready nhưng thiếu coverage hoặc tải giảm nên chưa thể khẳng định phục hồi.",
  },
  {
    id: "viewer",
    label: "Quyền chỉ đọc",
    goal: "Xem và xuất dữ liệu; các thao tác thay đổi trạng thái bị chặn.",
  },
  {
    id: "session-expired",
    label: "Phiên đăng nhập hết hạn",
    goal: "Xem thao tác bị chặn, rồi khôi phục phiên operator mô phỏng.",
  },
] as const;

const copy = <T>(value: T): T => structuredClone(value);
const time = (clock: number) =>
  new Date(Date.UTC(2026, 9, 21, 3, 30 + clock)).toISOString();
const emptyExecution = (): Execution => ({
  id: "",
  status: "none",
  outcomeUnknown: false,
  reconcileAttempts: 0,
  rollbackEligible: false,
  summary: "No execution has started.",
  verification: [],
  manualEvidence: null,
});
const findings: Record<
  Pattern,
  { title: string; impact: string; summary: string; event: string }
> = {
  OOM: {
    title: "Checkout restarts at the memory limit",
    impact: "Two container restarts; one replica unavailable.",
    summary:
      "OOM termination and memory near the configured limit support a limit-related restart. A leak remains unverified.",
    event:
      "lastState.terminated.reason=OOMKilled; memory limit=256Mi; restartCount=2",
  },
  CrashLoop: {
    title: "Startup failure after configuration change",
    impact: "The new rollout has no Ready replica.",
    summary:
      "Previous logs show a missing APP_MODE after revision 18. Restore is applicable only with a recorded healthy baseline and a nonempty template diff.",
    event:
      "CrashLoopBackOff; previous logs: APP_MODE is required; template revision=18",
  },
  Load: {
    title: "Traffic increase coincides with latency",
    impact: "p95 latency 820ms with elevated errors.",
    summary:
      "RPS, latency and throttling rose together. Capacity is available and no HPA owns the replica field in this fixture.",
    event:
      "RPS=180; p95=820ms; error rate=3%; CPU throttling increased; node headroom available",
  },
  Pending: {
    title: "Replica remains Pending",
    impact: "One replica is unschedulable.",
    summary:
      "FailedScheduling reports insufficient memory. This release provides diagnosis and manual next steps, not node or quota changes.",
    event: "FailedScheduling: 0/1 nodes available: insufficient memory",
  },
  ImagePull: {
    title: "Image pull failure blocks rollout",
    impact: "New image unavailable; existing replica remains Ready.",
    summary:
      "The registry returned manifest unknown for the configured image tag. Registry ownership/access must be reviewed manually; no secret contents are collected.",
    event:
      "ImagePullBackOff; manifest unknown for registry.example.invalid/media:missing-tag",
  },
};

function makeRun(id: string, pattern: Pattern, clock = 0): InvestigationRun {
  const fixture = findings[pattern];
  const evidence: Evidence[] = [
    {
      id: `${id}-E1`,
      title: "Kubernetes event and previous state",
      source: "Kubernetes API · demo snapshot",
      observedAt: time(clock),
      status: "available",
      excerpt: fixture.event,
    },
    {
      id: `${id}-E2`,
      title: "Deployment configuration and ownership",
      source: "Kubernetes API · demo snapshot",
      observedAt: time(clock),
      status: "available",
      excerpt: `namespace=cloudops-lab; UID recorded; ${pattern === "Load" ? "replicas=1; bounds=1–3; no HPA; capacity checked" : pattern === "CrashLoop" ? "revision=18; APP_MODE empty; known-good revision17 changes APP_MODE only" : "template and controller ownership captured"}`,
    },
    {
      id: `${id}-E3`,
      title: "Metric observation window",
      source: "Prometheus · illustrative demo values",
      observedAt: time(clock),
      status:
        pattern === "Pending" || pattern === "ImagePull"
          ? "unavailable"
          : "available",
      excerpt:
        pattern === "OOM"
          ? "10:30–10:45 UTC+7: memory MiB 170,215,255,180; limit256; restarts0,0,1,2"
          : pattern === "Load"
            ? "10:30–10:45 UTC+7: RPS40,90,160,180; p95ms110,180,560,820"
            : pattern === "CrashLoop"
              ? "10:30–10:45 UTC+7: restarts0,2,4,6; ready1,1,0,0"
              : "No metric trend is asserted for this diagnostic fixture.",
    },
  ];
  return {
    id,
    status: "completed",
    summary: fixture.summary,
    evidence,
    supportingEvidenceIds: evidence
      .filter((e) => e.status === "available")
      .map((e) => e.id),
    missingEvidence: [],
    ruledOut: [],
    unverified: [
      "Long-term application behavior outside this observation window.",
      "Historical changes not established by a single configuration snapshot.",
    ],
    window: "Last 30 minutes",
    question: `Investigate ${pattern} symptoms`,
    reason: "Alert-originated investigation",
    createdAt: time(clock),
    configurationVersion: "demo-config-v3",
    retrievalMode: "full_text",
    attempt: 1,
  };
}

function makePlan(
  incident: Incident,
  state: Pick<DemoState, "knownGood" | "healthyCandidates">,
): ActionPlan | null {
  if (
    incident.run.status !== "completed" ||
    ["Pending", "ImagePull"].includes(incident.pattern)
  )
    return null;
  if (state.healthyCandidates[incident.workloadId]) return null;
  if (incident.pattern === "CrashLoop" && !state.knownGood[incident.workloadId])
    return null;
  if (
    incident.pattern === "CrashLoop" &&
    (!incident.applicability.restoreDiff ||
      incident.applicability.externalConfig)
  )
    return null;
  if (
    incident.pattern === "Load" &&
    (!incident.applicability.headroom || incident.applicability.hpa)
  )
    return null;
  const template: Template =
    incident.pattern === "OOM"
      ? "set_memory_profile"
      : incident.pattern === "Load"
        ? "scale_deployment"
        : "restore_deployment_template";
  const diff =
    template === "set_memory_profile"
      ? [
          { field: "memory request", before: "128Mi", after: "256Mi" },
          { field: "memory limit", before: "256Mi", after: "512Mi" },
        ]
      : template === "scale_deployment"
        ? [{ field: "spec.replicas", before: "1", after: "3" }]
        : [
            {
              field: "APP_MODE (template)",
              before: "empty",
              after: "production (known-good revision 17)",
            },
          ];
  return {
    id: `${incident.id}-${incident.run.id}-plan`,
    template,
    hash: `demo-${incident.id}-${incident.run.id}-v3`,
    status: "awaiting_approval",
    runId: incident.run.id,
    targetUid: `demo-uid-${incident.workloadId}`,
    diff,
    prerequisites: [
      "Fresh target UID and relevant fields match the approved snapshot.",
      "Capacity is available and no conflicting controller owns the changed field.",
      ...(template === "restore_deployment_template"
        ? [
            "Known-good baseline confirmed while healthy; referenced ConfigMap/Secret contents are not restored.",
          ]
        : []),
    ],
    verification: [
      "Fresh rollout Ready/Available.",
      template === "scale_deployment"
        ? "Latency/error reach configured thresholds with adequate telemetry coverage."
        : "No new restart/OOM events in the configured verification window.",
      "Falling traffic or missing coverage makes recovery inconclusive.",
    ],
    risks: [
      "A rollout may interrupt requests.",
      "A larger memory profile does not establish that an application leak is fixed.",
    ],
    rollbackEligible: true,
    expiresAt: "2026-10-21T05:00:00.000Z",
    configurationVersion: "demo-config-v3",
  };
}

function makeIncident(workloadId: string, index: number): Incident {
  const workload = workloadOptions.find((w) => w.id === workloadId)!;
  const fixture = findings[workload.pattern];
  const run = makeRun(`${index}-run-1`, workload.pattern);
  return {
    id: `INC-${String(42 - index).padStart(3, "0")}`,
    title: fixture.title,
    workloadId,
    workload: workload.name,
    namespace: workload.namespace,
    pattern: workload.pattern,
    severity:
      workload.pattern === "Pending" || workload.pattern === "ImagePull"
        ? "warning"
        : "critical",
    status: "open",
    acknowledgedBy: null,
    impact: fixture.impact,
    run,
    runs: [copy(run)],
    plan: null,
    plans: [],
    execution: emptyExecution(),
    executions: [],
    notes: [],
    activity: [
      {
        id: `initial-${index}`,
        at: time(0),
        actor: "Alertmanager · demo",
        title: "Incident opened",
        detail:
          "Scoped Deployment identity validated; initial snapshot collected.",
      },
    ],
    resolutions: [],
    expectedOutcome:
      "Use fresh telemetry, configured thresholds and adequate coverage. Falling traffic or missing coverage is inconclusive.",
    manualReason: ["Pending", "ImagePull"].includes(workload.pattern)
      ? "Diagnostic-only pattern: follow the runbook and hand off to the resource owner."
      : null,
    freshness: "Demo snapshot · 21 Oct 2026, 10:30 UTC+7",
    demoOutcome: "success",
    applicability: {
      headroom: true,
      hpa: workload.controller === "HPA",
      restoreDiff: true,
      externalConfig: false,
      rollbackExpired: false,
      rollbackStale: false,
      rollbackUnsafe: false,
    },
  };
}

export function createInitialState(): DemoState {
  const state: DemoState = {
    scenarioId: "success",
    role: "operator",
    sessionActive: true,
    incidents: ["checkout", "catalog", "search", "worker", "media"].map(
      makeIncident,
    ),
    selectedIncidentId: "INC-042",
    knownGood: { catalog: false, payments: false },
    healthyCandidates: { catalog: false, payments: true },
    targetLocks: {},
    notice: "",
    error: "",
    clock: 0,
  };
  for (const incident of state.incidents) {
    incident.plan = makePlan(incident, state);
    if (incident.plan) incident.plans = [copy(incident.plan)];
    if (incident.pattern === "CrashLoop")
      incident.manualReason =
        "No confirmed known-good baseline. Use the restore-baseline scenario to prepare one while healthy.";
  }
  return state;
}

export function getIncident(
  state: DemoState,
  id = state.selectedIncidentId,
): Incident {
  return (
    state.incidents.find((incident) => incident.id === id) ?? state.incidents[0]
  );
}

export function createScenario(id: string): DemoState {
  const state = createInitialState();
  state.scenarioId = scenarioOptions.some((option) => option.id === id)
    ? id
    : "success";
  const selected: Record<string, string> = {
    restore: "INC-041",
    "restore-no-baseline": "INC-041",
    "restore-external-config": "INC-041",
    scale: "INC-040",
    "scale-hpa": "INC-040",
    "scale-no-headroom": "INC-040",
    pending: "INC-039",
    imagepull: "INC-038",
  };
  state.selectedIncidentId = selected[id] ?? "INC-042";
  const incident = getIncident(state);
  if (id === "restore") {
    state.healthyCandidates.catalog = true;
    incident.title = "Prepare a healthy restore baseline";
    incident.impact =
      "Healthy demo candidate: 1/1 Ready, revision 17, APP_MODE=production.";
    incident.run.status = "queued";
    incident.run.summary =
      "Confirm the healthy template as known-good, then explicitly inject the demo startup fault.";
    incident.run.evidence = [];
    incident.run.supportingEvidenceIds = [];
    incident.manualReason =
      "Baseline preparation: confirm known-good in Workload detail before injecting the simulated fault.";
    incident.plan = null;
    incident.plans = [];
  }
  if (id === "partial") {
    incident.run.status = "insufficient_evidence";
    incident.run.summary =
      "A memory-limit cause cannot be established: previous termination state and metric coverage are unavailable.";
    incident.run.evidence = incident.run.evidence.map((e) => ({
      ...e,
      status: e.id.endsWith("E2") ? "available" : "unavailable",
      excerpt: e.id.endsWith("E2")
        ? e.excerpt
        : "Unavailable; no measurement or termination reason inferred.",
    }));
    incident.run.supportingEvidenceIds = [incident.run.evidence[1].id];
    incident.run.missingEvidence = [
      "Previous termination state",
      "Memory metrics for the restart window",
    ];
    incident.plan = null;
    incident.plans = [];
    incident.manualReason =
      "Collect missing evidence before proposing a change.";
  }
  if (id === "quota") {
    incident.run.status = "waiting_for_quota";
    incident.run.summary =
      "Simulated 429: evidence is retained; no finding generated for this attempt.";
    incident.plan = null;
    incident.plans = [];
    incident.manualReason = "Waiting for provider quota. No paid fallback.";
  }
  if (id === "stale" && incident.plan) incident.plan.status = "stale";
  if (id === "expired" && incident.plan) incident.plan.status = "expired";
  if (id === "rollback") incident.demoOutcome = "verification_failed";
  if (id === "failed-rollback") incident.demoOutcome = "rollback_failed";
  if (id.startsWith("rollback-")) {
    incident.demoOutcome = "verification_failed";
    incident.applicability.rollbackExpired = id === "rollback-expired";
    incident.applicability.rollbackStale = id === "rollback-stale";
    incident.applicability.rollbackUnsafe = id === "rollback-unsafe";
  }
  if (id === "scale-hpa" || id === "scale-no-headroom") {
    incident.applicability.hpa = id === "scale-hpa";
    incident.applicability.headroom = id !== "scale-no-headroom";
    incident.plan = null;
    incident.plans = [];
    incident.manualReason =
      id === "scale-hpa"
        ? "HPA owns replicas. No scale proposal is allowed; coordinate the controller policy manually."
        : "Insufficient node headroom. Scaling would not be safe; review capacity manually.";
    incident.run.summary = incident.manualReason;
    incident.run.evidence[1].excerpt =
      id === "scale-hpa"
        ? "HPA owns spec.replicas; manual controller coordination required."
        : "Node allocatable memory/CPU cannot fit additional replicas.";
  }
  if (id === "restore-external-config") {
    state.knownGood.catalog = true;
    incident.applicability.externalConfig = true;
    incident.applicability.restoreDiff = false;
    incident.plan = null;
    incident.plans = [];
    incident.manualReason =
      "Referenced ConfigMap changed outside the pod template. Restore diff is empty; this action cannot restore external configuration.";
    incident.run.summary = incident.manualReason;
    incident.run.evidence[1].excerpt =
      "Pod template unchanged; referenced ConfigMap metadata is current only. Historical contents unavailable; no Secret is read.";
    incident.run.unverified.push(
      "Historical external configuration contents and exact change time.",
    );
  }
  if (id === "failed-rerun") {
    beginRun(state, incident, "Check fresh evidence after operator changes");
    incident.run.status = "failed";
    incident.run.summary =
      "Simulated endpoint timeout. No replacement finding generated; previous completed run is preserved.";
    incident.manualReason =
      "Retry the failed attempt or inspect the earlier completed finding; do not treat it as a fresh conclusion.";
  }
  if (id === "uncertain") incident.demoOutcome = "uncertain";
  if (id === "no-op") incident.demoOutcome = "no_op";
  if (id === "inconclusive") incident.demoOutcome = "inconclusive";
  if (id === "viewer") state.role = "viewer";
  if (id === "session-expired") state.sessionActive = false;
  syncRun(incident);
  syncPlan(incident);
  return state;
}

function syncRun(incident: Incident) {
  const index = incident.runs.findIndex((run) => run.id === incident.run.id);
  if (index < 0) incident.runs.push(copy(incident.run));
  else incident.runs[index] = copy(incident.run);
}
function syncPlan(incident: Incident) {
  if (!incident.plan) return;
  const index = incident.plans.findIndex(
    (plan) => plan.id === incident.plan!.id,
  );
  if (index < 0) incident.plans.push(copy(incident.plan));
  else incident.plans[index] = copy(incident.plan);
}
function syncExecution(incident: Incident) {
  if (!incident.execution.id) return;
  const index = incident.executions.findIndex(
    (execution) => execution.id === incident.execution.id,
  );
  if (index < 0) incident.executions.push(copy(incident.execution));
  else incident.executions[index] = copy(incident.execution);
}
function activeExecution(incident: Incident) {
  return [
    "queued",
    "executing",
    "verifying",
    "uncertain",
    "rolling_back",
  ].includes(incident.execution.status);
}
function workingRun(incident: Incident) {
  return ["queued", "collecting", "analyzing", "waiting_for_quota"].includes(
    incident.run.status,
  );
}
function unusedPlan(incident: Incident) {
  return (
    !!incident.plan &&
    (incident.plan.status === "awaiting_approval" ||
      (incident.plan.status === "approved" &&
        incident.execution.status === "none"))
  );
}
function addActivity(
  state: DemoState,
  incident: Incident,
  title: string,
  detail: string,
) {
  incident.activity.push({
    id: `event-${state.clock}-${incident.activity.length}`,
    at: time(state.clock),
    actor: "Alex · Operator · demo",
    title,
    detail,
  });
}
function beginRun(
  state: DemoState,
  incident: Incident,
  reason: string,
  question = incident.run.question,
  window = incident.run.window,
) {
  if (incident.plan && unusedPlan(incident)) {
    incident.plan.status = "stale";
    syncPlan(incident);
  }
  if (incident.runs.length) syncRun(incident);
  const run = makeRun(
    `${incident.id}-run-${incident.runs.length + 1}`,
    incident.pattern,
    state.clock,
  );
  run.status = "queued";
  run.summary = "Queued for scoped evidence collection.";
  run.reason = reason;
  run.question = question;
  run.window = window;
  run.evidence = [];
  run.supportingEvidenceIds = [];
  incident.run = run;
  incident.runs.push(copy(run));
  incident.plan = null;
  incident.manualReason = null;
  addActivity(state, incident, "Investigation queued", `${reason} · ${window}`);
}
function finishRun(state: DemoState, incident: Incident) {
  const prior = incident.run;
  incident.run = {
    ...makeRun(prior.id, incident.pattern, state.clock),
    question: prior.question,
    window: prior.window,
    reason: prior.reason,
    attempt: prior.attempt,
  };
  if (state.healthyCandidates[incident.workloadId]) {
    incident.title = "Workload healthy in the observed snapshot";
    incident.impact =
      "Rollout Ready/Available; no supported incident impact established.";
    incident.run.summary =
      "Workload is healthy in this snapshot. No supported failure or executable recovery is established.";
    incident.run.evidence = incident.run.evidence.map((evidence, index) => ({
      ...evidence,
      status: index === 2 ? "unavailable" : "available",
      excerpt:
        index === 0
          ? "Current rollout Ready/Available; no termination or failure event in the observed snapshot."
          : index === 1
            ? "Fresh target UID/template recorded for the healthy candidate."
            : "No metric series attached; no trend inferred.",
    }));
    incident.run.supportingEvidenceIds = incident.run.evidence
      .filter((evidence) => evidence.status === "available")
      .map((evidence) => evidence.id);
    incident.plan = null;
    incident.manualReason =
      "Healthy workload: no recovery action is applicable. Keep the observations or hand off the question.";
    syncRun(incident);
    return;
  }
  incident.plan = makePlan(incident, state);
  incident.manualReason = incident.plan
    ? null
    : incident.pattern === "CrashLoop"
      ? incident.applicability.externalConfig ||
        !incident.applicability.restoreDiff
        ? "External configuration is unverified or restore diff is empty; template restore cannot repair it."
        : "No confirmed healthy baseline; restore is unavailable."
      : incident.pattern === "Load"
        ? incident.applicability.hpa
          ? "HPA owns replicas; coordinate the controller manually."
          : "Insufficient node headroom; no scale proposal."
        : "Diagnostic-only pattern: follow the runbook and hand off.";
  syncRun(incident);
  syncPlan(incident);
}

export function reducer(previous: DemoState, action: DemoAction): DemoState {
  if (action.type === "load_scenario") return createScenario(action.id);
  const state = copy(previous);
  state.notice = "";
  state.error = "";
  const fail = (message: string) => {
    state.error = message;
    return state;
  };
  if (action.type === "clear_feedback") return state;
  if (action.type === "select_incident") {
    if (!state.incidents.some((incident) => incident.id === action.id))
      return fail("Incident not found in this demo scope.");
    state.selectedIncidentId = action.id;
    return state;
  }
  if (action.type === "set_role") {
    state.role = action.role;
    state.notice = `Sample ${action.role} identity selected.`;
    return state;
  }
  if (action.type === "set_session") {
    state.sessionActive = action.active;
    state.notice = action.active
      ? "Demo session restored."
      : "Demo session expired.";
    return state;
  }
  if (!state.sessionActive)
    return fail(
      "Session expired. Restore the demo session before changing data.",
    );
  if (state.role !== "operator")
    return fail(
      "Viewer access is read-only. An Operator is required for this action.",
    );
  state.clock++;
  if (action.type === "confirm_known_good") {
    if (!workloadOptions.some((workload) => workload.id === action.workloadId))
      return fail("Workload is outside the configured scope.");
    if (!state.healthyCandidates[action.workloadId])
      return fail(
        "Known-good confirmation requires a fresh healthy candidate. A failed template cannot be marked healthy.",
      );
    state.knownGood[action.workloadId] = true;
    state.notice =
      "Healthy demo candidate confirmed with UID, template digest and stability window. No cluster write.";
    return state;
  }
  if (action.type === "inject_failure") {
    if (
      action.workloadId !== "catalog" ||
      !state.knownGood.catalog ||
      !state.healthyCandidates.catalog
    )
      return fail(
        "Confirm the healthy catalog baseline before injecting this demo fault.",
      );
    const incident = state.incidents.find(
      (item) => item.workloadId === "catalog",
    )!;
    state.selectedIncidentId = incident.id;
    state.healthyCandidates.catalog = false;
    incident.title = findings.CrashLoop.title;
    incident.impact = findings.CrashLoop.impact;
    beginRun(
      state,
      incident,
      "Explicit demo startup fault injected after healthy baseline confirmation",
    );
    state.notice =
      "Demo fault injected locally; advance the new investigation.";
    return state;
  }
  if (action.type === "create_investigation") {
    const workload = workloadOptions.find(
      (option) => option.id === action.workloadId,
    );
    if (!workload) return fail("Workload is outside the configured scope.");
    if (!action.question.trim() || !action.window.trim())
      return fail("A scoped question and time window are required.");
    const incident = makeIncident(workload.id, state.incidents.length);
    incident.id = `INC-${String(43 + state.incidents.length).padStart(3, "0")}`;
    incident.title = `Manual investigation · ${workload.name}`;
    incident.impact = "Investigation requested; impact not yet established.";
    incident.activity = [];
    incident.runs = [];
    incident.plan = null;
    incident.plans = [];
    incident.run.question = action.question;
    beginRun(
      state,
      incident,
      `Manual investigation${action.symptom ? ": " + action.symptom : ""}`,
      action.question.trim(),
      action.window,
    );
    state.incidents.unshift(incident);
    state.selectedIncidentId = incident.id;
    state.notice = "Scoped demo incident created. No backend request was sent.";
    return state;
  }
  const requestedId = "incidentId" in action ? action.incidentId : undefined;
  if (
    requestedId &&
    !state.incidents.some((incident) => incident.id === requestedId)
  )
    return fail("Incident not found in this demo scope.");
  const incident = getIncident(state, requestedId);
  const reason = "reason" in action ? (action.reason?.trim() ?? "") : "";
  if (
    [
      "add_note",
      "reinvestigate",
      "reject",
      "resolve",
      "reopen",
      "request_rollback",
      "manual_handoff",
      "verify_manually",
    ].includes(action.type) &&
    !reason
  )
    return fail("A reason is required.");
  const blockedTarget = state.targetLocks[incident.workloadId];
  switch (action.type) {
    case "acknowledge":
      if (incident.status === "resolved")
        return fail("Reopen this incident before acknowledging it.");
      incident.status = "acknowledged";
      incident.acknowledgedBy = "Alex";
      addActivity(state, incident, "Acknowledged", "Alex took ownership.");
      break;
    case "add_note":
      incident.notes.push({
        id: `note-${state.clock}`,
        at: time(state.clock),
        actor: "Alex",
        text: reason,
      });
      addActivity(state, incident, "Note added", reason);
      break;
    case "reinvestigate":
      if (activeExecution(incident))
        return fail(
          "Execution or reconciliation is active. A new run cannot reset its outcome.",
        );
      if (incident.status === "resolved")
        return fail("Reopen the incident before a new investigation.");
      if (workingRun(incident))
        return fail(
          "Finish or cancel the existing investigation before starting another.",
        );
      beginRun(state, incident, reason);
      break;
    case "retry":
      if (activeExecution(incident))
        return fail("Execution or reconciliation is active.");
      if (
        ![
          "waiting_for_quota",
          "failed",
          "insufficient_evidence",
          "cancelled",
        ].includes(incident.run.status)
      )
        return fail("This investigation is not in a retryable state.");
      if (incident.status === "resolved")
        return fail("Reopen this incident before retrying.");
      beginRun(state, incident, "Operator requested a new bounded retry");
      break;
    case "cancel_run":
      if (!["queued", "waiting_for_quota"].includes(incident.run.status))
        return fail(
          "Only queued or quota-waiting investigations can be cancelled; an in-flight provider call is not cancelled.",
        );
      incident.run.status = "cancelled";
      incident.run.summary = "Cancelled before dispatch.";
      addActivity(
        state,
        incident,
        "Investigation cancelled",
        "No in-flight provider cancellation is claimed.",
      );
      break;
    case "stale_plan":
    case "expire_plan":
      if (!incident.plan || incident.plan.status !== "awaiting_approval")
        return fail("Only an unused proposal can become stale or expire.");
      incident.plan.status = action.type === "stale_plan" ? "stale" : "expired";
      addActivity(
        state,
        incident,
        "Proposal unavailable",
        incident.plan.status,
      );
      break;
    case "approve":
      if (
        incident.status === "resolved" ||
        !incident.plan ||
        incident.plan.status !== "awaiting_approval"
      )
        return fail("This proposal is not awaiting approval.");
      if (blockedTarget)
        return fail(
          `Target is locked by ${blockedTarget.incidentId}: ${blockedTarget.reason}`,
        );
      if (
        state.incidents.some(
          (other) =>
            other.workloadId === incident.workloadId && activeExecution(other),
        )
      )
        return fail("Another execution already owns this Deployment.");
      incident.plan.status = "approved";
      incident.execution = {
        ...emptyExecution(),
        id: `${incident.plan.id}-execution`,
        status: "queued",
        rollbackEligible: incident.plan.rollbackEligible,
        summary:
          "Approval recorded; executor will check the fresh target before writing.",
      };
      state.targetLocks[incident.workloadId] = {
        incidentId: incident.id,
        executionId: incident.execution.id,
        reason: "Approved execution pending a known outcome.",
      };
      addActivity(
        state,
        incident,
        "Plan approved",
        `Exact hash ${incident.plan.hash}. ${reason}`,
      );
      break;
    case "reject":
      if (!incident.plan || incident.plan.status !== "awaiting_approval")
        return fail("This proposal cannot be rejected now.");
      incident.plan.status = "rejected";
      addActivity(state, incident, "Plan rejected", reason);
      break;
    case "resolve":
      if (incident.status === "resolved")
        return fail("This incident is already resolved.");
      if (activeExecution(incident))
        return fail(
          "Cannot resolve while execution, verification or uncertain reconciliation is active.",
        );
      if (incident.plan && unusedPlan(incident))
        incident.plan.status = "invalidated";
      if (["queued", "waiting_for_quota"].includes(incident.run.status)) {
        incident.run.status = "cancelled";
        incident.run.summary =
          "Undispatched investigation cancelled by incident resolution.";
      }
      if (["collecting", "analyzing"].includes(incident.run.status))
        return fail(
          "Wait for the active investigation to finish before resolving.",
        );
      incident.status = "resolved";
      incident.resolutions.push({
        id: `${incident.id}-resolution-${incident.resolutions.length + 1}`,
        at: time(state.clock),
        actor: "Alex",
        reason,
        run: copy(incident.run),
        plan: copy(incident.plan),
        execution: copy(incident.execution),
        notes: copy(incident.notes),
        impact: incident.impact,
        title: incident.title,
        workload: incident.workload,
        namespace: incident.namespace,
      });
      addActivity(
        state,
        incident,
        "Incident resolved",
        reason +
          (incident.execution.outcomeUnknown
            ? " · Unknown execution outcome retained; target remains locked."
            : ""),
      );
      break;
    case "reopen":
      if (incident.status !== "resolved")
        return fail("Only resolved incidents can be reopened.");
      incident.status = "open";
      addActivity(state, incident, "Incident reopened", reason);
      break;
    case "request_rollback": {
      if (incident.status === "resolved")
        return fail(
          "Reopen the incident before requesting rollback; a resolved incident cannot start a new write.",
        );
      const originatingPlan = incident.plans.find(
        (plan) => `${plan.id}-execution` === incident.execution.id,
      );
      if (!originatingPlan || originatingPlan.status !== "approved")
        return fail(
          "The approved originating plan for this execution is unavailable. Do not derive rollback from the latest proposal.",
        );
      if (
        !["failed", "succeeded"].includes(incident.execution.status) ||
        !incident.execution.rollbackEligible ||
        incident.execution.outcomeUnknown
      )
        return fail(
          "Rollback is not eligible: require a known outcome, safe inverse diff and fresh matching fields.",
        );
      if (!originatingPlan.rollbackEligible)
        return fail("The originating plan does not permit rollback.");
      if (blockedTarget && blockedTarget.incidentId !== incident.id)
        return fail("Another execution owns this target.");
      if (incident.applicability.rollbackExpired)
        return fail(
          "Rollback window expired; its expiry is separate from original plan approval.",
        );
      if (incident.applicability.rollbackStale)
        return fail(
          "Rollback target fields changed; an inverse write would overwrite newer state.",
        );
      if (incident.applicability.rollbackUnsafe)
        return fail(
          "Old memory profile is unsafe after OOM. Rollback is denied; hand off for manual intervention.",
        );
      if (
        incident.plan &&
        incident.plan.id !== originatingPlan.id &&
        unusedPlan(incident)
      ) {
        incident.plan.status = "stale";
        syncPlan(incident);
      }
      incident.execution.status = "rolling_back";
      incident.execution.summary =
        "Operator confirmed the inverse diff. Fresh UID, fields, expiry and old-profile safety checked in this demo.";
      state.targetLocks[incident.workloadId] = {
        incidentId: incident.id,
        executionId: incident.execution.id,
        reason: "Rollback is active.",
      };
      addActivity(
        state,
        incident,
        "Rollback approved",
        `${reason} · originating plan ${originatingPlan.id} · ${originatingPlan.hash}`,
      );
      break;
    }
    case "reconcile":
      if (
        !incident.execution.outcomeUnknown ||
        !["uncertain", "manual_intervention"].includes(
          incident.execution.status,
        )
      )
        return fail("There is no unknown execution outcome to reconcile.");
      if (incident.execution.reconcileAttempts >= 2)
        return fail(
          "Reconciliation attempt budget exhausted. Record a manual handoff or evidence-backed verification; no additional automatic read is dispatched.",
        );
      incident.execution.reconcileAttempts++;
      if (incident.execution.reconcileAttempts >= 2) {
        incident.execution.status = "manual_intervention";
        incident.execution.summary =
          "Reconciliation budget exhausted. API access remains unavailable; outcome unknown. Manual handoff is available; target remains locked.";
      } else
        incident.execution.summary =
          "Bounded fresh read attempted; API unavailable. No write retry was sent.";
      addActivity(
        state,
        incident,
        "Reconciliation attempted",
        incident.execution.summary,
      );
      break;
    case "manual_handoff":
      if (activeExecution(incident))
        return fail(
          "Finish the active execution or bounded reconciliation before recording a manual handoff.",
        );
      if (
        incident.execution.outcomeUnknown &&
        incident.execution.status !== "manual_intervention"
      )
        return fail(
          "Complete bounded reconciliation before the unknown-outcome handoff.",
        );
      incident.notes.push({
        id: `handoff-${state.clock}`,
        at: time(state.clock),
        actor: "Alex",
        text: reason,
      });
      addActivity(
        state,
        incident,
        "Manual handoff recorded",
        reason +
          (incident.execution.outcomeUnknown
            ? " · Outcome remains unknown and target lock remains."
            : " · Evidence and open questions retained for the receiving operator."),
      );
      break;
    case "verify_manually":
      if (
        !incident.execution.outcomeUnknown ||
        incident.execution.status !== "manual_intervention"
      )
        return fail(
          "Manual verification is available after bounded unknown-outcome reconciliation.",
        );
      if (
        !action.evidence.trim() ||
        !action.oldRequestStopped ||
        !action.outcome
      )
        return fail(
          "Record fresh evidence, the observed outcome, and confirmation that the old worker/request cannot write late. No blind unlock.",
        );
      incident.execution.outcomeUnknown = false;
      incident.execution.manualEvidence = action.evidence.trim();
      incident.execution.status = "manual_intervention";
      incident.execution.rollbackEligible = false;
      incident.execution.summary =
        action.outcome === "applied"
          ? "Manual evidence establishes the desired state; causal recovery is not attributed to the uncertain request."
          : "Manual evidence establishes that the desired change is not present.";
      delete state.targetLocks[incident.workloadId];
      addActivity(
        state,
        incident,
        "Target reconciled and unlocked",
        `${reason} · ${action.evidence} · old worker/request confirmed unable to write late.`,
      );
      break;
    case "advance": {
      const advancingRun = ![
        "queued",
        "executing",
        "verifying",
        "rolling_back",
        "uncertain",
      ].includes(incident.execution.status);
      if (incident.execution.status === "queued") {
        incident.execution.status = "executing";
        incident.execution.summary =
          "Fresh UID, fields, scope, capacity and controller checks passed; simulated conditional write started.";
      } else if (incident.execution.status === "executing") {
        if (incident.demoOutcome === "uncertain") {
          incident.execution.status = "uncertain";
          incident.execution.outcomeUnknown = true;
          incident.execution.rollbackEligible = false;
          incident.execution.summary =
            "Write response lost. Outcome unknown; do not resend the write.";
        } else if (incident.demoOutcome === "no_op") {
          incident.execution.status = "no_op";
          incident.execution.rollbackEligible = false;
          incident.execution.summary =
            "Workload already recovered before write. No mutation sent; not counted as remediation success.";
          delete state.targetLocks[incident.workloadId];
        } else {
          incident.execution.status = "verifying";
          incident.execution.summary =
            "Conditional write acknowledged; checking fresh rollout, event deltas and telemetry.";
        }
      } else if (incident.execution.status === "verifying") {
        if (incident.demoOutcome === "inconclusive") {
          incident.execution.status = "manual_intervention";
          incident.execution.rollbackEligible = false;
          incident.execution.summary =
            "Rollout Ready, but traffic fell and metric coverage is insufficient. Recovery is inconclusive.";
        } else if (
          ["verification_failed", "rollback_failed"].includes(
            incident.demoOutcome,
          )
        ) {
          incident.execution.status = "failed";
          incident.execution.summary =
            "Fresh post-check failed: restart/rollout criteria were not met. Review the inverse diff before rollback.";
        } else {
          incident.execution.status = "succeeded";
          incident.execution.summary =
            "Fresh rollout and telemetry meet the configured criteria within the observed window. No claim of long-term leak resolution.";
          incident.execution.verification = [
            "Rollout Ready/Available",
            "No new OOM/restarts in configured window",
            "Telemetry coverage 96% (demo fixture)",
          ];
        }
        delete state.targetLocks[incident.workloadId];
      } else if (incident.execution.status === "rolling_back") {
        incident.execution.status =
          incident.demoOutcome === "rollback_failed"
            ? "manual_intervention"
            : "rolled_back";
        incident.execution.rollbackEligible = false;
        incident.execution.summary =
          incident.demoOutcome === "rollback_failed"
            ? "Rollback precondition changed; inverse write was not sent. Manual intervention required."
            : "Approved inverse diff applied; fresh post-check confirms the restored fields.";
        delete state.targetLocks[incident.workloadId];
      } else if (incident.execution.status === "uncertain")
        return fail(
          "Unknown write outcome: request bounded reconciliation; do not advance by replaying a write.",
        );
      else if (incident.run.status === "queued") {
        if (state.scenarioId === "restore" && state.healthyCandidates.catalog)
          return fail(
            "Confirm known-good and inject the explicit demo fault before collecting the restore incident.",
          );
        incident.run.status = "collecting";
        incident.run.summary =
          "Collecting scoped Kubernetes and Prometheus snapshots.";
      } else if (incident.run.status === "collecting") {
        incident.run.status = "analyzing";
        incident.run.summary =
          "Preparing bounded evidence and versioned runbook context; one model request is simulated.";
      } else if (incident.run.status === "analyzing")
        finishRun(state, incident);
      else
        return fail(
          "Nothing is currently advancing. Choose the next explicit operator action.",
        );
      addActivity(
        state,
        incident,
        "Demo stage advanced",
        advancingRun ? incident.run.summary : incident.execution.summary,
      );
      break;
    }
  }
  syncRun(incident);
  syncPlan(incident);
  syncExecution(incident);
  if (!state.notice)
    state.notice = "Demo state updated locally. No live system was changed.";
  return state;
}

export function handoffMarkdown(
  incident: Incident,
  resolutionId?: string,
): string {
  const resolution = resolutionId
    ? incident.resolutions.find((item) => item.id === resolutionId)
    : undefined;
  if (resolutionId && !resolution) return "Resolution snapshot not found.";
  const source = resolution ?? incident;
  const run = source.run,
    execution = source.execution;
  const title = resolution?.title ?? incident.title;
  return [
    `# ${incident.id} · ${title}`,
    "**DEMO DATA — no live operations**",
    `Workload: ${source.namespace}/${source.workload}`,
    `Impact: ${source.impact}`,
    `Investigation: ${run.id} · ${run.status}`,
    `Finding: ${run.summary}`,
    "Evidence:",
    ...run.evidence.map(
      (e) =>
        `- ${e.id}: ${e.title} · ${e.source} · ${e.observedAt} · ${e.status}`,
    ),
    `Ruled out: ${run.ruledOut.join("; ") || "None established."}`,
    `Unverified: ${run.unverified.join("; ") || "None recorded."}`,
    `Missing: ${run.missingEvidence.join("; ") || "None recorded."}`,
    `Plan: ${source.plan ? source.plan.template + " · " + source.plan.status + " · " + source.plan.hash : "Manual guidance; no executable plan."}`,
    `Execution: ${execution.status}; outcome_unknown=${execution.outcomeUnknown}`,
    execution.summary,
    ...(execution.manualEvidence
      ? [`Manual verification evidence: ${execution.manualEvidence}`]
      : []),
    `Incident: ${resolution ? "resolved" : incident.status}`,
    ...(resolution
      ? [
          `Resolution ${resolution.id}: ${resolution.reason} · ${resolution.actor} · ${resolution.at}`,
        ]
      : []),
    "Notes:",
    ...source.notes.map((note) => `- ${note.actor} · ${note.at}: ${note.text}`),
    "Open questions: sustained behavior outside the observed window; any unverified checks above.",
  ].join("\n");
}
