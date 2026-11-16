import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

import e07_live_worker
from diagnosis_fixtures import profile
from e07_live_worker import Budget, ledger_lock, require_qualification, reserve_send
from cloudops.features.diagnosis.messages import Message
from cloudops.features.diagnosis.schemas import ModelCall, CallKind, Usage
from cloudops.features.diagnosis.settings import Deployment, Qualification
from cloudops.features.diagnosis.transport import ModelClient
from test_diagnosis_validation import policy


REQUEST = {
    "run_id": "11111111-1111-4111-8111-111111111111",
    "analysis_id": "22222222-2222-4222-8222-222222222222",
    "request_digest": "a" * 64,
}


@pytest.fixture
def finite_budget(tmp_path):
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
    return Budget(path, "bounded", 1)


@pytest.mark.parametrize("changed_part", ["deployment", "client"])
def test_e07_rechecks_actual_send_identity_after_startup(monkeypatch, finite_budget, changed_part):
    original = profile(mode="operational", model="gpt-6.1-sol")

    def qualified(candidate):
        deployment = Deployment(candidate, policy(), None)
        return replace(
            deployment,
            qualification=Qualification(
                schema_version="DiagnosisQualification/v1",
                cohort_id="controlled",
                evidence_reference="local://controlled-identity-regression",
                identity=deployment.identity(),
            ),
        )

    current = [qualified(original)]
    finite_budget.data["accepted_E06_identity_unchanged"] = current[0].identity()
    monkeypatch.setattr(e07_live_worker, "load", lambda: current[0])
    require_qualification(finite_budget)
    changed = profile(mode="operational", model="gpt-6.1-sol", base_url="http://127.0.0.1:18081/v1")
    client_profile = original
    if changed_part == "deployment":
        current[0] = qualified(changed)
        assert current[0].denied() is None
    else:
        client_profile = changed
    sent, core_reservations = [], []
    client = ModelClient(
        client_profile,
        transport=httpx.MockTransport(lambda request: sent.append(request)),
        environ={"DIAGNOSIS_TEST_TOKEN": "controlled-test-only"},
    )
    before = finite_budget.path.read_bytes()
    with pytest.raises(ValueError, match="exact retained"):
        client.send(
            [Message("user", "Controlled identity check")],
            before_send=lambda: reserve_send(
                finite_budget,
                client_profile,
                False,
                lambda: core_reservations.append(True),
                REQUEST,
            ),
        )
    assert not sent and not core_reservations
    assert finite_budget.path.read_bytes() == before
    assert finite_budget.data["model_requests_total"] == 0


@pytest.mark.parametrize("filename", ["budget.json", "budget.lock", "budget.tmp"])
def test_e07_atomic_replacement_retains_cross_process_lock(finite_budget, filename):
    path = finite_budget.path.with_name(filename)
    path.write_text(json.dumps(finite_budget.data))
    budget = Budget(path, "lock-regression", 1)
    with ledger_lock(path):
        old_inode = path.stat().st_ino
        budget.reserve("gpt-6.1-sol", False, **REQUEST)
        assert path.stat().st_ino != old_inode
        assert json.loads(path.read_text())["model_requests_total"] == 1
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\nimport sys\nfrom e07_live_worker import ledger_lock\n"
                "try:\n with ledger_lock(Path(sys.argv[1])): print('acquired')\n"
                "except BlockingIOError: print('blocked')\n",
                str(path),
            ],
            cwd=Path(__file__).parent,
            check=True,
            text=True,
            capture_output=True,
        )
        assert child.stdout.strip() == "blocked"


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
    budget.reserve("gpt-6.1-sol", False, **REQUEST)
    restored = Budget(path, "after-pg-restore", 1)
    assert restored.data["model_requests_total"] == 2
    assert all(restored.data["calls"][-1][key] == value for key, value in REQUEST.items())
    with pytest.raises(ValueError, match="Unsettled"):
        restored.reserve("gpt-6.1-sol", False, **REQUEST)


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
        Budget(path, "bounded", 1).reserve("gpt-6.1-sol", False, **REQUEST)


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
    budget.reserve("gpt-6.1-sol", True, **REQUEST)
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
        restored.reserve("gpt-6.1-sol", True, **REQUEST)
    with pytest.raises(ValueError, match="allowance"):
        Budget(path, "no-inference", 0).reserve("gpt-6.1-sol", False, **REQUEST)
