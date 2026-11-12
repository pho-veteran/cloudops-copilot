"""Trusted maintenance CLI handlers for the immutable runbook index."""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from cloudops.features.knowledge.catalog import index_status
from cloudops.features.knowledge.indexing import index_corpus
from cloudops.shared.database import require_schema, sessions
from cloudops.shared.errors import DomainError


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, DomainError):
        return exc.message
    if isinstance(exc, SQLAlchemyError):
        return "Knowledge database operation failed. Check database availability and retry."
    if isinstance(exc, ValidationError):
        return "Knowledge input validation failed."
    message = " ".join(str(exc).split())
    return message[:512] or "Knowledge operation failed."


def _print(value: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
    else:
        for key, item in value.items():
            if item is not None:
                print(f"{key}: {item}")


def run_knowledge(args) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        require_schema()
        if args.command == "knowledge-index":
            result = index_corpus(getattr(args, "root", "."))
            payload = result.model_dump(mode="json")
            _print(payload, json_output=json_output)
            return 1 if result.outcome in {"failed", "index_busy"} else 0
        with sessions()() as db:
            payload = index_status(db).model_dump(mode="json")
        _print(payload, json_output=json_output)
        return 0
    except (DomainError, ValidationError, RuntimeError, SQLAlchemyError) as exc:
        message = _safe_message(exc)
        print(f"error: {message}", file=sys.stderr)
        if json_output:
            print(json.dumps({"outcome": "failed", "reason": message}, sort_keys=True))
        return 1
