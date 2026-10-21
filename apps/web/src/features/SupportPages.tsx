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
    recommended: "Không đề xuất",
    coverage: "96%",
    status: "Excluded · recent OOM",
    reason:
      "Có OOM trong cửa sổ thu thập. Không hạ memory request/limit; cần theo dõi sau phục hồi.",
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
      "Dựa trên window demo 6 giờ; chưa đại diện chu kỳ tải tuần. Chỉ xuất đề xuất, không tự apply.",
  },
  {
    id: "media",
    name: "media-api",
    current: "CPU 600m · RAM 512 / 768 MiB",
    observed: "CPU p95 380m · RAM max 330 MiB",
    recommended: "Không đề xuất",
    coverage: "97%",
    status: "Excluded · HPA",
    reason:
      "Workload do HPA điều khiển. Cần đánh giá tương tác giữa requests và autoscaling trước khi right-size.",
  },
  {
    id: "worker",
    name: "worker-api",
    current: "CPU 250m · RAM 256 / 512 MiB",
    observed: "Không đủ mẫu",
    recommended: "Không đề xuất",
    coverage: "22%",
    status: "Insufficient",
    reason:
      "Pod Pending; dữ liệu usage không đủ. Thu thêm telemetry sau khi workload chạy ổn định.",
  },
  {
    id: "search",
    name: "search-api",
    current: "CPU 250m · RAM 128 / 256 MiB",
    observed: "Mẫu cuối: 09:00",
    recommended: "Không đề xuất",
    coverage: "61%",
    status: "Stale",
    reason:
      "Nguồn telemetry ngừng cập nhật. Report cũ không đủ điều kiện ra quyết định.",
  },
];
const runbooks = [
  {
    id: "oom",
    title: "OOM: phân biệt giới hạn thấp và memory leak",
    version: "v3",
    tags: ["OOM", "memory"],
    source: "runbooks/oom.md",
    body: "1. Đối chiếu OOMKilled, restart count và memory limit trong cùng cửa sổ.\n2. Kiểm tra working set trước lần restart; không kết luận memory leak chỉ từ OOM.\n3. Xem profile server-side, headroom và quota. Thiếu bằng chứng → manual guidance.\n4. Sau thay đổi, theo dõi restart và memory trong cửa sổ verification.\nĐiều kiện: không tự hạ memory khi vừa có OOM.",
  },
  {
    id: "rollout",
    title: "CrashLoop sau rollout: kiểm tra và phục hồi",
    version: "v3",
    tags: ["CrashLoop", "rollout"],
    source: "runbooks/failed-rollout.md",
    body: "1. So sánh thời điểm rollout với lần lỗi đầu tiên.\n2. Đọc previous logs, exit code và events; liên kết đúng Deployment UID.\n3. Chỉ restore snapshot known-good đã xác nhận trước sự cố.\n4. Review diff, preconditions và approval TTL. Không có baseline → hướng dẫn thủ công.\n5. Xác minh rollout ready và không có restart mới.",
  },
  {
    id: "load",
    title: "Latency tăng: kiểm tra tải và capacity",
    version: "v3",
    tags: ["Load", "capacity"],
    source: "runbooks/traffic-spike.md",
    body: "1. So sánh RPS, latency, errors và CPU throttling.\n2. Kiểm tra replicas, node headroom và HPA.\n3. Chỉ scale trong bounds nếu không xung đột controller.\n4. Verification phải giữ cùng mức tải; ghi thời điểm load generator dừng.\n5. Nếu lỗi đến từ dependency, scale không được xem là giải pháp đã chứng minh.",
  },
  {
    id: "pending",
    title: "Pending / ImagePull: thu evidence và handoff",
    version: "v3",
    tags: ["Pending", "ImagePull"],
    source: "runbooks/pod-startup.md",
    body: "1. Đọc scheduling / image pull events và trạng thái container.\n2. Đối chiếu requests, quota, node capacity và image reference.\n3. Chỉ báo cáo nguyên nhân có evidence; lỗi quyền đọc là Not checked.\n4. Hai nhóm này investigation-only trong release. Không tạo action ghi.\n5. Ghi bước kiểm tra tiếp theo và người nhận handoff.",
  },
];
const integrations = [
  {
    id: "worker",
    name: "CloudOps worker",
    subtitle: "Durable workflow · demo-worker-01",
    status: "Sẵn sàng",
    detail:
      "Một worker; job có thể retry và giữ lịch sử. Không chạy Akmatori/Aurora runtime.",
  },
  {
    id: "kubernetes",
    name: "Kubernetes",
    subtitle: "lab-k3s · namespace cloudops-lab",
    status: "Sẵn sàng",
    detail:
      "Collector chỉ đọc; executor tách quyền ghi. Kết nối demo không truy cập cluster thật.",
  },
  {
    id: "prometheus",
    name: "Prometheus",
    subtitle: "metrics.internal · cửa sổ 6 giờ",
    status: "Stale",
    detail:
      "Mẫu search-api đã cũ. Retry demo chỉ cập nhật trạng thái kết nối; không sửa report lịch sử.",
  },
  {
    id: "model",
    name: "Model endpoint",
    subtitle: "OpenAI-compatible · ops-model",
    status: "Chưa kiểm tra",
    detail:
      "Base URL cấu hình phía server: https://model.example/v1 · credential ref: MODEL_API_KEY. Không lưu key trong browser.",
  },
  {
    id: "embedding",
    name: "Runbook retrieval",
    subtitle: "Embedding optional · catalog fallback",
    status: "Degraded",
    detail:
      "Index chưa cập nhật. Vẫn đọc được bản runbook đã lưu; retrieval có thể bỏ sót tài liệu mới.",
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
          Demo tương tác
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
  const [tag, setTag] = useState("Tất cả");
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
      (tag === "Tất cả" || r.tags.includes(tag)),
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
        "Không thể xác nhận: candidate không còn healthy hoặc phiên không có quyền.",
      );
      setConfirm(null);
      return;
    }
    dispatch({ type: "confirm_known_good", workloadId: confirm });
    setNotice(
      "Đã xác nhận known-good metadata trong demo. Không có Kubernetes write.",
    );
    setConfirm(null);
  };

  if (page === "workloads")
    return (
      <div className="space-y-6">
        <Header
          eyebrow="Tài nguyên & tối ưu"
          title="Workloads"
          description="Đọc tình trạng workload, chuẩn bị known-good baseline và bắt đầu điều tra theo scope. Recommendations chỉ hỗ trợ quyết định; không tự thay đổi tài nguyên."
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
                aria-label="Tìm workload"
                className="pl-9"
                placeholder="Tìm workload, namespace hoặc dấu hiệu…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <Card className="overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Workload</TableHead>
                    <TableHead>Tình trạng</TableHead>
                    <TableHead>Controller</TableHead>
                    <TableHead>Known-good</TableHead>
                    <TableHead className="text-right">Thao tác</TableHead>
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
                            ? "Healthy · ổn định 30 phút"
                            : w.pattern}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {w.controller === "HPA" ? "HPA" : "Deployment"}
                      </TableCell>
                      <TableCell>
                        {state.knownGood[w.id]
                          ? "Đã xác nhận"
                          : "Chưa có baseline"}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSelected(w.id)}
                        >
                          Xem chi tiết
                          <ArrowRight className="ml-1 h-3 w-3" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {!filteredWorkloads.length && (
                <p className="p-8 text-center text-muted-foreground">
                  Không có workload khớp tìm kiếm.{" "}
                  <button className="underline" onClick={() => setQuery("")}>
                    Xóa bộ lọc
                  </button>
                </p>
              )}
            </Card>
            <p className="text-xs text-muted-foreground">
              Scope demo: 1 cluster · cloudops-lab. Healthy dựa trên candidate
              fixture, không suy từ việc không có alert.
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
                  aria-label="Chọn report"
                  className="rounded-md border bg-background px-3 py-2 text-sm"
                  value={report}
                  onChange={(e) => setReport(e.target.value)}
                >
                  <option value="current">Report 10:00–16:00 · 6 giờ</option>
                  <option value="old">Report hôm qua · stale</option>
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
                                ? "Không đề xuất: report stale"
                                : r.recommended,
                          })),
                        },
                        null,
                        2,
                      ),
                      "application/json",
                    );
                    setNotice(
                      "Đã xuất report JSON có coverage, units và exclusions. Export không phải apply.",
                    );
                  }}
                >
                  <ArrowDownToLine className="mr-2 h-4 w-4" />
                  Xuất JSON
                </Button>
              </div>
            </div>
            <Alert>
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>
                {report === "old"
                  ? "Report đã cũ — không dùng để apply"
                  : "Chỉ là advisory từ 6 giờ dữ liệu demo"}
              </AlertTitle>
              <AlertDescription>
                Không phải billing savings. Giảm requests trên EC2 cố định không
                tự giảm hóa đơn. Không có auto-apply; không bỏ exclusions bằng
                UI.
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
                    <TableHead>Kết quả</TableHead>
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
                          {r.coverage} coverage · 6 giờ
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">{r.current}</TableCell>
                      <TableCell className="text-xs">{r.observed}</TableCell>
                      <TableCell className="text-xs">
                        {report === "old"
                          ? "Không đề xuất: stale"
                          : r.recommended}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {report === "old" ? "Stale" : r.status}
                        </Badge>
                        <p className="mt-2 max-w-xs text-xs leading-5 text-muted-foreground">
                          {report === "old"
                            ? "Cần report mới trước khi review."
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
                      ? "Healthy · 30 phút"
                      : `Chưa ổn định · ${chosen.pattern}`}
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
                      : "Chưa xác nhận"}
                  </div>
                </div>
                <Alert>
                  <ShieldCheck className="h-4 w-4" />
                  <AlertDescription>
                    Baseline chỉ lưu metadata khi healthy. Snapshot không đủ
                    điều kiện không thể dùng để restore. UID/hash/health phải
                    được kiểm tra lại trước khi lưu trong backend thật.
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
                    Bắt đầu điều tra
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
                      ? "Known-good đã lưu"
                      : "Xác nhận known-good"}
                  </Button>
                </div>
                {!state.healthyCandidates[chosen.id] && (
                  <p className="text-xs text-muted-foreground">
                    Không thể xác nhận baseline khi workload chưa healthy.
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
                          "Đã mô phỏng rollout lỗi cho catalog. Mở incident mới để tiếp tục investigation.",
                        );
                        setSelected(null);
                      }}
                    >
                      <Activity className="mr-2 h-4 w-4" />
                      Demo: inject rollout lỗi
                    </Button>
                  )}
                <div className="border-t pt-4">
                  <h3 className="mb-2 text-sm font-medium">
                    Incident liên quan
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
                      Chưa có incident. Có thể chủ động điều tra workload.
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
              <DialogTitle>Xác nhận candidate known-good?</DialogTitle>
              <DialogDescription>
                Chỉ xác nhận bản bạn đã review. Đây là metadata demo; không thay
                đổi tài nguyên.
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
                  ? "Healthy · 30 phút"
                  : "Không đủ điều kiện"}
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirm(null)}>
                Hủy
              </Button>
              <Button
                disabled={
                  !canWrite || !confirm || !state.healthyCandidates[confirm]
                }
                onClick={confirmBaseline}
              >
                Xác nhận snapshot
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
          eyebrow="Nguồn tri thức"
          title="Knowledge"
          description="Runbook có source và version rõ ràng. Tài liệu hướng dẫn điều tra; telemetry live mới là evidence cho kết luận incident."
        />
        {feedback}
        <Alert>
          <Database className="h-4 w-4" />
          <AlertTitle>
            {indexDegraded
              ? "Retrieval index degraded"
              : "Index demo đã đồng bộ"}
          </AlertTitle>
          <AlertDescription>
            {indexDegraded
              ? "Bản mới có thể chưa searchable qua retrieval. Catalog vẫn đọc được tài liệu đã lưu."
              : "Đã mô phỏng reindex 4 runbooks. Không gọi embedding provider."}
            <Button
              className="ml-3"
              size="sm"
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                setIndexDegraded((v) => !v);
                setNotice(
                  indexDegraded
                    ? "Reindex demo hoàn tất; 4 version đã được đánh dấu indexed."
                    : "Đã mô phỏng lỗi index để kiểm tra degraded UX.",
                );
              }}
            >
              {indexDegraded ? "Retry reindex demo" : "Mô phỏng degraded"}
            </Button>
          </AlertDescription>
        </Alert>
        <div className="flex flex-wrap gap-3">
          <div className="relative min-w-56 flex-1">
            <Search className="absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
            <Input
              aria-label="Tìm runbook"
              className="pl-9"
              placeholder="Tìm nội dung, tiêu đề hoặc tag…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <select
            aria-label="Lọc tag runbook"
            className="rounded-md border bg-background px-3 py-2 text-sm"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
          >
            {[
              "Tất cả",
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
                  Đọc runbook
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
        {!filteredBooks.length && (
          <Card>
            <CardContent className="p-8 text-center text-sm text-muted-foreground">
              Không tìm thấy runbook.{" "}
              <button
                className="underline"
                onClick={() => {
                  setQuery("");
                  setTag("Tất cả");
                }}
              >
                Xóa bộ lọc
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
                {activeRunbook?.source} · {activeRunbook?.version} · bản hiện
                hành trong catalog demo
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
                Tải bản Markdown
              </Button>
              <Button onClick={() => setSelected(null)}>Đóng</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    );

  return (
    <div className="space-y-6">
      <Header
        eyebrow="Vận hành hệ thống"
        title="Connections"
        description="Phân biệt transport, RCA contract và độ mới dữ liệu. Mọi kiểm tra bên dưới là mô phỏng local; không kết nối endpoint hay đọc secret thật."
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
              Provider chưa trả usage; không giả định zero.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Remaining quota</p>
            <p className="mt-2 font-medium">Unknown</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Không suy quota từ số request local.
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
                          ? "Sẵn sàng"
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
                      ? "Last attempt: vừa chạy mô phỏng local"
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
                  Hướng dẫn
                </Button>
                {c.id !== "model" && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!canWrite}
                    onClick={() => {
                      setConnectionStates((v) => ({
                        ...v,
                        [c.id]: "Sẵn sàng · demo",
                      }));
                      if (c.id === "embedding") setIndexDegraded(false);
                      setNotice(
                        `${c.name}: retry mô phỏng hoàn tất. Không có network request.`,
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
          <CardTitle className="text-base">Kiểm tra model endpoint</CardTitle>
          <CardDescription>
            Transport OK chỉ xác nhận truy cập. RCA contract: structured JSON,
            evidence IDs, scope and abstention. Evidence is supplied by the
            fixed workflow; model tool calling is not required.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <select
              aria-label="Kịch bản kiểm tra endpoint"
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
                            contract: "Not checked · transport thất bại",
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
              Chạy kiểm tra demo
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
            Capabilities chỉ được đánh dấu sau contract pass. Kiểm tra demo
            không cấp quyền execute hoặc tự thay model.
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
              Cấu hình {integrations.find((c) => c.id === setup)?.name}
            </DialogTitle>
            <DialogDescription>
              Hướng dẫn setup có scope; không có secret editor hoặc endpoint
              editor trong browser.
            </DialogDescription>
          </DialogHeader>
          <ol className="list-decimal space-y-3 pl-5 text-sm leading-6">
            {setup === "model" ? (
              <>
                <li>
                  Đặt base URL OpenAI-compatible và model ID trong cấu hình
                  server. URL hiển thị ở đây chỉ là example fixture.
                </li>
                <li>
                  Đặt credential qua secret/environment reference MODEL_API_KEY.
                  Không paste key vào UI hay runbook.
                </li>
                <li>
                  Cấu hình request/token budget, timeout, retry và concurrency.
                  Không tự fallback sang endpoint trả phí.
                </li>
                <li>
                  Chạy transport test rồi RCA contract test; lỗi contract giữ
                  integration degraded. Usage không được provider trả →
                  unavailable.
                </li>
              </>
            ) : setup === "kubernetes" ? (
              <>
                <li>Chỉ kết nối cluster lab-k3s và namespace cloudops-lab.</li>
                <li>
                  Cấp credential read-only cho collector; executor dùng
                  credential riêng với action allowlist.
                </li>
                <li>
                  Kiểm tra quyền đọc pods, logs, events và Deployment; không cấp
                  cluster-admin để bỏ qua lỗi.
                </li>
              </>
            ) : setup === "embedding" ? (
              <>
                <li>
                  Embedding là optional; cấu hình provider/model phía server nếu
                  dùng semantic retrieval.
                </li>
                <li>
                  Index theo source checksum và version. Không trộn
                  dimension/model khác nhau.
                </li>
                <li>
                  Khi index lỗi, vẫn cho đọc catalog; retry không sửa snapshot
                  evidence cũ.
                </li>
              </>
            ) : (
              <>
                <li>
                  Kiểm tra service trong deployment nội bộ và health endpoint
                  theo tài liệu setup.
                </li>
                <li>
                  Xác minh timestamp của nguồn, timeout và quyền truy cập; giữ
                  rõ missing/stale.
                </li>
                <li>
                  Retry trong prototype chỉ mô phỏng trạng thái. Không khẳng
                  định telemetry lịch sử đã được phục hồi.
                </li>
              </>
            )}
          </ol>
          <DialogFooter>
            <Button onClick={() => setSetup(null)}>Đã hiểu</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
