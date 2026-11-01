const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function safeRoute(hash: string) {
  const fallback = { page: "queue", id: "", run: "", baseline: "" };
  const match = /^#\/(queue|workloads|connections|reports)$/.exec(hash);
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
      : `#/${route.page}${route.page === "workloads" && route.id ? `?workload=${route.id}${route.baseline ? `&baseline=${route.baseline}` : ""}` : ""}`;
  if (window.location.hash !== canonical)
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${canonical}`,
    );
  return route;
}
