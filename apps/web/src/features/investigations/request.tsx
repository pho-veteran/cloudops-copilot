import type { components } from "@/lib/api/generated/schema";
import { ApiError } from "@/lib/api/client";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Schemas = components["schemas"];
export type WindowSpec = Schemas["WindowPreset"] | Schemas["WindowInterval"];
export type RequestWindow = Schemas["RequestWindow"];
export type RunView = Schemas["RunView"];
export type WindowChoice =
  | { mode: "preset"; preset: 15 | 30 | 60 }
  | { mode: "explicit"; start: string; end: string };

export const defaultWindow: WindowChoice = { mode: "preset", preset: 30 };

const pad = (value: number) => String(value).padStart(2, "0");
/** A `datetime-local` value in the browser's zone, truncated to the minute. */
export function localInput(date: Date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function explicitDefault(): WindowChoice {
  // Rounding down keeps a slightly fast browser clock from proposing a future end.
  const end = new Date();
  end.setSeconds(0, 0);
  return {
    mode: "explicit",
    start: localInput(new Date(end.getTime() - 30 * 60_000)),
    end: localInput(end),
  };
}

export function toSpec(choice: WindowChoice): WindowSpec {
  if (choice.mode === "preset") return { preset: choice.preset };
  // Invalid input is left as typed; validation reports it and the button stays disabled.
  const iso = (value: string) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toISOString();
  };
  return { start: iso(choice.start), end: iso(choice.end) };
}

/** Mirrors the server rule for early guidance; the server still decides. */
export function windowProblem(choice: WindowChoice, now = new Date()) {
  if (choice.mode === "preset") return null;
  const start = new Date(choice.start).getTime();
  const end = new Date(choice.end).getTime();
  if (Number.isNaN(start) || Number.isNaN(end))
    return "Enter both the start and end of the window.";
  if (end > now.getTime()) return "The window end cannot be in the future.";
  const minutes = (end - start) / 60_000;
  if (minutes < 1 || minutes > 60)
    return "Choose a window that starts before it ends and spans 1 to 60 minutes.";
  return null;
}

const sourceLabels: Record<RequestWindow["source"], string> = {
  preset_15: "Last 15 minutes at admission",
  preset_30: "Last 30 minutes at admission",
  preset_60: "Last 60 minutes at admission",
  explicit: "Explicit interval",
  legacy_default: "Default 30 minutes; window not chosen",
  automatic_default: "Automatic 30 minutes at intake",
};
export function describeWindow(window: RequestWindow | null) {
  if (!window) return "Not captured for this earlier request";
  const start = new Date(window.start);
  const end = new Date(window.end);
  return `${start.toLocaleString()} → ${end.toLocaleString()} (${start.toISOString().slice(11, 16)}–${end.toISOString().slice(11, 16)} UTC) · ${sourceLabels[window.source]}`;
}
export function describeQuestion(run: RunView) {
  if (!run.question) return "Not captured for this earlier request";
  return run.question_source === "title_default"
    ? `${run.question} (defaulted from the incident title)`
    : run.question;
}
export function describeOrigin(origin: Schemas["OriginView"]) {
  return origin.kind === "operator"
    ? "Manual · Operator"
    : `Alertmanager · ${origin.source_id ?? "unknown source"}`;
}
export const runStateLabel = (run: Pick<RunView, "state" | "queue_reason">) =>
  run.queue_reason === "capacity"
    ? "waiting for capacity"
    : run.state.replaceAll("_", " ");

export function WindowFields({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: WindowChoice;
  onChange: (value: WindowChoice) => void;
  disabled?: boolean;
}) {
  const problem = windowProblem(value);
  return (
    <fieldset className="foundation-stack" disabled={disabled}>
      <legend>Time window</legend>
      <Label htmlFor={`${id}-window`}>Investigate</Label>
      <select
        id={`${id}-window`}
        value={value.mode === "preset" ? String(value.preset) : "explicit"}
        onChange={(event) =>
          onChange(
            event.target.value === "explicit"
              ? explicitDefault()
              : {
                  mode: "preset",
                  preset: Number(event.target.value) as 15 | 30 | 60,
                },
          )
        }
      >
        <option value="15">Last 15 minutes</option>
        <option value="30">Last 30 minutes (default)</option>
        <option value="60">Last 60 minutes</option>
        <option value="explicit">Custom interval (up to 60 minutes)</option>
      </select>
      {value.mode === "preset" ? (
        <p className="muted">
          The server fixes the absolute UTC window when it accepts the request;
          a retry keeps that window.
        </p>
      ) : (
        <>
          <Label htmlFor={`${id}-start`}>Start (your local time)</Label>
          <Input
            id={`${id}-start`}
            type="datetime-local"
            required
            value={value.start}
            onChange={(event) => onChange({ ...value, start: event.target.value })}
          />
          <Label htmlFor={`${id}-end`}>End (your local time)</Label>
          <Input
            id={`${id}-end`}
            type="datetime-local"
            required
            value={value.end}
            onChange={(event) => onChange({ ...value, end: event.target.value })}
          />
          {problem ? (
            <p role="alert">{problem}</p>
          ) : (
            <p className="muted">
              UTC {new Date(value.start).toISOString()} →{" "}
              {new Date(value.end).toISOString()}
            </p>
          )}
        </>
      )}
    </fieldset>
  );
}

// Uncertain retries reuse the same key across remounts; a definite answer settles it.
const pendingKeys = new Map<string, { input: string; key: string }>();
export function requestKey(scope: string, input: unknown) {
  const serialized = JSON.stringify(input);
  const pending = pendingKeys.get(scope);
  if (pending?.input === serialized) return pending.key;
  const key = crypto.randomUUID();
  pendingKeys.set(scope, { input: serialized, key });
  return key;
}
export function settleKey(scope: string, error?: unknown, key?: string) {
  const definite =
    error === undefined ||
    (error instanceof ApiError && error.status >= 400 && error.status < 500);
  if (definite && (key === undefined || pendingKeys.get(scope)?.key === key)) {
    pendingKeys.delete(scope);
  }
}
export function clearRequestKeys() {
  pendingKeys.clear();
}
