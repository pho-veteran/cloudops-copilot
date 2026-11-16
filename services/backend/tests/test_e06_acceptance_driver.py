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
    reloaded.data["prior_requests"] = campaign.MAX_REQUESTS - 2
    reloaded.reserve(campaign.MODELS[1], "last", False)
    with pytest.raises(ValueError, match="budget exhausted"):
        reloaded.reserve(campaign.MODELS[0], "over global limit", False)
    reloaded.data["calls"] = []
    reloaded.data["prior_requests"] = 0
    reloaded.data["observed_tokens"] = campaign.TOKEN_STOP
    with pytest.raises(ValueError, match="token stop"):
        reloaded.reserve(campaign.MODELS[0], "over token stop", False)


def passing_probe_receipt():
    outcome = {
        "kind": "text",
        "http_status": 200,
        "text": "OK",
        "identity": {"requested_model": campaign.MODELS[1]},
        "usage": {"prompt_tokens": 67, "completion_tokens": 5, "total_tokens": 72},
    }
    receipt = {
        "endpoint": "http://localhost:20128/v1",
        "output_limit": {"param": "max_completion_tokens", "value": 64},
        "prior_model_requests": 1,
        "model_requests_total": 3,
        "results": [
            {"admitted": True, "configured_model": campaign.MODELS[0], "outcome": outcome},
            {
                "admitted": True,
                "configured_model": campaign.MODELS[1],
                "schema_success": True,
                "profile_identity": {},
                "policy_digest": "fixture",
                "outcome": outcome,
            },
        ],
    }
    return receipt


@pytest.mark.parametrize("reuse_secondary", [False, True])
def test_entire_initial_driver_uses_existing_lifecycle_and_excludes_evaluation(
    db, seeded, tmp_path, monkeypatch, reuse_secondary
):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    cases = json.loads((campaign.FIXTURES / "cases.json").read_text())
    oracle = json.loads((campaign.FIXTURES / "oracle.json").read_text())["cases"]
    sent = []

    def answer(handler):
        model = json.loads(handler.server.requests[-1]["body"])["model"]
        index = sent.count(campaign.MODELS[0])
        sent.append(model)
        if model == campaign.MODELS[1]:
            respond(handler, text_payload("OK"))
            return
        expected = oracle[cases[index if index < len(cases) else 0]["id"]]
        healthy = index < len(cases) and cases[index]["id"] == "n-healthy"
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
            original(root, model, operational=False)
            path = root / "profile.json"
            value = json.loads(path.read_text())
            value["base_url"] = server.url + "/v1"
            value["mode"] = "operational" if kwargs.get("operational") else "evaluation_only"
            path.write_text(ModelProfile.model_validate(value).model_dump_json(by_alias=True))
            if kwargs.get("operational"):
                monkeypatch.setenv("DIAGNOSIS_QUALIFICATION_FILE", str(root / "qualification.json"))
                assert load().denied() is None
            return load().identity()

        monkeypatch.setattr(campaign, "configure", configured)
        monkeypatch.setattr(
            campaign, "OWNED_DATABASE", make_url(os.environ["TEST_DATABASE_URL"]).database
        )
        monkeypatch.setenv("E06_OWNED_LAB", "e06-r5")
        monkeypatch.setenv("DIAGNOSIS_LIVE_TOKEN", "owned-fixture-token")
        proof = tmp_path / "prior-probe.json"
        proof.write_text(json.dumps(passing_probe_receipt()))
        campaign.freeze(tmp_path, proof if reuse_secondary else None)
        expected_calls = 12 if reuse_secondary else 13
        monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(tmp_path)])
        campaign.main()
        ledger = json.loads((tmp_path / "ledger.json").read_text())
        assert len(sent) == len(ledger["calls"]) == expected_calls
        assert ledger["cases"]["n-required-gap"]["calls"] == 0
        assert all(case["excluded_from_operational_api"] for case in ledger["cases"].values())
        assert ledger["secondary_probe"]["receipt"]["schema_success"] is True
        assert ledger["secondary_probe"]["quality"] == "not_established"
        assert ledger["evaluation_finished"] is True
        with pytest.raises(ValueError, match="already started"):
            campaign.main()
        assert len(sent) == expected_calls
        continued = tmp_path / "continued"
        campaign.freeze(continued, previous=tmp_path / "ledger.json", continue_cases=True)
        monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(continued)])
        campaign.main()
        continuation = json.loads((continued / "ledger.json").read_text())
        assert len(sent) == expected_calls and continuation["calls"] == []
        assert continuation["cases"] == ledger["cases"]
        assert continuation["total_requests"] == ledger.get("prior_requests", 0) + expected_calls
        wrong = tmp_path / "changed-identity"
        campaign.freeze(wrong, previous=tmp_path / "ledger.json", continue_cases=True)
        wrong_ledger = json.loads((wrong / "ledger.json").read_text())
        wrong_ledger["continued_identity"]["prompt_version"] = "different"
        campaign.save(wrong / "ledger.json", wrong_ledger)
        monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(wrong)])
        with pytest.raises(ValueError, match="unchanged deployment identity"):
            campaign.main()
        assert len(sent) == expected_calls
        campaign.save(
            tmp_path / "qualification.json",
            {
                "schema_version": "DiagnosisQualification/v1",
                "cohort_id": "fixture-not-provider-quality",
                "evidence_reference": "controlled test",
                "identity": ledger["primary_identity"],
            },
        )
        monkeypatch.setattr(sys, "argv", ["campaign", "smoke", "--state-dir", str(tmp_path)])

        def failed_read(*_):
            raise RuntimeError("Controlled result read failure after paid send")

        monkeypatch.setattr(campaign, "case_result", failed_read)
        with pytest.raises(RuntimeError, match="Controlled result read failure"):
            campaign.main()
        assert len(sent) == expected_calls + 1
        ledger = json.loads((tmp_path / "ledger.json").read_text())
        assert ledger["smoke_started"] is True and "operational-smoke" not in ledger["cases"]
        assert ledger["calls"][-1]["case"] == "operational-smoke"
        assert ledger["calls"][-1]["outcome"]["kind"] == "text"
        with pytest.raises(ValueError, match="cannot repeat"):
            campaign.main()
        assert len(sent) == expected_calls + 1


