import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { useWorkload, useWorkloads } from "@/features/workloads/queries";
import { Failure, Loading } from "@/app/Feedback";
import { buildReportHash, type ReportRoute } from "@/app/routes";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ReportDetails, outcome, time, words } from "./ReportDetails";

export function Reports({ canMutate, route = {} }: { canMutate: boolean; route?: ReportRoute }) {
  const cache = useQueryClient();
  const [workloadOffset, setWorkloadOffset] = useState(0);
  const workloads = useWorkloads(workloadOffset);
  const selectedWorkload = useWorkload(route.workload);
  const hash = buildReportHash(route);
  const current = useRef({ hash, report: route.report, canMutate, revision: 0 });
  current.current = { hash, report: route.report, canMutate, revision: current.current.revision + (current.current.hash === hash ? 0 : 1) };
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const navigate = (next: ReportRoute) => { window.location.hash = buildReportHash(next); };
  const reports = useQuery({
    queryKey: ["recommendation-reports", route.workload, route.cursor],
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/recommendation-reports", {
      signal, params: { query: { workload_id: route.workload, cursor: route.cursor, limit: 25 } },
    })),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const selectedHeading = useRef<HTMLElement | null>(null);
  const historyList = useRef<HTMLUListElement | null>(null);
  const selected = useQuery({
    queryKey: ["recommendation-report", route.report],
    enabled: !!route.report,
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/recommendation-reports/{report_id}", {
      signal, params: { path: { report_id: route.report! } },
    })),
    refetchInterval: query => ["queued", "running"].includes(query.state.data?.job_phase ?? "") ? 2000 : 15000,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    if (route.report && selected.data?.id === route.report) selectedHeading.current?.focus();
  }, [route.report, selected.data?.id]);
  useEffect(() => { if (historyList.current) historyList.current.scrollTop = 0; }, [route.cursor, route.workload]);
  const create = useMutation({
    mutationFn: async (request: { workload: string; hours: number; hash: string; revision: number }) => unwrap(await api.POST("/api/recommendation-reports", {
      body: { workload_id: request.workload, requested_hours: request.hours },
    })),
    onSuccess: (report, request) => {
      void cache.invalidateQueries({ queryKey: ["recommendation-reports"] });
      if (mounted.current && current.current.canMutate && current.current.hash === request.hash && current.current.revision === request.revision && window.location.hash === request.hash) {
        cache.setQueryData(["recommendation-report", report.id], report);
        navigate({ workload: request.workload, hours: request.hours, report: report.id });
      }
    },
  });
  const hours = route.hours ?? 6;
  const selectedIdentity = selected.data?.input_identity ?? selected.data?.result_v2?.identity;
  const workloadOnPage = workloads.data?.items.some(w => w.id === route.workload);
  const activeReport = [selected.data, ...(reports.data?.items ?? [])].some(report => report?.schema_version === 2 && report.workload_id === route.workload && ["queued", "running"].includes(report.job_phase));
  const generationAllowed = canMutate && !!selectedWorkload.data?.active && selectedWorkload.data.source === "live_kubernetes";
  const selectedReportIsCurrent = selected.data?.id === route.report;
  const exportHref = selectedReportIsCurrent && selected.data?.export_ready
    ? `/api/recommendation-reports/${encodeURIComponent(selected.data.id)}/export`
    : undefined;
  return <section className="foundation-stack">
    <h1>Resource recommendations</h1>
    <p>Advisory KRR reports for one saved workload identity. Review each resource and its evidence before making any external change.</p>
    {reports.data?.observation && <section className="foundation-panel" aria-label="Report service observation">
      <p><strong>Report service:</strong> {reports.data.observation.status} · {words(reports.data.observation.reason)}</p>
      <p>Observed {time(reports.data.observation.observed_at)} · latest qualified success {time(reports.data.observation.last_success_at)}</p>
      <p>Saved reports remain readable independently from the current service status.</p>
    </section>}
    <section className="foundation-panel foundation-stack" aria-label="Workload and generation controls">
      <Label htmlFor="report-workload">Workload history</Label>
      <select id="report-workload" value={route.workload ?? ""} onChange={event => {
        create.reset(); navigate({ workload: event.target.value || undefined, hours });
      }}>
        <option value="">All saved workload histories</option>
        {route.workload && !workloadOnPage && <option value={route.workload}>{selectedWorkload.data ? `${selectedWorkload.data.namespace}/${selectedWorkload.data.name} · UID ${selectedWorkload.data.uid}` : selectedIdentity?.workload_id === route.workload ? `${selectedIdentity.namespace}/${selectedIdentity.name} · saved UID ${selectedIdentity.uid}` : `Saved workload ${route.workload}`}</option>}
        {workloads.data?.items.filter(w => w.source === "live_kubernetes").map(w => <option key={w.id} value={w.id}>{w.namespace}/{w.name} · UID {w.uid}</option>)}
      </select>
      {workloads.isPending && <Loading />}
      {workloads.isError && <Failure error={workloads.error} retry={() => void workloads.refetch()} />}
      <div className="foundation-actions">
        <Button variant="outline" disabled={workloadOffset === 0} onClick={() => setWorkloadOffset(Math.max(0, workloadOffset - 25))}>Previous workloads</Button>
        <Button variant="outline" disabled={workloads.data?.next_offset === null || !workloads.data} onClick={() => setWorkloadOffset(workloads.data!.next_offset!)}>More workloads</Button>
      </div>
      {canMutate ? <>
        <Label htmlFor="report-hours">Requested history window</Label>
        <select id="report-hours" value={hours} onChange={event => { create.reset(); navigate({ ...route, hours: Number(event.target.value) }); }}>
          {Array.from({ length: 19 }, (_, index) => index + 6).map(value => <option key={value} value={value}>{value} hours</option>)}
        </select>
        <p id="report-generation-help">Generation analyzes existing history; it does not wait six new hours. Numeric advice requires qualified six-hour input. Other allowed durations return insufficient data. Select an active live workload to generate a report.</p>
        {activeReport && <p role="status">A report is already queued or running for this workload. Inspect it before requesting another.</p>}
        <Button disabled={!generationAllowed || activeReport || create.isPending} aria-describedby="report-generation-help" onClick={() => create.mutate({ workload: route.workload!, hours, hash: buildReportHash(route), revision: current.current.revision })}>
          {create.isPending ? "Requesting report…" : "Generate resource report"}
        </Button>
        {selectedWorkload.isError && <Failure error={selectedWorkload.error} retry={() => void selectedWorkload.refetch()} />}
        {selectedWorkload.data && !selectedWorkload.data.active && <p>This saved identity is inactive. Existing reports are still available.</p>}
      </> : <p>Viewer access: inspect and export saved reports. An Operator starts generation.</p>}
      {create.isError && <Failure error={create.error} />}
    </section>
    <section className="foundation-panel foundation-stack" aria-label="Saved report history">
      <h2>Saved report history</h2>
      {reports.isPending && <Loading />}
      {reports.isError && <Failure error={reports.error} retry={() => void reports.refetch()} />}
      {reports.isError && !reports.data && route.cursor && <Button variant="outline" onClick={() => navigate({ ...route, cursor: undefined })}>First report page</Button>}
      {reports.data && <>
        {!reports.data.items.length && <p>{route.workload ? "No reports match this workload history." : "No saved reports yet. An Operator can generate the first report."}</p>}
        <ul ref={historyList} className="foundation-stack report-history-list" aria-label="Reports on this history page">
          {reports.data.items.map(report => <li key={report.id}>
            <a href={buildReportHash({ ...route, report: report.id })} aria-label={`Open report ${report.id}: ${time(report.created_at)}`} aria-current={route.report === report.id ? "true" : undefined}>
              {time(report.created_at)} · {report.input_identity ? `${report.input_identity.namespace}/${report.input_identity.name} · UID ${report.input_identity.uid}` : `workload ${report.workload_id}`} · {words(report.job_phase)} / {outcome(report)} · report {report.id.slice(0, 8)}
            </a>
          </li>)}
        </ul>
        <div className="foundation-actions">
          <Button variant="outline" disabled={!route.cursor} onClick={() => navigate({ ...route, cursor: undefined })}>First report page</Button>
          <Button variant="outline" disabled={!reports.data.next_cursor} onClick={() => navigate({ ...route, cursor: reports.data!.next_cursor! })}>Next report page</Button>
        </div>
      </>}
    </section>
    {!route.report ? <p>Select a saved report to inspect its exact result and export.</p> : <section className="foundation-panel foundation-stack" aria-label="Selected resource report" tabIndex={-1} ref={selectedHeading}>
      {selected.isPending && <Loading />}
      {selected.isError && <>
        <p>The selected report could not be read. Its selection has been retained.</p>
        <Failure error={selected.error} retry={() => void selected.refetch()} />
      </>}
      {selected.data && <>
        {route.workload && selectedIdentity && selectedIdentity.workload_id !== route.workload && <p>This report belongs to a different saved workload identity. <a href={buildReportHash({ ...route, workload: selectedIdentity.workload_id, cursor: undefined })}>Filter to this report’s workload</a></p>}
        <ReportDetails report={selected.data} historyHref={buildReportHash({ ...route, workload: selected.data.workload_id, cursor: undefined })} />
        {exportHref ? <Button asChild variant="outline" aria-describedby="report-export-help">
          <a href={exportHref} download={`resource-report-${selected.data.id}.json`} aria-label={`Export selected report ${selected.data.id.slice(0, 8)}`}>Export selected report</a>
        </Button> : <Button variant="outline" disabled aria-describedby="report-export-help" aria-label={`Export selected report ${selected.data.id.slice(0, 8)}`}>Export selected report</Button>}
        <p id="report-export-help">{exportHref ? "Downloads the exact stored JSON for this report. Check the browser Downloads list for completion." : "The export becomes available when this report finishes. Queued and running reports cannot be exported."}</p>
      </>}
    </section>}
  </section>;
}
