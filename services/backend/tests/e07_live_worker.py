"""Finite trusted E07 worker; never collected by pytest or run by CI."""

import argparse
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import make_url


def save(path, value):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Budget:
    def __init__(self, path, case, allowance):
        self.path, self.case, self.allowance = path, case, allowance
        self.data = json.loads(path.read_text())
        self.sent = 0

    def check(self, model, repair):
        if (
            model != "gpt-6.1-sol"
            or self.data["request_limit"] != 50
            or self.data["observed_token_stop"] != 60000
            or self.data["model_requests_total"] != len(self.data["calls"])
        ):
            raise ValueError("Unexpected E07 authorization or ledger identity")
        if any(call.get("outcome") == "unsettled" for call in self.data["calls"]):
            raise ValueError("Unsettled request requires explicit reconciliation")
        if self.sent >= self.allowance or self.data["model_requests_total"] >= 50:
            raise ValueError("Finite inference allowance exhausted")
        if self.data["observed_tokens"] >= 60000:
            raise ValueError("Observed token stop reached")
        if repair and any(call.get("repair") for call in self.data["calls"]):
            raise ValueError("Shared structural repair reserve exhausted")

    def reserve(self, model, repair):
        self.check(model, repair)
        self.data["calls"].append(
            {"case": self.case, "model": model, "repair": repair, "outcome": "unsettled"}
        )
        self.data["model_requests_total"] += 1
        self.data["remaining_requests"] = 50 - self.data["model_requests_total"]
        self.sent += 1
        save(self.path, self.data)

    def settle(self, outcome):
        record = asdict(outcome)
        record.pop("text")
        self.data["calls"][-1]["outcome"] = record
        if outcome.usage:
            self.data["observed_tokens"] += max(
                outcome.usage.total_tokens or 0,
                (outcome.usage.prompt_tokens or 0) + (outcome.usage.completion_tokens or 0),
            )
        if outcome.usage is None or outcome.usage.total_tokens is None:
            self.data["missing_usage_calls"] += 1
        save(self.path, self.data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--seconds", type=int, choices=range(1, 901), required=True)
    parser.add_argument("--requests", type=int, choices=range(4), default=0)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    if make_url(os.environ["DATABASE_URL"]).database != "cloudops_e07_integrated_test":
        raise ValueError("Only the owned integrated acceptance database is supported")
    with args.ledger.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        budget = Budget(args.ledger, args.case, args.requests)
        from cloudops.features.diagnosis.transport import ModelClient
        from cloudops.features.diagnosis.settings import load
        from cloudops.features.diagnosis.models import Analysis
        from cloudops.entrypoints import worker
        from cloudops.shared.database import sessions

        if args.collect_only:
            worker.HANDLERS = {
                "development_collection_v1": worker.HANDLERS["development_collection_v1"]
            }

        deployment = load()
        if args.requests and (
            deployment.denied()
            or deployment.identity() != budget.data["accepted_E06_identity_unchanged"]
        ):
            raise ValueError("The exact retained E06 operational qualification is required")
        original, halted = ModelClient.send, []

        def counted(client, messages, *, before_send):
            content, _ = json.JSONDecoder().raw_decode(messages[-1].content)
            with sessions()() as db:
                phase = db.scalar(
                    select(Analysis.phase).where(
                        Analysis.run_id == UUID(content["scope"]["run_id"]),
                        Analysis.purpose == "operational",
                        Analysis.state == "analyzing",
                    )
                )
            if phase not in {"initial", "repair"}:
                halted.append(True)
                raise ValueError("Only an initial request or one structural repair is authorized")
            repair = phase == "repair"

            def reserve():
                try:
                    budget.check(client.profile.model, repair)
                    before_send()
                    budget.reserve(client.profile.model, repair)
                except BaseException:
                    halted.append(True)
                    raise

            previous = budget.sent
            outcome = original(client, messages, before_send=reserve)
            if budget.sent != previous:
                budget.settle(outcome)
            if (
                outcome.kind != "text"
                or outcome.served_model != "gpt-6.1-sol"
                or outcome.usage is None
                or outcome.usage.total_tokens is None
            ):
                halted.append(True)
            return outcome

        ModelClient.send = counted
        try:
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and not halted:
                worker.run_once()
                time.sleep(1)
        finally:
            ModelClient.send = original
        if halted:
            raise RuntimeError("Finite worker stopped; inspect retained outcomes before continuing")


if __name__ == "__main__":
    main()
