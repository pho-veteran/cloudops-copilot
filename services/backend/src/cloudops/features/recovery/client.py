import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from cloudops.shared.errors import DomainError


def gateway(path, body=None, *, authenticated=False):
    server = os.environ.get("EXECUTOR_URL")
    if not server:
        raise DomainError(503, "executor_unavailable", "The isolated executor is not configured.")
    headers = {"Content-Type": "application/json"}
    if body is not None or authenticated:
        try:
            headers["Authorization"] = (
                "Bearer " + Path(os.environ["EXECUTOR_DISPATCH_KEY"]).read_text().strip()
            )
        except (OSError, KeyError):
            raise DomainError(
                503, "executor_unavailable", "This process has no executor dispatch credential."
            ) from None
    request = Request(
        server.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404 and path.startswith("/operations/"):
            raise DomainError(
                404,
                "operation_not_found",
                "No durable receipt is available; absence does not prove no write.",
            ) from None
        raise DomainError(
            409 if error.code == 409 else 503,
            "target_quarantined",
            "The target is not enabled for this plan. Inspect current authority and maintenance history.",
        ) from None
    except (URLError, OSError, ValueError):
        raise DomainError(
            503,
            "executor_unavailable",
            "Executor response unavailable; do not infer whether an admitted write happened.",
        ) from None


def target(uid):
    return gateway("/targets/" + quote(uid, safe=""))


def operation(operation_id):
    return gateway("/operations/" + quote(str(operation_id), safe=""), authenticated=True)
