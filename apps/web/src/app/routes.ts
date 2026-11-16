const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface WorkQueueParams {
  search?: string;
  workload?: string;
  origin?: "operator" | "alertmanager";
  state?: "open" | "acknowledged" | "resolved";
  severity?: string;
  alert_signal?: "none" | "firing" | "resolved";
  sort_by?: "created_at" | "first_seen_at" | "last_seen_at" | "severity";
  sort_order?: "asc" | "desc";
  offset?: number;
  selected?: string;
  view?: "all" | "needs_attention" | "awaiting_decision" | "verifying" | "blocked" | "resolved";
  run_state?: "queued" | "collecting" | "analyzing" | "waiting_for_quota" | "insufficient_evidence" | "completed" | "failed" | "cancelled";
  execution_class?: "active" | "terminal_unknown" | "terminal" | "unrecognized";
  anchor_created_at?: string;
  anchor_id?: string;
}

const ORIGINS = ["operator", "alertmanager"] as const;
const STATES = ["open", "acknowledged", "resolved"] as const;
const ALERT_SIGNALS = ["none", "firing", "resolved"] as const;
const SORT_FIELDS = ["created_at", "first_seen_at", "last_seen_at", "severity"] as const;
const SORT_ORDERS = ["asc", "desc"] as const;
const QUEUE_VIEWS = [
  "all",
  "needs_attention",
  "awaiting_decision",
  "verifying",
  "blocked",
  "resolved",
] as const;
const EXECUTION_CLASSES = ["active", "terminal_unknown", "terminal", "unrecognized"] as const;
const RUN_STATES = [
  "queued",
  "collecting",
  "analyzing",
  "waiting_for_quota",
  "insufficient_evidence",
  "completed",
  "failed",
  "cancelled",
] as const;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : undefined;
}

export function parseWorkQueueHash(hash: string): WorkQueueParams {
  const defaults: WorkQueueParams = { sort_by: "last_seen_at", sort_order: "desc", offset: 0 };
  const match = /^#\/queue(?:\?([^#]*))?$/.exec(hash);
  if (!match) return defaults;
  const params = new URLSearchParams(match[1] ?? "");
  const parsed: WorkQueueParams = { ...defaults };

  const search = params.get("search");
  if (search && search.length <= 160) parsed.search = search;
  const workload = params.get("workload");
  if (workload && uuid.test(workload)) parsed.workload = workload;
  const origin = oneOf(params.get("origin"), ORIGINS);
  if (origin) parsed.origin = origin;
  const state = oneOf(params.get("state"), STATES);
  if (state) parsed.state = state;
  const severity = params.get("severity");
  if (severity && severity.length <= 32) parsed.severity = severity;
  const alertSignal = oneOf(params.get("alert_signal"), ALERT_SIGNALS);
  if (alertSignal) parsed.alert_signal = alertSignal;
  const sortBy = oneOf(params.get("sort_by"), SORT_FIELDS);
  if (sortBy) parsed.sort_by = sortBy;
  const sortOrder = oneOf(params.get("sort_order"), SORT_ORDERS);
  if (sortOrder) parsed.sort_order = sortOrder;
  const offset = Number(params.get("offset") ?? "0");
  if (Number.isSafeInteger(offset) && offset >= 0 && offset <= 100000) parsed.offset = offset;
  const selected = params.get("selected");
  if (selected && uuid.test(selected)) parsed.selected = selected;
  const view = oneOf(params.get("view"), QUEUE_VIEWS);
  if (view) parsed.view = view;
  const runState = oneOf(params.get("run_state"), RUN_STATES);
  if (runState) parsed.run_state = runState;
  const executionClass = oneOf(params.get("execution_class"), EXECUTION_CLASSES);
  if (executionClass) parsed.execution_class = executionClass;
  const anchorCreatedAt = params.get("anchor_created_at");
  const anchorId = params.get("anchor_id");
  if (
    anchorCreatedAt &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(anchorCreatedAt) &&
    Number.isFinite(Date.parse(anchorCreatedAt)) &&
    anchorId &&
    uuid.test(anchorId)
  ) {
    parsed.anchor_created_at = anchorCreatedAt;
    parsed.anchor_id = anchorId;
  }

  return parsed;
}

export function buildWorkQueueHash(params: WorkQueueParams): string {
  const query = new URLSearchParams();
  if (params.search) query.set("search", params.search);
  if (params.workload) query.set("workload", params.workload);
  if (params.origin) query.set("origin", params.origin);
  if (params.state) query.set("state", params.state);
  if (params.severity) query.set("severity", params.severity);
  if (params.alert_signal) query.set("alert_signal", params.alert_signal);
  if (params.sort_by && params.sort_by !== "last_seen_at") query.set("sort_by", params.sort_by);
  if (params.sort_order && params.sort_order !== "desc") query.set("sort_order", params.sort_order);
  if (params.offset) query.set("offset", params.offset.toString());
  if (params.selected) query.set("selected", params.selected);
  if (params.view) query.set("view", params.view);
  if (params.run_state) query.set("run_state", params.run_state);
  if (params.execution_class) query.set("execution_class", params.execution_class);
  if (params.anchor_created_at && params.anchor_id) {
    query.set("anchor_created_at", params.anchor_created_at);
    query.set("anchor_id", params.anchor_id);
  }

  const search = query.toString();
  return `#/queue${search ? `?${search}` : ""}`;
}

