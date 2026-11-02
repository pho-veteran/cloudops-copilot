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
}

const ORIGINS = ["operator", "alertmanager"] as const;
const STATES = ["open", "acknowledged", "resolved"] as const;
const ALERT_SIGNALS = ["none", "firing", "resolved"] as const;
const SORT_FIELDS = ["created_at", "first_seen_at", "last_seen_at", "severity"] as const;
const SORT_ORDERS = ["asc", "desc"] as const;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : undefined;
}

export function parseWorkQueueHash(hash: string): WorkQueueParams {
  const defaults: WorkQueueParams = { sort_by: "created_at", sort_order: "desc", offset: 0 };
  const match = /^#\/queue(?:\?([^#]*))?$/.exec(hash);
  if (!match) return defaults;
  const params = new URLSearchParams(match[1] ?? "");
  const parsed: WorkQueueParams = { ...defaults };

  const search = params.get("search");
  if (search) parsed.search = search;
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
  if (Number.isSafeInteger(offset) && offset >= 0) parsed.offset = offset;
  const selected = params.get("selected");
  if (selected && uuid.test(selected)) parsed.selected = selected;

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
  if (params.sort_by && params.sort_by !== "created_at") query.set("sort_by", params.sort_by);
  if (params.sort_order && params.sort_order !== "desc") query.set("sort_order", params.sort_order);
  if (params.offset) query.set("offset", params.offset.toString());
  if (params.selected) query.set("selected", params.selected);

  const search = query.toString();
  return `#/queue${search ? `?${search}` : ""}`;
}

export function safeRoute(hash: string) {
  const fallback = { page: "queue", id: "", run: "", baseline: "" };
  if (/^#\/queue(?:\?[^#]*)?$/.test(hash)) return { ...fallback, page: "queue" };
  const match = /^#\/(workloads|connections|reports)$/.exec(hash);
  if (match) return { ...fallback, page: match[1] };
  const workload = /^#\/workloads\?workload=([^&#]+)(?:&baseline=([^&#]+))?$/.exec(hash);
  if (workload && uuid.test(workload[1]) && (!workload[2] || uuid.test(workload[2])))
    return { ...fallback, page: "workloads", id: workload[1], baseline: workload[2] || "" };
  const incident = /^#\/incidents\/([^/?#]+)(?:\?run=([^&#]+))?$/.exec(hash);
  if (
    incident &&
    uuid.test(incident[1]) &&
    (!incident[2] || uuid.test(incident[2]))
  )
    return { ...fallback, page: "incidents", id: incident[1], run: incident[2] || "" };
  return fallback;
}

export function readRoute() {
  const route = safeRoute(window.location.hash);
  const canonical =
    route.page === "incidents"
      ? `#/incidents/${route.id}${route.run ? `?run=${route.run}` : ""}`
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
