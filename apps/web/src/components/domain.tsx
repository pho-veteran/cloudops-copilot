import { Badge } from "@/components/ui/badge";

export const statusLabels: Record<string, string> = {
  open: "Đang mở",
  acknowledged: "Đã tiếp nhận",
  resolved: "Đã đóng",
  queued: "Đang chờ",
  collecting: "Thu thập evidence",
  analyzing: "Đang phân tích",
  waiting_for_quota: "Chờ quota",
  completed: "Phân tích xong",
  insufficient_evidence: "Thiếu evidence",
  failed: "Không đạt",
  cancelled: "Đã hủy",
  awaiting_approval: "Chờ duyệt",
  approved: "Đã duyệt",
  rejected: "Đã từ chối",
  stale: "Dữ liệu đã đổi",
  expired: "Hết hạn",
  invalidated: "Đã vô hiệu",
  none: "Chưa thực thi",
  executing: "Đang thực thi",
  verifying: "Đang xác minh",
  succeeded: "Đã xác minh",
  no_op: "Không cần thay đổi",
  uncertain: "Chưa rõ kết quả ghi",
  rolling_back: "Đang rollback",
  rolled_back: "Đã rollback",
  manual_intervention: "Cần xử lý thủ công",
  available: "Có dữ liệu",
  partial: "Dữ liệu một phần",
  unavailable: "Không có dữ liệu",
};
export function StatusBadge({ status }: { status: string }) {
  const tone = ["succeeded", "rolled_back"].includes(status)
    ? "success"
    : ["failed", "uncertain"].includes(status)
      ? "danger"
      : [
            "waiting_for_quota",
            "insufficient_evidence",
            "stale",
            "expired",
            "manual_intervention",
            "partial",
          ].includes(status)
        ? "warning"
        : "neutral";
  return (
    <Badge variant="outline" className={`status-badge ${tone}`}>
      <span className="status-dot" aria-hidden="true" />
      {statusLabels[status] || status}
    </Badge>
  );
}
export function downloadText(
  filename: string,
  text: string,
  type = "text/markdown;charset=utf-8",
) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