/** Only queue hashes may be used as in-app return targets. */
export function queueReturnHash(hash: string): string {
  if (!/^#\/queue(?:\?[^#]*)?$/.test(hash)) return "#/queue";
  return buildWorkQueueHash(parseWorkQueueHash(hash));
}

export function incidentReturnHash(hash: string): string {
  const match = /^#\/incidents\/[^/?#]+(?:\?([^#]*))?$/.exec(hash);
  const target = new URLSearchParams(match?.[1] ?? "").get("return_to");
  return target ? queueReturnHash(target) : "#/queue";
}

export interface EvidenceSelection {
  plan?: string;
  execution?: string;
  leftIncident?: string;
  leftRun?: string;
  rightIncident?: string;
  rightRun?: string;
  changedOnly?: boolean;
  differenceStatus?: string;
  source?: string;
  catalog?: string;
  locatorKind?: string;
  locatorKey?: string;
}

const extraParams = {
  plan: "plan", execution: "execution",
  leftIncident: "compare_left_incident", leftRun: "compare_left_run",
  rightIncident: "compare_incident", rightRun: "compare_run",
  source: "compare_source", catalog: "compare_catalog",
  locatorKind: "locator_kind", locatorKey: "locator_key",
  differenceStatus: "compare_status",
} as const;

export function buildIncidentHash(id: string, run = "", returnTo = "#/queue", resolution = "", evidence = "", selection: EvidenceSelection = {}): string {
  const query = new URLSearchParams();
  if (run) query.set("run", run);
  if (evidence && run) query.set("evidence", evidence);
  if (resolution) query.set("resolution", resolution);
  for (const [field, key] of Object.entries(extraParams)) {
    if ((field === "locatorKind" || field === "locatorKey") && (!run || !evidence)) continue;
    const value = selection[field as keyof typeof extraParams];
    if (value) query.set(key, value);
  }
  if (selection.changedOnly) query.set("compare_filter", "changed");
  const target = queueReturnHash(returnTo);
  if (target !== "#/queue") query.set("return_to", target);
  const suffix = query.toString();
  return `#/incidents/${id}${suffix ? `?${suffix}` : ""}`;
}

export interface KnowledgeRoute {
  search?: string;
  tags?: string[];
  document?: string;
  chunk?: string;
  incident?: string;
  run?: string;
  selection?: string;
  returnTo?: string;
}

function knowledgeReturnTarget(hash: string): string | undefined {
  if (hash.length > 4096 || !hash.startsWith("#/incidents/")) return undefined;
  const route = safeRoute(hash);
  if (route.page !== "incidents" || !route.run) return undefined;
  return buildIncidentHash(route.id, route.run, incidentReturnHash(hash), route.resolution, route.evidence, route.selection);
}

export function buildKnowledgeHash(route: KnowledgeRoute = {}): string {
  const params = new URLSearchParams();
  if (route.search) params.set("q", route.search);
  for (const tag of route.tags ?? []) params.append("tag", tag);
  for (const key of ["document", "chunk", "incident", "run", "selection"] as const)
    if (route[key]) params.set(key, route[key]);
  const target = route.returnTo && knowledgeReturnTarget(route.returnTo);
  if (target) params.set("return_to", target);
  return `#/knowledge${params.size ? `?${params}` : ""}`;
}

function parseKnowledgeHash(hash: string): KnowledgeRoute | null {
  const match = /^#\/knowledge(?:\?([^#]*))?$/.exec(hash);
  if (!match) return null;
  const params = new URLSearchParams(match[1] ?? "");
  const allowed = ["q", "tag", "document", "chunk", "incident", "run", "selection", "return_to"];
  if ([...params.keys()].some((key) => !allowed.includes(key) || key !== "tag" && params.getAll(key).length > 1)) return null;
  const search = params.get("q") || undefined;
  const tags = params.getAll("tag");
  if ((search?.length ?? 0) > 2000 || tags.length > 16 || new Set(tags).size !== tags.length || tags.some((tag) => !tag || tag.length > 64)) return null;
  const route: KnowledgeRoute = { ...(search ? { search } : {}), ...(tags.length ? { tags } : {}) };
  for (const key of ["document", "chunk", "incident", "run", "selection"] as const) {
    const value = params.get(key);
    if (value !== null) {
      if (!uuid.test(value)) return null;
      route[key] = value;
    }
  }
  if (route.chunk && !route.document) return null;
  if ((route.incident || route.run || route.selection) && !(route.incident && route.run && route.selection && route.document && route.chunk)) return null;
  const returnTo = params.get("return_to");
  if (returnTo !== null) {
    const target = knowledgeReturnTarget(returnTo);
    if (!target) return null;
    route.returnTo = target;
  }
  return route;
}

interface SafeRoute {
  page: string; id: string; run: string; evidence: string; resolution: string; baseline: string;
  selection?: EvidenceSelection;
  knowledge?: KnowledgeRoute;
}

export function safeRoute(hash: string): SafeRoute {
  const fallback = { page: "queue", id: "", run: "", evidence: "", resolution: "", baseline: "" };
  if (/^#\/queue(?:\?[^#]*)?$/.test(hash)) return { ...fallback, page: "queue" };
  const knowledge = parseKnowledgeHash(hash);
  if (knowledge) return { ...fallback, page: "knowledge", knowledge };
  const match = /^#\/(workloads|connections|reports)$/.exec(hash);
  if (match) return { ...fallback, page: match[1] };
  const workload = /^#\/workloads\?workload=([^&#]+)(?:&baseline=([^&#]+))?$/.exec(hash);
  if (workload && uuid.test(workload[1]) && (!workload[2] || uuid.test(workload[2])))
    return { ...fallback, page: "workloads", id: workload[1], baseline: workload[2] || "" };
  const incident = /^#\/incidents\/([^/?#]+)(?:\?([^#]*))?$/.exec(hash);
  const incidentParams = new URLSearchParams(incident?.[2] ?? "");
  const run = incidentParams.get("run") || "";
  const evidence = incidentParams.get("evidence") || "";
  const resolution = incidentParams.get("resolution") || "";
  const selection: EvidenceSelection = {};
  for (const [field, key] of Object.entries(extraParams)) {
    const value = incidentParams.get(key);
    if (value) selection[field as keyof typeof extraParams] = value;
  }
  const hasCompare = Boolean(selection.rightIncident || selection.rightRun || selection.leftIncident || selection.leftRun);
  const validCompare = !hasCompare || Boolean(
    selection.rightIncident && uuid.test(selection.rightIncident) && selection.rightRun && uuid.test(selection.rightRun) &&
    (!selection.leftIncident || uuid.test(selection.leftIncident)) && (!selection.leftRun || uuid.test(selection.leftRun)) &&
    Boolean(selection.leftIncident) === Boolean(selection.leftRun) && run,
  );
  const validSource = !selection.source && !selection.catalog || Boolean(hasCompare && selection.source && selection.catalog && /^[a-zA-Z0-9_.-]{1,64}$/.test(selection.source) && /^[a-zA-Z0-9_.-]{1,32}$/.test(selection.catalog));
  const validLocator = !selection.locatorKind && !selection.locatorKey || Boolean(evidence && selection.locatorKey && /^v1_[a-f0-9]{64}$/.test(selection.locatorKey) && ["field", "event", "log_segment", "series", "sample"].includes(selection.locatorKind || ""));
  const filter = incidentParams.get("compare_filter");
  const validStatus = !selection.differenceStatus || hasCompare && ["added", "removed", "changed", "unchanged", "unavailable", "not_comparable"].includes(selection.differenceStatus);
  const validRecovery = (!selection.plan || uuid.test(selection.plan)) && (!selection.execution || uuid.test(selection.execution));
  if (filter === "changed") selection.changedOnly = true;
  const allowed = ["run", "evidence", "resolution", "return_to", "compare_filter", ...Object.values(extraParams)];
  if (
    incident &&
    uuid.test(incident[1]) &&
    (!run || uuid.test(run)) &&
    (!evidence || (uuid.test(evidence) && Boolean(run))) &&
    (!resolution || uuid.test(resolution)) &&
    validCompare && validSource && validLocator && validStatus && validRecovery && (!filter || hasCompare && filter === "changed") &&
    [...incidentParams.keys()].every((key) => allowed.includes(key) && incidentParams.getAll(key).length <= 1) &&
    incidentParams.getAll("run").length <= 1 &&
    incidentParams.getAll("evidence").length <= 1 &&
    incidentParams.getAll("resolution").length <= 1 &&
    incidentParams.getAll("return_to").length <= 1
  )
    return { ...fallback, page: "incidents", id: incident[1], run, evidence, resolution, ...(Object.keys(selection).length ? { selection } : {}) };
  return fallback;
}

export function readRoute() {
  const route = safeRoute(window.location.hash);
  const canonical =
    route.page === "incidents"
      ? buildIncidentHash(route.id, route.run, incidentReturnHash(window.location.hash), route.resolution, route.evidence, route.selection)
      : route.page === "knowledge"
        ? buildKnowledgeHash(route.knowledge)
      : route.page === "queue"
        ? buildWorkQueueHash(parseWorkQueueHash(window.location.hash))
        : `#/${route.page}${route.page === "workloads" && route.id ? `?workload=${route.id}${route.baseline ? `&baseline=${route.baseline}` : ""}` : ""}`;
  if (window.location.hash !== canonical)
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${canonical}`,
    );
  return route;
}
