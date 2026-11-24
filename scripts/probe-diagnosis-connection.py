"""Send one owner-authorized connectivity request through the diagnosis client.

The probe never retries: it reserves its single request in a new private directory
before sending, and refuses to run again against an existing reservation. It keeps
bounded, redacted provider rejection detail so a failure can be diagnosed, and it
never prints or stores the credential. A successful probe proves transport and
envelope compatibility only; it is not RCA qualification.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))

from cloudops.features.diagnosis.messages import Message, prepare_payload  # noqa: E402
from cloudops.features.diagnosis.profile import Mode, ModelProfile  # noqa: E402
from cloudops.features.diagnosis.transport import ModelClient  # noqa: E402

PROMPT = "Reply with exactly OK."
SCHEMA = "DiagnosisConnectionProbe/v1"


class ProbeRefused(RuntimeError):
    pass


def read_secret(path: Path, name: str) -> str:
    values = [
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(name + "=")
    ]
    if len(values) != 1 or not values[0]:
        raise ProbeRefused(f"Expected exactly one non-empty {name} in the secret file.")
    return values[0]


def private_write(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def estimate_usd(prompt_tokens: int, completion_tokens: int, rates: dict) -> float:
    return (
        prompt_tokens * rates["input_usd_per_mtok"]
        + completion_tokens * rates["output_usd_per_mtok"]
    ) / 1_000_000


def run(args: argparse.Namespace, *, transport=None) -> dict:
    loaded = ModelProfile.load(args.profile)
    if loaded.model is None or loaded.output_limit is None or loaded.credential is None:
        raise ProbeRefused("The profile must name a model, an output limit and a credential.")
    for name in ("ceiling_usd", "input_usd_per_mtok", "output_usd_per_mtok"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "ceiling_usd" and value == 0):
            raise ProbeRefused(f"--{name.replace('_', '-')} must be a finite non-negative number.")
    payload = loaded.model_dump(mode="json", by_alias=True)
    payload.update(
        mode=Mode.EVALUATION_ONLY.value,
        authorization_id=args.authorization_id,
        budget_policy_id=args.budget_policy_id,
    )
    profile = ModelProfile.model_validate(payload)
    # Keep relative TLS paths resolving against the loaded profile's directory.
    profile._base_dir = loaded._base_dir
    secret = read_secret(args.secret_env, profile.credential.name)
    messages = [Message("user", PROMPT)]
    request_bytes = len(prepare_payload(profile, messages))
    rates = {
        "input_usd_per_mtok": args.input_usd_per_mtok,
        "output_usd_per_mtok": args.output_usd_per_mtok,
        "source_date": args.rates_date,
    }
    # Conservative ceiling: every request byte counted as a token, plus the full
    # output limit (which also bounds reasoning tokens for the Chat Completions API).
    reserved_usd = estimate_usd(request_bytes, profile.output_limit.value, rates)
    if not math.isfinite(reserved_usd) or reserved_usd > args.ceiling_usd:
        raise ProbeRefused(
            f"Reserved cost {reserved_usd:.6f} USD exceeds the {args.ceiling_usd} USD ceiling."
        )
    directory = args.reservation_dir
    if directory.exists():
        raise ProbeRefused("The reservation directory already exists; a probe is never retried.")
    directory.mkdir(mode=0o700, parents=True)
    identity = asdict(profile.identity())

    def reserve() -> None:
        private_write(
            directory / "send-reserved.json",
            {
                "schema": SCHEMA,
                "at": datetime.now(timezone.utc).isoformat(),
                "requests": 1,
                "no_retry": True,
                "ceiling_usd": args.ceiling_usd,
                "reserved_usd": reserved_usd,
                "request_bytes": request_bytes,
                "output_limit": profile.output_limit.value,
                "rates": rates,
                "identity": identity,
            },
        )

    client = ModelClient(
        profile,
        transport=transport,
        environ={profile.credential.name: secret},
        capture_provider_error=True,
    )
    result = client.send(messages, before_send=reserve)
    usage = asdict(result.usage) if result.usage is not None else None
    observed = (
        result.usage_status == "observed"
        and result.usage is not None
        and result.usage.prompt_tokens is not None
        and result.usage.completion_tokens is not None
    )
    receipt = {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "reasoning_effort": profile.reasoning_effort,
        "output_limit": {
            "param": profile.output_limit.param.value,
            "value": profile.output_limit.value,
        },
        "kind": result.kind.value,
        "http_status": result.http_status,
        "error_code": result.error_code,
        "provider_error": asdict(result.provider_error) if result.provider_error else None,
        "finish_reason": result.finish_reason,
        "request_id": result.request_id,
        "served_model": result.served_model,
        "model_matches": result.served_model is not None and result.served_model == profile.model,
        "usage": usage,
        "usage_status": result.usage_status,
        "elapsed_ms": result.elapsed_ms,
        "response_bytes": result.response_bytes,
        "response_sha256": result.response_sha256,
        "ok_marker": result.text is not None and result.text.strip() == "OK",
        "reserved_usd": reserved_usd,
        "estimated_usd": estimate_usd(
            result.usage.prompt_tokens, result.usage.completion_tokens, rates
        )
        if observed
        else None,
        "billed_usd": None,
        "billed_note": "Billed charges come only from the provider's billing records.",
        "retried": False,
        "rca_qualified": False,
    }
    receipt["compatibility_pass"] = (
        receipt["ok_marker"] and result.kind.value == "text" and receipt["model_matches"]
    )
    rendered = json.dumps(receipt)
    if secret in rendered:
        raise ProbeRefused("The receipt would contain the credential; nothing was written.")
    private_write(directory / "receipt.json", receipt)
    return receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--profile", type=Path, required=True)
    value.add_argument("--secret-env", type=Path, required=True)
    value.add_argument("--reservation-dir", type=Path, required=True)
    value.add_argument("--authorization-id", required=True)
    value.add_argument("--budget-policy-id", required=True)
    value.add_argument("--ceiling-usd", type=float, required=True)
    value.add_argument("--input-usd-per-mtok", type=float, required=True)
    value.add_argument("--output-usd-per-mtok", type=float, required=True)
    value.add_argument("--rates-date", required=True, help="Date the rates were checked.")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        receipt = run(args)
    except ProbeRefused as error:
        print(f"Probe refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["compatibility_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
