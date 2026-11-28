"""Campaign controls use real PostgreSQL/worker and owned loopback HTTP, never paid CI."""

from dataclasses import replace
import json
import os
from pathlib import Path
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


def test_usage_stop_prevents_analysis_admission(tmp_path, monkeypatch):
    campaign.freeze(tmp_path)
    budget = campaign.Budget(tmp_path / "ledger.json")
    budget.data["observed_tokens"] = campaign.TOKEN_STOP
    monkeypatch.setattr(campaign, "seed_case", lambda *_: pytest.fail("No Run may be admitted"))
    with pytest.raises(ValueError, match="token stop"):
        campaign.execute_case(None, None, {"id": "uncalled"}, budget, {})
    assert budget.data["calls"] == [] and budget.data["cases"] == {}


CONFIG_GLOBALS = (
    "CONFIG",
    "MODELS",
    "OWNED_DATABASE",
    "OWNED_LAB",
    "MAX_REQUESTS",
    "TOKEN_STOP",
    "PRIMARY_LIMIT",
    "MAX_REPAIRS",
)


def configured_campaign(tmp_path, monkeypatch, *, base_url="http://127.0.0.1:18080/v1", **changes):
    for name in CONFIG_GLOBALS:
        monkeypatch.setattr(campaign, name, getattr(campaign, name))
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    profile = {
        "schema": "DiagnosisModelProfile/v1",
        "config_id": "fixture-luna",
        "config_version": "3",
        "mode": "disabled",
        "endpoint_label": "loopback",
        "base_url": base_url,
        "allow_insecure_loopback": True,
        "model": "fixture-luna",
        "reasoning_effort": "xhigh",
        "credential": {"kind": "env", "name": "DIAGNOSIS_LIVE_TOKEN"},
        "output_limit": {"param": "max_completion_tokens", "value": 16384},
        "limits": {"read_timeout_seconds": 30, "deadline_seconds": 30, "max_input_bytes": 32768},
    }
    (directory / "profile.json").write_text(json.dumps(profile))
    (directory / "prior.json").write_text("{}")
    config = {
        "schema": "E06CampaignConfig/v1",
        "cohort": "fixture-luna-cohort",
        "profile": "profile.json",
        "authorization_id": "owner-delegated-fixture",
        "budget_policy_id": "fixture-24-requests",
        "owned_database": make_url(os.environ["TEST_DATABASE_URL"]).database,
        "owned_lab": "fixture-luna-lab",
        "max_requests": 24,
        "max_repairs": 4,
        "observed_token_stop": 400000,
        "max_reserved_usd": 1.0,
        "input_usd_per_mtok": 0.10,
        "output_usd_per_mtok": 0.50,
        "rates_date": "2026-11-24",
        "prior_receipts": ["prior.json"],
        **changes,
    }
    path = directory / "campaign.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.parametrize("input_bytes", [32768, 65536, 131072])
