const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function safeRoute(hash: string) {
  const fallback = { page: "queue", id: "", run: "" };
  const match = /^#\/(queue|workloads|connections|reports)$/.exec(hash);
  if (match) return { ...fallback, page: match[1] };
  const incident = /^#\/incidents\/([^/?#]+)(?:\?run=([^&#]+))?$/.exec(hash);
  if (
    incident &&
    uuid.test(incident[1]) &&
    (!incident[2] || uuid.test(incident[2]))
  )
    return { page: "incidents", id: incident[1], run: incident[2] || "" };
  return fallback;
}

export function readRoute() {
  const route = safeRoute(window.location.hash);
  const canonical =
    route.page === "incidents"
      ? `#/incidents/${route.id}${route.run ? `?run=${route.run}` : ""}`
      : `#/${route.page}`;
  if (window.location.hash !== canonical)
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${canonical}`,
    );
  return route;
}
