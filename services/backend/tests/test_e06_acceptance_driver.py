"""Campaign controls use real PostgreSQL/worker and owned loopback HTTP, never paid CI."""

from dataclasses import replace
import json
import os
import sys

import pytest
from sqlalchemy.engine import make_url
from diagnosis_fake import respond, serve
from diagnosis_fixtures import text_payload
from test_diagnosis_validation import _raw

import e06_live_acceptance as campaign
from cloudops.features.diagnosis.profile import ModelProfile
from cloudops.features.diagnosis.settings import load
from cloudops.features.diagnosis.schemas import CallKind
from cloudops.features.diagnosis.transport import ModelClient


def test_budget_is_durable_bounded_and_redacts_decoded_credential(tmp_path):
    campaign.freeze(tmp_path)
    budget = campaign.Budget(tmp_path / "ledger.json")
    budget.reserve(campaign.MODELS[0], "case", True)
    with pytest.raises(ValueError, match="repair"):
        budget.reserve(campaign.MODELS[0], "another", True)
    with pytest.raises(ValueError, match="Unapproved"):
        budget.reserve("unapproved", "another", False)
    from diagnosis_fixtures import profile
    from diagnosis_fake import mock
    import httpx

    client = ModelClient(
        profile(),
        transport=mock(lambda _: httpx.Response(200, json=text_payload('{"summary":"secret"}'))),
        environ={"DIAGNOSIS_TEST_TOKEN": "fixture"},
    )
    call = client.send(
        [{"role": "user", "content": "Fixed controlled probe"}], before_send=lambda: None
    )
    budget.settle(
        replace(call, kind=CallKind.TEXT, text='{"summary":"\\u0073ecret"}', served_model="secret"),
        "secret",
    )
    assert "secret" not in (tmp_path / "ledger.json").read_text()
    reloaded = campaign.Budget(tmp_path / "ledger.json")
    assert len(reloaded.data["calls"]) == 1 and reloaded.data["repairs"] == 1
    for _ in range(14):
        reloaded.reserve(campaign.MODELS[0] if _ < 13 else campaign.MODELS[1], "remaining", False)
    with pytest.raises(ValueError, match="budget exhausted"):
        reloaded.reserve(campaign.MODELS[0], "sixteenth", False)
    reloaded.data["calls"] = []
    reloaded.data["observed_tokens"] = campaign.TOKEN_STOP
    with pytest.raises(ValueError, match="token stop"):
        reloaded.reserve(campaign.MODELS[0], "over token stop", False)


def test_entire_initial_driver_uses_existing_lifecycle_and_excludes_evaluation(
    db, seeded, tmp_path, monkeypatch
):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    cases = json.loads((campaign.FIXTURES / "cases.json").read_text())
    oracle = json.loads((campaign.FIXTURES / "oracle.json").read_text())["cases"]
    sent = []

    def answer(handler):
        index = len(sent)
        sent.append(index)
        if index == len(cases):
            respond(handler, text_payload("OK"))
            return
        expected = oracle[cases[index]["id"]]
        healthy = cases[index]["id"] == "n-healthy"
        respond(
            handler,
            text_payload(
                _raw(
                    status="insufficient_evidence" if healthy else "supported",
                    cause_category=expected["category"],
                    supporting_evidence_ids=[] if healthy else ["E001"],
                    candidate_template_id=next(iter(expected["allowed_candidates"]), None),
                )
            ),
        )

    with serve(answer) as server:
        original = campaign.configure

        def configured(root, model, **kwargs):
            original(root, model, **kwargs)
            path = root / "profile.json"
            value = json.loads(path.read_text())
            value["base_url"] = server.url + "/v1"
            path.write_text(ModelProfile.model_validate(value).model_dump_json(by_alias=True))
            return load().identity()

        monkeypatch.setattr(campaign, "configure", configured)
        monkeypatch.setattr(
            campaign, "OWNED_DATABASE", make_url(os.environ["TEST_DATABASE_URL"]).database
        )
        monkeypatch.setenv("E06_OWNED_LAB", "e06-r5")
        monkeypatch.setenv("DIAGNOSIS_LIVE_TOKEN", "owned-fixture-token")
        campaign.freeze(tmp_path)
        monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(tmp_path)])
        campaign.main()
        ledger = json.loads((tmp_path / "ledger.json").read_text())
        assert len(sent) == len(ledger["calls"]) == 13
        assert ledger["cases"]["n-required-gap"]["calls"] == 0
        assert all(case["excluded_from_operational_api"] for case in ledger["cases"].values())
        assert ledger["secondary_probe"]["receipt"]["schema_success"] is True
        assert ledger["secondary_probe"]["quality"] == "not_established"
        assert ledger["evaluation_finished"] is True
        with pytest.raises(ValueError, match="already started"):
            campaign.main()
        assert len(sent) == 13


def test_driver_denies_other_database_or_missing_owner_before_any_send(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(tmp_path)])
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/unrelated_test")
    monkeypatch.delenv("E06_OWNED_LAB", raising=False)
    with pytest.raises(ValueError, match="explicitly owned"):
        campaign.main()
    assert not (tmp_path / "campaign.lock").exists()