def test_campaign_config_binds_one_primary_and_its_bounds(tmp_path, monkeypatch, input_bytes):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    path = configured_campaign(tmp_path, monkeypatch)
    profile_path = path.parent / "profile.json"
    profile = json.loads(profile_path.read_text())
    profile["limits"]["max_input_bytes"] = input_bytes
    profile_path.write_text(json.dumps(profile))
    config = campaign.load_config(path)
    assert campaign.MODELS == ("fixture-luna",) and campaign.MAX_REQUESTS == 24
    assert campaign.MAX_REPAIRS == 4 and campaign.TOKEN_STOP == 400000
    assert config["per_request_reserve_usd"] == pytest.approx(
        (input_bytes * 0.10 + 16384 * 0.50) / 1_000_000
    )
    state = tmp_path / "state"
    campaign.freeze(state)
    ledger = json.loads((state / "ledger.json").read_text())
    assert ledger["cohort"] == "fixture-luna-cohort" and ledger["models"] == ["fixture-luna"]
    assert ledger["endpoint"] == "http://127.0.0.1:18080/v1"
    assert ledger["campaign_config_sha256"] == config["config_sha256"]
    assert ledger["prior_receipts"][0]["path"] == "prior.json"
    assert ledger["secondary_probe"]["state"] == "not_configured"
    assert ledger["billed_usd"] is None
    campaign.configure(state, "fixture-luna")
    deployment = load()
    assert deployment.policy.max_message_bytes == input_bytes
    assert deployment.profile.limits.max_input_bytes == input_bytes
    budget = campaign.Budget(state / "ledger.json")
    for index in range(4):
        budget.reserve("fixture-luna", f"repair-{index}", True)
    with pytest.raises(ValueError, match="repair"):
        budget.reserve("fixture-luna", "fifth-repair", True)
    with pytest.raises(ValueError, match="Unapproved"):
        budget.reserve("gpt-6.1-sol", "legacy", False)
    while len(budget.data["calls"]) < 24:
        budget.reserve("fixture-luna", "fill", False)
    with pytest.raises(ValueError, match="budget exhausted"):
        budget.reserve("fixture-luna", "over", False)
    assert budget.data["reserved_usd"] == pytest.approx(24 * config["per_request_reserve_usd"])


def test_campaign_config_reserved_spend_ceiling_stops_before_send(tmp_path, monkeypatch):
    path = configured_campaign(tmp_path, monkeypatch, max_reserved_usd=0.02)
    config = campaign.load_config(path)
    campaign.freeze(tmp_path / "state")
    budget = campaign.Budget(tmp_path / "state/ledger.json")
    allowed = int(0.02 // config["per_request_reserve_usd"])
    for _ in range(allowed):
        budget.reserve("fixture-luna", "case", False)
    with pytest.raises(ValueError, match="spend ceiling"):
        budget.reserve("fixture-luna", "over", False)
    assert len(budget.data["calls"]) == allowed


def test_unpriced_campaign_keeps_cost_unknown_and_enforces_request_and_token_bounds(
    tmp_path, monkeypatch
):
    path = configured_campaign(
        tmp_path, monkeypatch, pricing_available=False, max_requests=1, observed_token_stop=100
    )
    campaign.load_config(path)
    state = tmp_path / "state"
    campaign.freeze(state)
    budget = campaign.Budget(state / "ledger.json")
    assert budget.data["rates"] is None
    assert budget.data["estimated_usd"] is None
    assert "not prices or a spend cap" in budget.data["allowance"]
    budget.reserve("fixture-luna", "case", False)
    from diagnosis_fixtures import profile
    from diagnosis_fake import mock
    import httpx

    client = ModelClient(
        profile(),
        transport=mock(lambda _: httpx.Response(200, json=text_payload("OK"))),
        environ={"DIAGNOSIS_TEST_TOKEN": "fixture"},
    )
    call = client.send(
        [{"role": "user", "content": "Fixed controlled probe"}], before_send=lambda: None
    )
    budget.settle(call, "fixture")
    reloaded = campaign.Budget(state / "ledger.json")
    assert reloaded.data["estimated_usd"] is None
    assert reloaded.data["billed_usd"] is None
    assert reloaded.data["observed_tokens"] > 0
    assert reloaded.data["reserved_usd"] > 0
    with pytest.raises(ValueError, match="budget exhausted"):
        reloaded.reserve("fixture-luna", "over-request", False)
    reloaded.data["calls"] = []
    reloaded.data["observed_tokens"] = campaign.TOKEN_STOP
    with pytest.raises(ValueError, match="token stop"):
        reloaded.reserve("fixture-luna", "over-token", False)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"owned_database": "cloudops"}, "_test"),
        ({"schema": "Other/v1"}, "Unsupported"),
        ({"unexpected": True}, "Unsupported"),
        ({"max_requests": -1}, "non-negative integer"),
        ({"max_reserved_usd": float("inf")}, "bounded"),
        ({"pricing_available": "false"}, "boolean"),
    ],
)
def test_campaign_config_rejects_unsafe_values(tmp_path, monkeypatch, changes, message):
    path = configured_campaign(tmp_path, monkeypatch, **changes)
    with pytest.raises(ValueError, match=message):
        campaign.load_config(path)