def test_driver_denies_other_database_or_missing_owner_before_any_send(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(tmp_path)])
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/unrelated_test")
    monkeypatch.delenv("E06_OWNED_LAB", raising=False)
    with pytest.raises(ValueError, match="explicitly owned"):
        campaign.main()
    assert not (tmp_path / "campaign.lock").exists()


@pytest.mark.parametrize("reported_total", [None, 1])
def test_partial_or_inconsistent_usage_stops_before_another_http_send(tmp_path, reported_total):
    from diagnosis_fake import mock
    from diagnosis_fixtures import profile
    from cloudops.features.diagnosis.schemas import Usage
    import httpx

    campaign.freeze(tmp_path)
    budget = campaign.Budget(tmp_path / "ledger.json")
    client = ModelClient(
        profile(),
        transport=mock(lambda _: httpx.Response(200, json=text_payload())),
        environ={"DIAGNOSIS_TEST_TOKEN": "fixture"},
    )
    messages = [{"role": "user", "content": "Controlled probe"}]
    call = client.send(messages, before_send=lambda: None)
    budget.reserve(campaign.MODELS[0], "first", False)
    budget.settle(replace(call, usage=Usage(campaign.TOKEN_STOP, 10, reported_total)), "fixture")
    assert budget.data["observed_tokens"] == campaign.TOKEN_STOP + 10
    assert budget.data["calls"][0]["outcome"]["usage"]["total_tokens"] == reported_total
    client = ModelClient(
        profile(),
        transport=mock(
            lambda _: pytest.fail("The known usage stop must prevent another HTTP send")
        ),
        environ={"DIAGNOSIS_TEST_TOKEN": "fixture"},
    )
    with pytest.raises(ValueError, match="token stop"):
        client.send(messages, before_send=lambda: budget.reserve(campaign.MODELS[0], "next", False))
    assert len(budget.data["calls"]) == 1


def test_carryover_keeps_prior_spend_and_reuses_only_exact_secondary_proof(tmp_path):
    from copy import deepcopy

    receipt = passing_probe_receipt()
    source = tmp_path / "probes.json"
    source.write_text(json.dumps(receipt))
    campaign.freeze(tmp_path / "next", source)
    ledger = json.loads((tmp_path / "next/ledger.json").read_text())
    assert ledger["prior_requests"] == ledger["total_requests"] == 3
    assert ledger["observed_tokens"] == 144 and ledger["missing_usage_calls"] == 1
    assert ledger["secondary_probe"]["quality"] == "not_established"
    assert ledger["calls"] == []
    for invalid in ["count", "marker", "model", "destination"]:
        bad = deepcopy(receipt)
        if invalid == "count":
            bad["model_requests_total"] = 0
        if invalid == "marker":
            bad["results"][1]["outcome"]["text"] = "not OK"
        if invalid == "model":
            bad["results"][1]["outcome"]["identity"]["requested_model"] = "unselected"
        if invalid == "destination":
            bad["endpoint"] = "http://unselected.invalid/v1"
        source.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            campaign.carried_probe(source)


def test_changed_prompt_cohort_preserves_used_requests_tokens_and_repair(tmp_path):
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps(passing_probe_receipt()))
    initial = tmp_path / "initial"
    campaign.freeze(initial, probe)
    budget = campaign.Budget(initial / "ledger.json")
    budget.reserve(campaign.MODELS[0], "failed-initial", True)
    budget.data["calls"][-1]["outcome"] = {"kind": "text"}
    budget.data["observed_tokens"] += 100
    campaign.save(budget.path, budget.data)
    changed = tmp_path / "changed"
    campaign.freeze(changed, previous=budget.path)
    carried = campaign.Budget(changed / "ledger.json")
    assert carried.data["prior_requests"] == carried.data["total_requests"] == 4
    assert carried.data["observed_tokens"] == 244
    assert carried.data["missing_usage_calls"] == 1
    assert carried.data["secondary_probe"] == budget.data["secondary_probe"]
    with pytest.raises(ValueError, match="repair reserve"):
        carried.reserve(campaign.MODELS[0], "second-repair", True)
    budget.data["calls"][-1]["outcome"] = "unsettled"
    campaign.save(budget.path, budget.data)
    with pytest.raises(ValueError, match="unsettled"):
        campaign.freeze(tmp_path / "unsafe", previous=budget.path)
