import json

import pytest

from e07_live_worker import Budget
from cloudops.features.diagnosis.schemas import ModelCall, CallKind, Usage


def test_e07_reservation_survives_reopen_and_blocks_unknown_retry(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "request_limit": 50,
                "observed_token_stop": 60000,
                "model_requests_total": 1,
                "observed_tokens": 72,
                "calls": [{"outcome": {"kind": "text"}}],
            }
        )
    )
    budget = Budget(path, "live-restore", 1)
    budget.reserve("gpt-6.1-sol", False)
    restored = Budget(path, "after-pg-restore", 1)
    assert restored.data["model_requests_total"] == 2
    with pytest.raises(ValueError, match="Unsettled"):
        restored.reserve("gpt-6.1-sol", False)


@pytest.mark.parametrize(
    "field,value", [("model_requests_total", 50), ("observed_tokens", 60000), ("request_limit", 51)]
)
def test_e07_never_extends_authorized_budget(tmp_path, field, value):
    path = tmp_path / "ledger.json"
    data = {
        "request_limit": 50,
        "observed_token_stop": 60000,
        "model_requests_total": 1,
        "observed_tokens": 72,
        "calls": [{"outcome": {"kind": "text"}}],
    }
    data[field] = value
    if field == "model_requests_total":
        data["calls"] *= value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        Budget(path, "bounded", 1).reserve("gpt-6.1-sol", False)


def test_e07_settlement_retains_usage_and_one_shared_repair(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "request_limit": 50,
                "observed_token_stop": 60000,
                "model_requests_total": 0,
                "observed_tokens": 0,
                "missing_usage_calls": 0,
                "calls": [],
            }
        )
    )
    budget = Budget(path, "first-case", 1)
    budget.reserve("gpt-6.1-sol", True)
    budget.settle(
        ModelCall(
            kind=CallKind.TEXT,
            identity=None,
            text="private telemetry",
            finish_reason="stop",
            usage=Usage(10, 5, 15),
            usage_status="observed",
            request_id=None,
            served_model="gpt-6.1-sol",
            http_status=200,
            error_code=None,
            retry_after_seconds=None,
            retry_after_raw=None,
            elapsed_ms=1,
            response_bytes=100,
        )
    )
    restored = Budget(path, "second-case", 1)
    assert restored.data["observed_tokens"] == 15
    assert "text" not in restored.data["calls"][0]["outcome"]
    with pytest.raises(ValueError, match="repair reserve"):
        restored.reserve("gpt-6.1-sol", True)
    with pytest.raises(ValueError, match="allowance"):
        Budget(path, "no-inference", 0).reserve("gpt-6.1-sol", False)