def test_campaign_config_requires_the_worker_credential_name(tmp_path, monkeypatch):
    path = configured_campaign(tmp_path, monkeypatch)
    profile = json.loads((path.parent / "profile.json").read_text())
    profile["credential"]["name"] = "OTHER_TOKEN"
    (path.parent / "profile.json").write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="DIAGNOSIS_LIVE_TOKEN"):
        campaign.load_config(path)


def test_frozen_campaign_refuses_other_config_or_changed_profile(tmp_path, monkeypatch):
    path = configured_campaign(tmp_path, monkeypatch)
    campaign.load_config(path)
    state = tmp_path / "state"
    campaign.freeze(state)
    monkeypatch.setattr(sys, "argv", ["campaign", "run", "--state-dir", str(state)])
    with pytest.raises(ValueError, match="exact campaign config"):
        campaign.main()
    assert campaign.CONFIG is None and campaign.MODELS == campaign._DEFAULTS["MODELS"]
    profile = json.loads((path.parent / "profile.json").read_text())
    profile["reasoning_effort"] = "high"
    (path.parent / "profile.json").write_text(json.dumps(profile))
    monkeypatch.setattr(
        sys,
        "argv",
        ["campaign", "run", "--state-dir", str(state), "--campaign-config", str(path)],
    )
    with pytest.raises(ValueError, match="profile changed"):
        campaign.main()
    assert not (state / "campaign.lock").exists()


