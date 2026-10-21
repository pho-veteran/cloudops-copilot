import { Badge } from "@/components/ui/badge";

export const statusLabels: Record<string, string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  resolved: "Resolved",
  queued: "Queued",
  collecting: "Collecting evidence",
  analyzing: "Analyzing",
  waiting_for_quota: "Waiting for quota",
  completed: "Analysis complete",
  insufficient_evidence: "Insufficient evidence",
  failed: "Failed",
  cancelled: "Cancelled",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
  stale: "Stale",
  expired: "Expired",
  invalidated: "Invalidated",
  none: "Not executed",
  executing: "Executing",
  verifying: "Verifying",
  succeeded: "Verified",
  no_op: "No change needed",
  uncertain: "Unknown write outcome",
  rolling_back: "Rolling back",
  rolled_back: "Rolled back",
  manual_intervention: "Manual intervention",
  available: "Available",
  partial: "Partial data",
  unavailable: "Unavailable",
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
