import { useState, type Dispatch } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  Check,
  Copy,
  Download,
  FileText,
  Play,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { StatusBadge, downloadText } from "@/components/domain";
import {
  handoffMarkdown,
  type DemoState,
  type DemoAction,
  type Incident,
  type Evidence,
  type InvestigationRun,
} from "@/demo/model";

type Props = {
  state: DemoState;
  incident: Incident;
  dispatch: Dispatch<DemoAction>;
  tab: string;
  runId?: string;
  onTab: (tab: string, run?: string) => void;
  onBack: () => void;
  onOpenWorkload: () => void;
  onOpenIncident: (id: string) => void;
};
type Decision =
  | "approve"
  | "reject"
  | "resolve"
  | "reopen"
  | "reinvestigate"
  | "request_rollback"
  | "manual_handoff"
  | "verify_manually";
const labels: Record<Decision, string> = {
  approve: "Duyệt thay đổi",
  reject: "Từ chối đề xuất",
  resolve: "Đóng incident",
  reopen: "Mở lại incident",
  reinvestigate: "Điều tra lại",
  request_rollback: "Xác nhận rollback",
  manual_handoff: "Bàn giao xử lý thủ công",
  verify_manually: "Xác minh thủ công để mở khóa",
};

export function IncidentWorkspace({
  state,
  incident: i,
  dispatch,
  tab,
  runId,
  onTab,
  onBack,
  onOpenWorkload,
  onOpenIncident,
}: Props) {
  const [evidence, setEvidence] = useState<Evidence | null>(null),
    [decision, setDecision] = useState<Decision | null>(null),
    [reason, setReason] = useState(""),
    [manualEvidence, setManualEvidence] = useState(""),
    [stopped, setStopped] = useState(false),
    [outcome, setOutcome] = useState<"applied" | "not_applied">("applied"),
    [note, setNote] = useState(""),
    [copied, setCopied] = useState(false),
    [runbook, setRunbook] = useState(false),
    [resolutionId, setResolutionId] = useState(""),
    [compareId, setCompareId] = useState("");
  const readonly = state.role === "viewer",
    active = ["queued", "executing", "verifying", "rolling_back"].includes(
      i.execution.status,
    ),
    working = ["queued", "collecting", "analyzing"].includes(i.run.status);
  const historicalDefault = !["completed", "insufficient_evidence"].includes(
    i.run.status,
  )
    ? [...i.runs].reverse().find((r) => r.status === "completed")
    : undefined;
  const run = i.runs.find((r) => r.id === runId) || historicalDefault || i.run,
    old = run.id !== i.run.id;
  const other =
    i.runs.find((r) => r.id === compareId) ||
    i.runs.find((r) => r.id !== run.id) ||
    run;
  const plan = i.plan,
    locked = state.targetLocks[i.workloadId],
    md = handoffMarkdown(i, resolutionId || undefined);
  const executionPlan = i.plans.find(
    (p) => `${p.id}-execution` === i.execution.id,
  );
  const decisionPlan = decision === "request_rollback" ? executionPlan : plan;
  const act = (action: DemoAction) =>
    dispatch({
      ...action,
      ...("workloadId" in action ? {} : { incidentId: i.id }),
    } as DemoAction);
  const open = (d: Decision) => {
    setReason("");
    setManualEvidence("");
    setStopped(false);
    setDecision(d);
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  const submit = () => {
    if (!decision) return;
    if (decision === "verify_manually")
      act({
        type: decision,
        reason,
        evidence: manualEvidence,
        oldRequestStopped: stopped,
        outcome,
      });
    else act({ type: decision, reason });
    setDecision(null);
  };
  const canAdvance = working || active;
  return (
    <>
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft />
        Quay lại
      </Button>
      <div className="incident-heading">
        <div>
          <div className="flex gap-2 items-center flex-wrap">
            <span className="mono muted text-xs">{i.id}</span>
            <Badge variant="outline">
              {i.runs[0]?.reason.startsWith("Manual investigation")
                ? "Điều tra chủ động"
                : i.severity}
            </Badge>
            <StatusBadge status={i.status} />
          </div>
          <h1>{i.title}</h1>
          <button className="workload-link" onClick={onOpenWorkload}>
            {i.namespace} / {i.workload}
            <ArrowUpRight size={14} />
          </button>
          <p className="muted mt-2">{i.impact}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={readonly || i.status !== "open"}
            onClick={() => act({ type: "acknowledge" })}
          >
            {i.acknowledgedBy ? "Đã tiếp nhận" : "Tiếp nhận"}
          </Button>
          <Button
            variant="outline"
            disabled={
              readonly ||
              active ||
              i.execution.status === "uncertain" ||
              working ||
              i.run.status === "waiting_for_quota" ||
              i.status === "resolved"
            }
            onClick={() => open("reinvestigate")}
          >
            <RotateCcw />
            Điều tra lại
          </Button>
          <Button
            disabled={
              readonly ||
              active ||
              i.execution.status === "uncertain" ||
              ["collecting", "analyzing"].includes(i.run.status)
            }
            onClick={() => open(i.status === "resolved" ? "reopen" : "resolve")}
          >
            {i.status === "resolved" ? "Mở lại" : "Đóng incident"}
          </Button>
        </div>
      </div>
      <div className="run-meta">
        <span>Evidence: {i.freshness}</span>
        <span className="mono">{i.run.configurationVersion}</span>
        <span>Retrieval: full text</span>
      </div>
      {(canAdvance ||
        i.run.status === "waiting_for_quota" ||
        i.run.status === "failed" ||
        i.run.status === "insufficient_evidence") && (
        <Alert className="my-4">
          <Play />
          <AlertTitle>
            {active
              ? "Đang thực hiện quyết định"
              : "Tiến trình điều tra mới nhất"}{" "}
            ·{" "}
            <StatusBadge status={active ? i.execution.status : i.run.status} />
          </AlertTitle>
          <AlertDescription>
            <p>{active ? i.execution.summary : i.run.summary}</p>
            <div className="flex gap-2 flex-wrap mt-3">
              {canAdvance && (
                <Button
                  size="sm"
                  disabled={readonly}
                  onClick={() => act({ type: "advance" })}
                >
                  Tiếp tục mô phỏng
                </Button>
              )}
              {[
                "waiting_for_quota",
                "failed",
                "insufficient_evidence",
              ].includes(i.run.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={readonly}
                  onClick={() => act({ type: "retry" })}
                >
                  Thử điều tra lại
                </Button>
              )}
              {["queued", "waiting_for_quota"].includes(i.run.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={readonly}
                  onClick={() => act({ type: "cancel_run" })}
                >
                  Hủy lượt đang chờ
                </Button>
              )}
            </div>
          </AlertDescription>
        </Alert>
      )}
      {locked && (
        <Alert className="my-4">
          <ShieldCheck />
          <AlertTitle>Target đang khóa · {locked.incidentId}</AlertTitle>
          <AlertDescription>
            {locked.reason} Không gửi lại thao tác ghi khi chưa rõ kết quả.
            {locked.incidentId !== i.id && (
              <Button
                variant="link"
                onClick={() => onOpenIncident(locked.incidentId)}
              >
                Mở incident giữ khóa
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}
      <div className="incident-layout">
        <section className="investigation-main">
          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <Label htmlFor="run-select">Snapshot điều tra</Label>
            <select
              id="run-select"
              className="native-select max-w-80"
              value={run.id}
              onChange={(e) => onTab(tab, e.target.value)}
            >
              {i.runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.status}
                </option>
              ))}
            </select>
          </div>
          {old && (
            <Alert className="mb-4">
              <AlertTitle>Đang xem kết quả cũ · {run.id}</AlertTitle>
              <AlertDescription>
                Lượt mới nhất {i.run.id}: {i.run.status}. Kết luận này chưa phản
                ánh lượt mới.{" "}
                <Button variant="link" onClick={() => onTab(tab, i.run.id)}>
                  Xem lượt mới nhất
                </Button>
              </AlertDescription>
            </Alert>
          )}
          <Tabs value={tab} onValueChange={(t) => onTab(t, run.id)}>
            <div className="tabs-scroll">
              <TabsList>
                <TabsTrigger value="findings">Findings</TabsTrigger>
                <TabsTrigger value="evidence">
                  Evidence ({run.evidence.length})
                </TabsTrigger>
                <TabsTrigger value="activity">Activity</TabsTrigger>
                <TabsTrigger value="compare">So sánh runs</TabsTrigger>
                <TabsTrigger value="handoff">Handoff</TabsTrigger>
              </TabsList>
            </div>
            <TabsContent value="findings">
              <div className="content-panel">
                <div className="panel-heading">
                  <h2>Kết luận điều tra</h2>
                  <StatusBadge status={run.status} />
                </div>
                <p className="prose-block">{run.summary}</p>
                <div className="flex flex-wrap gap-2 my-4">
                  {run.supportingEvidenceIds.map((id) => (
                    <Button
                      key={id}
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setEvidence(
                          run.evidence.find((e) => e.id === id) || null,
                        )
                      }
                    >
                      <FileText size={13} />
                      {id}
                    </Button>
                  ))}
                </div>
                <MetricOverview run={run} pattern={i.pattern} />
                <div className="two-cols mt-6">
                  <div>
                    <h3>Đã loại trừ</h3>
                    <BulletList
                      items={
                        run.ruledOut.length
                          ? run.ruledOut
                          : ["Chưa đủ bằng chứng để loại trừ thêm nguyên nhân."]
                      }
                    />
                  </div>
                  <div>
                    <h3>Chưa kiểm chứng</h3>
                    <BulletList items={run.unverified} />
                  </div>
                </div>
                {run.missingEvidence.length > 0 && (
                  <Alert className="mt-5">
                    <AlertTitle>Evidence còn thiếu</AlertTitle>
                    <AlertDescription>
                      <BulletList items={run.missingEvidence} />
                    </AlertDescription>
                  </Alert>
                )}
                <Button
                  variant="outline"
                  className="mt-5"
                  onClick={() => setRunbook(true)}
                >
                  <BookOpen />
                  Runbook đã dùng · {i.pattern} v3
                </Button>
              </div>
            </TabsContent>
            <TabsContent value="evidence">
              <div className="content-panel">
                <h2>Evidence của {run.id}</h2>
                <p className="muted mb-5">
                  Nguồn, thời điểm và dữ liệu gốc gắn với từng lượt điều tra.
                </p>
                <div className="evidence-grid">
                  {run.evidence.map((e) => (
                    <button
                      key={e.id}
                      className="evidence-card"
                      onClick={() => setEvidence(e)}
                    >
                      <div className="flex justify-between gap-2">
                        <FileText size={18} />
                        <StatusBadge status={e.status} />
                      </div>
                      <h3>{e.title}</h3>
                      <p className="mono text-xs">{e.id}</p>
                      <p className="muted text-xs mt-2">{e.source}</p>
                      <p className="muted text-xs">{e.observedAt}</p>
                    </button>
                  ))}
                </div>
                {!run.evidence.length && (
                  <p className="empty-state">
                    Chưa có snapshot cho lượt này. Tiếp tục thu thập evidence.
                  </p>
                )}
              </div>
            </TabsContent>
            <TabsContent value="activity">
              <div className="content-panel">
                <h2>Activity & ghi chú</h2>
                <form
                  className="note-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    act({ type: "add_note", reason: note });
                    setNote("");
                  }}
                >
                  <Textarea
                    aria-label="Ghi chú incident"
                    placeholder="Ghi lại quan sát hoặc bước xử lý ngoài ứng dụng…"
                    required
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    disabled={readonly}
                  />
                  <Button type="submit" disabled={readonly || !note.trim()}>
                    Thêm ghi chú
                  </Button>
                </form>
                <div className="timeline">
                  {[...i.activity].reverse().map((a) => (
                    <div className="timeline-item" key={a.id}>
                      <span className="text-xs muted">
                        {a.at} · {a.actor}
                      </span>
                      <h3>{a.title}</h3>
                      <p>{a.detail}</p>
                    </div>
                  ))}
                </div>
                <h3 className="mt-5">Ghi chú đã lưu</h3>
                {i.notes.map((n) => (
                  <div key={n.id} className="prose-block">
                    <p>{n.text}</p>
                    <small className="muted">
                      {n.actor} · {n.at}
                    </small>
                  </div>
                ))}
              </div>
            </TabsContent>
            <TabsContent value="compare">
              <div className="content-panel">
                <h2>So sánh hai lượt điều tra</h2>
                <Label htmlFor="compare-run">So sánh với</Label>
                <select
                  id="compare-run"
                  className="native-select my-3"
                  value={other.id}
                  onChange={(e) => setCompareId(e.target.value)}
                >
                  {i.runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.id} · {r.status}
                    </option>
                  ))}
                </select>
                {i.runs.length < 2 && (
                  <p className="muted mb-4">
                    Điều tra lại để có snapshot thứ hai. Lượt cũ vẫn được giữ
                    nguyên.
                  </p>
                )}
                <div className="compare-grid">
                  {[run, other].map((r, index) => (
                    <div key={index}>
                      <div className="panel-heading">
                        <h3>{r.id}</h3>
                        <StatusBadge status={r.status} />
                      </div>
                      <p>{r.summary}</p>
                      <p className="muted my-3">
                        {r.reason} · {r.createdAt}
                      </p>
                      {r.evidence.map((e) => (
                        <Button
                          className="mb-2 mr-2"
                          key={e.id}
                          variant="outline"
                          size="sm"
                          onClick={() => setEvidence(e)}
                        >
                          {e.id}
                        </Button>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </TabsContent>
            <TabsContent value="handoff">
              <div className="content-panel">
                <div className="panel-heading">
                  <h2>Bàn giao & lịch sử đóng</h2>
                  <Badge variant="outline">
                    {i.resolutions.length} snapshots
                  </Badge>
                </div>
                <Label htmlFor="resolution">Bản báo cáo</Label>
                <select
                  id="resolution"
                  className="native-select my-3"
                  value={resolutionId}
                  onChange={(e) => {
                    setResolutionId(e.target.value);
                    setCopied(false);
                  }}
                >
                  <option value="">Trạng thái hiện tại</option>
                  {i.resolutions.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.id} · {r.at}
                    </option>
                  ))}
                </select>
                {resolutionId && (
                  <p className="muted mb-3">
                    Snapshot đã lưu khi đóng, không thay đổi sau khi mở lại hoặc
                    xác minh thêm.
                  </p>
                )}
                <div className="flex gap-2 mb-4">
                  <Button variant="outline" onClick={() => copy(md)}>
                    {copied ? <Check /> : <Copy />}
                    {copied ? "Đã sao chép" : "Sao chép"}
                  </Button>
                  <Button
                    onClick={() =>
                      downloadText(
                        `${i.id}-${resolutionId || "handoff"}.md`,
                        md,
                      )
                    }
                  >
                    <Download />
                    Tải Markdown
                  </Button>
                </div>
                <pre className="handoff-preview">{md}</pre>
              </div>
            </TabsContent>
          </Tabs>
        </section>
        <aside className="decision-rail">
          <div className="content-panel">
            <div className="eyebrow">NEXT STEP</div>
            <h2>
              {i.status === "resolved"
                ? "Incident đã đóng"
                : plan
                  ? "Quyết định thay đổi"
                  : "Hướng xử lý thủ công"}
            </h2>
            {plan ? (
              <>
                <div className="flex flex-wrap gap-2 my-3">
                  <StatusBadge status={plan.status} />
                  <Badge variant="secondary">{plan.template}</Badge>
                </div>
                <p className="mono text-xs break-all muted">{plan.hash}</p>
                <Diff rows={plan.diff} />
                <details className="my-4">
                  <summary>Điều kiện, rủi ro & xác minh</summary>
                  <h3 className="mt-3">Điều kiện</h3>
                  <BulletList items={plan.prerequisites} />
                  <h3 className="mt-3">Rủi ro</h3>
                  <BulletList items={plan.risks} />
                  <h3 className="mt-3">Kiểm chứng</h3>
                  <BulletList items={plan.verification} />
                  <p className="text-xs muted mt-3">
                    UID: {plan.targetUid}
                    <br />
                    Hết hạn: {plan.expiresAt}
                    <br />
                    Run: {plan.runId}
                  </p>
                </details>
                {plan.status === "awaiting_approval" &&
                i.status !== "resolved" ? (
                  <div className="flex gap-2">
                    <Button
                      className="flex-1"
                      disabled={readonly || Boolean(locked)}
                      onClick={() => open("approve")}
                    >
                      Review & duyệt
                    </Button>
                    <Button
                      variant="outline"
                      disabled={readonly}
                      onClick={() => open("reject")}
                    >
                      Từ chối
                    </Button>
                  </div>
                ) : (
                  <p className="muted text-sm">
                    {i.status === "resolved"
                      ? "Mở lại incident nếu cần một quyết định mới."
                      : [
                            "stale",
                            "expired",
                            "rejected",
                            "invalidated",
                          ].includes(plan.status)
                        ? "Đề xuất không còn dùng được. Điều tra lại để tạo một kế hoạch mới."
                        : "Quyết định và kết quả được ghi trong Activity."}
                  </p>
                )}
              </>
            ) : (
              <>
                <p className="prose-block">
                  {i.manualReason ||
                    "Đợi kết quả điều tra để xác định bước tiếp theo."}
                </p>
                <Button variant="outline" onClick={() => setRunbook(true)}>
                  <BookOpen />
                  Đọc runbook
                </Button>
              </>
            )}
          </div>
          <div className="content-panel">
            <h2>Kết quả thực thi</h2>
            <StatusBadge status={i.execution.status} />
            <p className="prose-block">{i.execution.summary}</p>
            {i.execution.id && (
              <p className="mono text-xs muted break-all my-3">
                Execution: {i.execution.id}
              </p>
            )}
            <BulletList items={i.execution.verification} />
            {i.execution.status === "none" && (
              <p className="muted text-sm">
                Duyệt một đề xuất để bắt đầu. Mỗi bước mô phỏng cần bạn bấm tiếp
                tục.
              </p>
            )}
            {i.status !== "resolved" &&
              ["failed", "succeeded"].includes(i.execution.status) &&
              i.execution.rollbackEligible && (
                <Button
                  variant="outline"
                  disabled={readonly}
                  onClick={() => open("request_rollback")}
                >
                  <RotateCcw />
                  Review rollback
                </Button>
              )}
            {i.execution.outcomeUnknown && (
              <>
                <p className="text-sm mt-3">
                  Reconciliation: {i.execution.reconcileAttempts}/2. Không suy
                  luận thành công từ trạng thái Ready.
                </p>
                <Button
                  className="mt-3"
                  variant="outline"
                  disabled={readonly || i.execution.reconcileAttempts >= 2}
                  onClick={() => act({ type: "reconcile" })}
                >
                  Đối soát kết quả
                </Button>
                <Button
                  className="mt-3 w-full"
                  variant="outline"
                  disabled={readonly || i.execution.reconcileAttempts < 2}
                  onClick={() => open("verify_manually")}
                >
                  Xác minh thủ công để mở khóa
                </Button>
              </>
            )}
            <p className="muted text-xs mt-4">{i.expectedOutcome}</p>
          </div>
          <Button
            variant="outline"
            disabled={readonly || active || i.execution.status === "uncertain"}
            onClick={() => open("manual_handoff")}
          >
            Bàn giao thủ công
          </Button>
          <Button variant="ghost" onClick={() => onTab("handoff", run.id)}>
            <FileText />
            Xem báo cáo bàn giao
          </Button>
        </aside>
      </div>
      <Sheet
        open={!!evidence}
        onOpenChange={(v) => {
          if (!v) setEvidence(null);
        }}
      >
        <SheetContent className="evidence-sheet">
          <SheetHeader>
            <SheetTitle>{evidence?.title}</SheetTitle>
            <SheetDescription>
              {evidence?.id} · snapshot đã lưu
            </SheetDescription>
          </SheetHeader>
          {evidence && (
            <div className="p-4 space-y-5">
              <StatusBadge status={evidence.status} />
              <dl>
                <dt className="muted">Nguồn</dt>
                <dd>{evidence.source}</dd>
                <dt className="muted mt-3">Quan sát lúc</dt>
                <dd>{evidence.observedAt}</dd>
              </dl>
              <pre className="handoff-preview">{evidence.excerpt}</pre>
              <Button variant="outline" onClick={() => copy(evidence.excerpt)}>
                <Copy />
                {copied ? "Đã sao chép" : "Sao chép evidence"}
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>
      <Dialog open={runbook} onOpenChange={setRunbook}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{i.pattern} · runbook v3</DialogTitle>
            <DialogDescription>
              Phiên bản tham chiếu bởi {run.id} · full-text retrieval
            </DialogDescription>
          </DialogHeader>
          <ol className="check-list">
            <li>Kiểm tra UID, owner và cửa sổ quan sát của workload.</li>
            <li>
              Đối chiếu event, previous logs và telemetry; ghi rõ phần thiếu.
            </li>
            <li>
              {i.pattern === "OOM"
                ? "Đối chiếu OOMKilled với memory limit; không kết luận memory leak chỉ từ một lần restart."
                : i.pattern === "Load"
                  ? "Kiểm tra RPS, latency, throttling, headroom và HPA trước khi scale."
                  : i.pattern === "CrashLoop"
                    ? "Chỉ restore template có baseline healthy đã xác nhận; không khôi phục nội dung ConfigMap/Secret."
                    : "Chuyển evidence cho resource owner; không đề xuất thay đổi tự động cho pattern này."}
            </li>
            <li>
              Xác minh bằng dữ liệu mới và coverage đầy đủ; bàn giao nếu kết quả
              chưa rõ.
            </li>
          </ol>
          <p className="muted text-xs">
            Nội dung mẫu minh họa cho review UX. Knowledge lưu các tài liệu hiện
            hành.
          </p>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!decision}
        onOpenChange={(v) => {
          if (!v) setDecision(null);
        }}
      >
        <DialogContent className="decision-dialog">
          <DialogHeader>
            <DialogTitle>{decision && labels[decision]}</DialogTitle>
            <DialogDescription>
              {decision === "resolve"
                ? "Lưu snapshot bằng chứng, quyết định và kết quả hiện tại. Đóng incident không chứng minh thay đổi thành công."
                : decision === "approve"
                  ? "Bạn đang duyệt đúng diff và target của kế hoạch này. Mọi thao tác ở đây chỉ là mô phỏng."
                  : decision === "request_rollback"
                    ? "Rollback là một quyết định riêng; các điều kiện và tính an toàn sẽ được kiểm tra lại."
                    : "Lý do được lưu vào lịch sử của incident."}
            </DialogDescription>
          </DialogHeader>
          <form
            className="form-stack"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            {(decision === "approve" || decision === "request_rollback") &&
              decisionPlan && (
                <>
                  <Diff
                    rows={decisionPlan.diff}
                    inverse={decision === "request_rollback"}
                  />
                  <p className="text-xs mono break-all">
                    {decisionPlan.hash} · {decisionPlan.targetUid}
                  </p>
                  {decision === "request_rollback" && (
                    <p className="muted">
                      Không rollback khi hết hạn, target đã đổi hoặc cấu hình cũ
                      không an toàn.
                    </p>
                  )}
                  {decision === "approve" && (
                    <details>
                      <summary>Thử xung đột trước khi gửi duyệt</summary>
                      <div className="flex gap-2 mt-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => act({ type: "stale_plan" })}
                        >
                          Mô phỏng target đổi
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => act({ type: "expire_plan" })}
                        >
                          Mô phỏng hết hạn
                        </Button>
                      </div>
                      <StatusBadge status={decisionPlan.status} />
                    </details>
                  )}
                </>
              )}
            {decision === "resolve" && (
              <div className="content-panel">
                <p>{i.title}</p>
                <p>
                  Run: {i.run.id} · {i.run.status}
                </p>
                <p>
                  Kết quả: <StatusBadge status={i.execution.status} />
                </p>
                <p>
                  {i.execution.outcomeUnknown
                    ? "Chưa rõ kết quả ghi. Target tiếp tục khóa sau khi đóng."
                    : i.execution.summary}
                </p>
                <p>
                  {i.notes.length} ghi chú · {i.run.evidence.length} evidence
                </p>
              </div>
            )}
            {decision === "verify_manually" && (
              <>
                <Label htmlFor="manual-evidence">
                  Bằng chứng kiểm chứng trực tiếp
                </Label>
                <Textarea
                  id="manual-evidence"
                  required
                  value={manualEvidence}
                  onChange={(e) => setManualEvidence(e.target.value)}
                  placeholder="UID/resourceVersion, giá trị quan sát, nguồn và thời điểm…"
                />
                <Label htmlFor="manual-outcome">Kết quả quan sát</Label>
                <select
                  id="manual-outcome"
                  className="native-select"
                  value={outcome}
                  onChange={(e) => setOutcome(e.target.value as typeof outcome)}
                >
                  <option value="applied">Thay đổi đã hiện diện</option>
                  <option value="not_applied">
                    Thay đổi không được áp dụng
                  </option>
                </select>
                <label className="flex gap-3 items-start">
                  <Checkbox
                    checked={stopped}
                    onCheckedChange={(v) => setStopped(v === true)}
                  />
                  Tôi đã xác minh yêu cầu ghi cũ đã dừng và không thể ghi muộn.
                </label>
              </>
            )}
            <Label htmlFor="decision-reason">
              Lý do {decision === "approve" ? "(tùy chọn)" : "(bắt buộc)"}
            </Label>
            <Textarea
              id="decision-reason"
              value={reason}
              required={decision !== "approve"}
              minLength={decision === "approve" ? undefined : 3}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Ghi rõ quyết định và bước xử lý tiếp theo…"
            />
            <div className="dialog-actions">
              <Button
                type="button"
                variant="outline"
                onClick={() => setDecision(null)}
              >
                Hủy
              </Button>
              <Button
                type="submit"
                disabled={
                  readonly || (decision === "verify_manually" && !stopped)
                }
              >
                {decision && labels[decision]}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="check-list">
      {items.map((s, index) => (
        <li key={index}>{s}</li>
      ))}
    </ul>
  );
}
function Diff({
  rows,
  inverse = false,
}: {
  rows: { field: string; before: string; after: string }[];
  inverse?: boolean;
}) {
  return (
    <div className="diff-table">
      <table>
        <thead>
          <tr>
            <th>Trường</th>
            <th>Hiện tại</th>
            <th>{inverse ? "Khôi phục" : "Đề xuất"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.field}>
              <td>{d.field}</td>
              <td className="muted">{inverse ? d.after : d.before}</td>
              <td>{inverse ? d.before : d.after}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function MetricOverview({
  run,
  pattern,
}: {
  run: InvestigationRun;
  pattern: string;
}) {
  const metric = run.evidence.find((e) => e.id.endsWith("E3"));
  if (!metric || metric.status !== "available")
    return (
      <div className="metric-card">
        <h3>Telemetry chưa đủ</h3>
        <p className="muted">
          Không hiển thị đường bằng 0 khi thiếu dữ liệu. Xem Evidence để biết
          phần chưa thu thập được.
        </p>
      </div>
    );
  const series =
    pattern === "OOM"
      ? [
          {
            name: "Memory",
            unit: "MiB",
            values: [170, 215, 255, 180],
            max: 300,
          },
          {
            name: "Container restarts",
            unit: "count",
            values: [0, 0, 1, 2],
            max: 3,
          },
        ]
      : pattern === "Load"
        ? [
            {
              name: "Traffic",
              unit: "req/s",
              values: [40, 90, 160, 180],
              max: 200,
            },
            {
              name: "p95 latency",
              unit: "ms",
              values: [110, 180, 560, 820],
              max: 1000,
            },
          ]
        : [
            {
              name: "Container restarts",
              unit: "count",
              values: [0, 2, 4, 6],
              max: 8,
            },
            {
              name: "Ready replicas",
              unit: "count",
              values: [1, 1, 0, 0],
              max: 2,
            },
          ];
  return (
    <>
      <div className="metric-grid">
        {series.map((s) => (
          <div key={s.name} className="metric-card">
            <div className="panel-heading">
              <h3>{s.name}</h3>
              <span className="muted text-xs">{s.unit}</span>
            </div>
            <div className="text-2xl font-medium">
              {s.values.at(-1)}{" "}
              <small className="text-xs muted">{s.unit}</small>
            </div>
            <svg
              className="metric-chart"
              viewBox="0 0 280 90"
              role="img"
              aria-label={`${s.name}: ${s.values.join(", ")} ${s.unit}, 10:30 đến 10:45 UTC+7`}
            >
              <path
                d="M0 75H280 M0 40H280 M0 5H280"
                stroke="currentColor"
                opacity=".12"
              />
              <polyline
                points={s.values
                  .map((v, n) => `${n * 90 + 5},${75 - (v / s.max) * 65}`)
                  .join(" ")}
                fill="none"
                stroke="var(--chart-2)"
                strokeWidth="2.5"
              />
              {s.values.map((v, n) => (
                <circle
                  key={n}
                  cx={n * 90 + 5}
                  cy={75 - (v / s.max) * 65}
                  r="3"
                  fill="var(--chart-2)"
                />
              ))}
            </svg>
            <div className="flex justify-between muted text-xs">
              <span>10:30</span>
              <span>10:45 UTC+7</span>
            </div>
          </div>
        ))}
      </div>
      <p className="text-xs muted mt-2">
        Dữ liệu fixture minh họa · {metric.id}. Cửa sổ snapshot: {run.window}.
      </p>
    </>
  );
}