@pytest.mark.parametrize("capture,input_bytes", [(False, 32768), (True, 65536)])
def test_configured_campaign_runs_the_full_cohort_through_the_lifecycle(
    db, seeded, tmp_path, monkeypatch, capture, input_bytes
):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    cases = json.loads((campaign.FIXTURES / "cases.json").read_text())
    oracle = json.loads((campaign.FIXTURES / "oracle.json").read_text())["cases"]
    sent = []

    repaired = {cases[0]["id"], cases[1]["id"]}
    # Cases run in order and a repair follows its failed first call immediately.
    schedule = [
        (case, kind)
        for case in cases
        for kind in (("invalid", "valid") if case["id"] in repaired else ("valid",))
    ]

    def answer(handler):
        body = json.loads(handler.server.requests[-1]["body"])
        sent.append(body)
        case, kind = schedule[len(sent) - 1]
        if kind == "invalid":
            payload = text_payload("not json")
            payload["model"] = "fixture-luna"
            payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            respond(handler, payload)
            return
        expected = oracle[case["id"]]
        healthy = case["id"] == "n-healthy"
        payload = text_payload(
            _raw(
                status="insufficient_evidence" if healthy else "supported",
                cause_category=expected["category"],
                supporting_evidence_ids=[] if healthy else ["E001"],
                candidate_template_id=next(iter(expected["allowed_candidates"]), None),
            )
        )
        payload["model"] = "fixture-luna"
        payload["usage"] = {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000}
        respond(handler, payload)

    with serve(answer) as server:
        extra = {"capture_contexts": True} if capture else {}
        path = configured_campaign(tmp_path, monkeypatch, base_url=server.url + "/v1", **extra)
        profile_path = path.parent / "profile.json"
        profile = json.loads(profile_path.read_text())
        profile["limits"]["max_input_bytes"] = input_bytes
        profile_path.write_text(json.dumps(profile))
        monkeypatch.setenv("E06_OWNED_LAB", "fixture-luna-lab")
        monkeypatch.setenv("DIAGNOSIS_LIVE_TOKEN", "owned-fixture-token")
        state = tmp_path / "state"
        args = ["--state-dir", str(state), "--campaign-config", str(path)]
        monkeypatch.setattr(sys, "argv", ["campaign", "freeze", *args])
        campaign.main()
        monkeypatch.setattr(sys, "argv", ["campaign", "run", *args])
        campaign.main()
        ledger = json.loads((state / "ledger.json").read_text())
        assert len(sent) == len(ledger["calls"]) == len(cases) + 2
        assert ledger["repairs"] == 2
        assert all(ledger["cases"][key]["repairs"] == 1 for key in repaired)
        assert all(body["model"] == "fixture-luna" for body in sent)
        assert all(body["reasoning_effort"] == "xhigh" for body in sent)
        assert all(body["max_completion_tokens"] == 16384 for body in sent)
        assert ledger["cases"]["n-required-gap"]["calls"] == 0
        assert all(case["excluded_from_operational_api"] for case in ledger["cases"].values())
        assert ledger["secondary_probe"]["state"] == "not_configured"
        assert ledger["evaluation_finished"] is True
        assert ledger["estimated_usd"] == pytest.approx(
            len(cases) * (1000 * 0.10 + 2000 * 0.50) / 1_000_000
            + 2 * (10 * 0.10 + 10 * 0.50) / 1_000_000
        )
        assert ledger["primary_identity"]["model"] == "fixture-luna"
        assert load().policy.max_message_bytes == input_bytes
        contexts = state / "contexts"
        if not capture:
            assert not contexts.exists()
            return
        from cloudops.features.diagnosis.context import PreparedContext

        assert sorted(path.stem for path in contexts.iterdir()) == sorted(
            case["id"] for case in cases
        )
        assert contexts.stat().st_mode & 0o777 == 0o700
        for case in cases:
            entry = ledger["cases"][case["id"]]
            file = contexts / f"{case['id']}.json"
            assert file.stat().st_mode & 0o777 == 0o600
            prepared = PreparedContext.model_validate_json(file.read_bytes())
            # A repaired case keeps the single snapshot both of its sends used.
            assert prepared.digest == entry["result"]["context_digest"]
            assert prepared.analysis_identity == entry["analysis_id"]


def test_configured_run_refuses_backend_source_changed_after_freezing(tmp_path, monkeypatch):
    path = configured_campaign(tmp_path, monkeypatch)
    campaign.load_config(path)
    state = tmp_path / "state"
    campaign.freeze(state)
    ledger = json.loads((state / "ledger.json").read_text())
    assert ledger["source_tree_sha256"] == campaign.source_tree_digest()
    monkeypatch.setenv("E06_OWNED_LAB", "fixture-luna-lab")
    monkeypatch.setenv("DIAGNOSIS_LIVE_TOKEN", "owned-fixture-token")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr(campaign, "source_tree_digest", lambda: "0" * 64)
    arguments = ["--state-dir", str(state), "--campaign-config", str(path)]
    monkeypatch.setattr(sys, "argv", ["campaign", "run", *arguments])
    with pytest.raises(ValueError, match="Frozen campaign source changed"):
        campaign.main()
    assert not (state / "campaign.lock").exists()
    assert json.loads((state / "ledger.json").read_text())["calls"] == []


def test_captured_context_is_write_once_per_case(tmp_path):
    class Prepared:
        def __init__(self, text):
            self.text = text

        def model_dump_json(self):
            return self.text

    campaign.capture_context(tmp_path, "c01", Prepared('{"a":1}'))
    campaign.capture_context(tmp_path, "c01", Prepared('{"a":1}'))
    with pytest.raises(ValueError, match="changed within one case"):
        campaign.capture_context(tmp_path, "c01", Prepared('{"a":2}'))
    assert (tmp_path / "contexts/c01.json").read_text() == '{"a":1}\n'


@pytest.mark.parametrize("value", ["yes", 1])
def test_campaign_config_capture_flag_must_be_boolean(tmp_path, monkeypatch, value):
    path = configured_campaign(tmp_path, monkeypatch, capture_contexts=value)
    with pytest.raises(ValueError, match="boolean"):
        campaign.load_config(path)


def test_profile_replaced_after_loading_is_never_installed(tmp_path, monkeypatch):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    path = configured_campaign(tmp_path, monkeypatch)
    campaign.load_config(path)
    swapped = json.loads((path.parent / "profile.json").read_text())
    swapped["output_limit"]["value"] = 300000
    swapped["base_url"] = "http://127.0.0.1:18081/v1"
    (path.parent / "profile.json").write_text(json.dumps(swapped))
    state = tmp_path / "state"
    state.mkdir()
    campaign.configure(state, "fixture-luna")
    installed = json.loads((state / "profile.json").read_text())
    assert installed["output_limit"]["value"] == 16384
    assert installed["base_url"] == "http://127.0.0.1:18080/v1"
    assert installed["mode"] == "evaluation_only"


def test_relative_campaign_ca_stays_bound_to_the_trusted_profile_directory(tmp_path, monkeypatch):
    import cloudops.features.diagnosis.profile as profile_module

    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    seen = []

    def context(*, cafile=None, **kwargs):
        seen.append(Path(cafile))
        return profile_module.ssl.SSLContext(profile_module.ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(profile_module.ssl, "create_default_context", context)
    path = configured_campaign(tmp_path, monkeypatch)
    (path.parent / "root-ca.pem").write_text("placeholder")
    profile = json.loads((path.parent / "profile.json").read_text())
    profile["tls"] = {"ca_file": "root-ca.pem"}
    (path.parent / "profile.json").write_text(json.dumps(profile))
    campaign.load_config(path)
    state = tmp_path / "state"
    state.mkdir()
    campaign.configure(state, "fixture-luna")
    installed = json.loads((state / "profile.json").read_text())
    expected = (path.parent / "root-ca.pem").resolve()
    assert Path(installed["tls"]["ca_file"]) == expected
    assert load().problem is None
    assert seen and all(item.resolve() == expected for item in seen)


def test_campaign_config_can_bind_the_operational_egress_and_context_profile(tmp_path, monkeypatch):
    for key in ("DIAGNOSIS_PROFILE_FILE", "DIAGNOSIS_EGRESS_FILE", "DIAGNOSIS_QUALIFICATION_FILE"):
        monkeypatch.setenv(key, "owned-test-placeholder")
    egress = {
        "schema": "EgressPolicy/v1",
        "policy_id": "operational-fixture",
        "policy_version": "2",
        "destination": {
            "config_id": "fixture-luna",
            "config_version": "3",
            "endpoint_label": "loopback",
        },
        "allowed_source_classes": ["operator_request", "kubernetes_state", "runbook"],
        "max_message_bytes": 65536,
        "max_target_bytes": 8192,
        "redaction_policy_version": "redaction/v1",
        "deny_patterns": [],
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "egress.json").write_text(json.dumps(egress))
    path = configured_campaign(tmp_path, monkeypatch, egress="egress.json")
    config = campaign.load_config(path)
    assert config["egress_policy"].policy_id == "operational-fixture"
    monkeypatch.setenv("DIAGNOSIS_CONTEXT_PROFILE", "engine")
    state = tmp_path / "state"
    campaign.freeze(state)
    identity = campaign.configure(state, "fixture-luna")
    assert load().policy.policy_id == "operational-fixture"
    assert identity == load().identity()
    assert identity["context_profile"] == "engine"
    wrong = dict(egress, destination={**egress["destination"], "config_id": "other"})
    (tmp_path / "config" / "egress.json").write_text(json.dumps(wrong))
    with pytest.raises(ValueError, match="egress"):
        campaign.load_config(path)
