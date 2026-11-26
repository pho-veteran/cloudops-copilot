"""Exercise the S6 handoff into the real S4 commands with controlled evidence."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cloudops.features.evaluation import cli, seal
from cloudops.features.evaluation.hashing import content_sha256
from eval_fixtures import context_for_case, receipt, write_corpus


CASE = "crashloop-positive"
OTHER_CASE = "oom-positive"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def campaign(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import e10_campaign

    return e10_campaign


@pytest.fixture
def state(tmp_path):
    fixtures = tmp_path / "fixtures"
    write_corpus(fixtures)
    # S6 captures this bundle after freezing the plan. Other fixture bundles stay sealed.
    for case in (CASE, OTHER_CASE):
        (fixtures / "bundles" / f"{case}.json").unlink()
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    seal.freeze(
        directory,
        fixtures / "cohort.json",
        fixtures / "labels.json",
        fixtures / "rubric.json",
        "a" * 40,
    )
    return directory, fixtures


@pytest.fixture
def trial(campaign):
    analysis_id = uuid4()
    context = context_for_case(CASE).model_copy(update={"analysis_identity": str(analysis_id)})
    binding = campaign.TrialBinding(
        case_id=CASE,
        trial_id=uuid4(),
        lab_run_id=uuid4(),
        lab_window_id=uuid4(),
        incident_id=context.incident_id,
        run_id=context.run_id,
        analysis_id=analysis_id,
        workload_uid=context.workload_uid,
        window_start=context.window.start,
        window_end=context.window.end,
        context_digest=context.digest,
        profile_digest=content_sha256(asdict(context.profile_identity)),
        source_binding_sha256="b" * 64,
    )
    return binding, context


def test_captured_bundle_is_accepted_by_both_s4_methods(campaign, state, trial):
    directory, fixtures = state
    binding, context = trial
    trial_path = directory / "trial.json"
    context_path = directory / "context.json"
    trial_path.write_text(binding.model_dump_json(), encoding="utf-8")
    context_path.write_text(context.model_dump_json(), encoding="utf-8")
    assert (
        campaign.main(
            [
                "capture",
                "--state-dir",
                str(directory),
                "--binding",
                str(trial_path),
                "--context",
                str(context_path),
            ]
        )
        == 0
    )

    repeated = campaign.capture(directory, binding, context)
    bundle = f"bundles/{CASE}.json"
    common = ["--state-dir", str(directory), "--case-id", CASE, "--bundle", bundle]
    assert cli.main(["compare", *common, "--output", f"outputs/rules/{CASE}.json"]) == 0
    investigator_output = Path("method-outputs") / "investigator" / f"{CASE}.json"
    (directory / investigator_output).parent.mkdir(parents=True)
    (directory / investigator_output).write_bytes((fixtures / investigator_output).read_bytes())
    for method in ("rules", "investigator"):
        output_args = (
            ["--method-output", str(investigator_output)] if method == "investigator" else []
        )
        receipt = f"receipts/{method}/{CASE}.json"
        assert (
            cli.main(
                [
                    "score",
                    *common,
                    "--method",
                    method,
                    "--disposition",
                    "scored",
                    "--output",
                    receipt,
                    *output_args,
                ]
            )
            == 0
        )
        result = json.loads((directory / receipt).read_bytes())
        assert result["disposition"] == "scored"
        assert result["bundle_sha256"] == repeated.bundle_sha256
        assert result["context_digest"] == context.digest

    outcome = campaign.record_outcome(directory, CASE, "completed", None)
    assert outcome.bundle_sha256 == repeated.bundle_sha256
    with pytest.raises(campaign.CampaignError, match="trial_closed"):
        campaign.capture(directory, binding, context)


@pytest.mark.parametrize(
    "wrong_binding",
    [
        {"run_id": uuid4()},
        {"analysis_id": uuid4()},
        {"workload_uid": "other-uid"},
        {"context_digest": "d" * 64},
        {"profile_digest": "e" * 64},
    ],
)
def test_mismatched_context_creates_no_bundle(campaign, state, trial, wrong_binding):
    directory, _ = state
    binding, context = trial
    with pytest.raises(campaign.CampaignError, match="context_binding_mismatch"):
        campaign.capture(directory, binding.model_copy(update=wrong_binding), context)
    assert not (directory / "campaign").exists()


def test_retry_recovers_interrupted_capture_and_refuses_rebinding(campaign, state, trial):
    directory, _ = state
    binding, context = trial
    original = campaign.capture(directory, binding, context)
    (directory / "campaign/captures" / f"{CASE}.json").unlink()
    with pytest.raises(campaign.CampaignError, match="trial_binding_mismatch"):
        campaign.capture(directory, binding.model_copy(update={"trial_id": uuid4()}), context)
    assert campaign.capture(directory, binding, context) == original
    with (directory / "bundles" / f"{CASE}.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(campaign.CampaignError, match="capture_binding_mismatch"):
        campaign.record_outcome(directory, CASE, "completed", None)


def test_trial_blocked_before_capture_is_retained_once(campaign, state, trial):
    directory, _ = state
    binding, context = trial
    with pytest.raises(campaign.CampaignError, match="requires_capture"):
        campaign.record_outcome(directory, CASE, "completed", None)
    record = campaign.record_outcome(directory, CASE, "blocked", "qualification_unavailable")
    assert record.bundle_sha256 is None
    assert record.reason == "qualification_unavailable"
    with pytest.raises(campaign.CampaignError, match="artifact_exists"):
        campaign.record_outcome(directory, CASE, "failed", "provider_unavailable")
    with pytest.raises(campaign.CampaignError, match="trial_closed"):
        campaign.capture(directory, binding, context)


def test_same_product_run_cannot_fill_a_second_case(campaign, state, trial):
    directory, _ = state
    binding, context = trial
    campaign.capture(directory, binding, context)
    repeated = binding.model_copy(
        update={
            "case_id": OTHER_CASE,
            "trial_id": uuid4(),
            "lab_run_id": uuid4(),
            "lab_window_id": uuid4(),
        }
    )
    with pytest.raises(campaign.CampaignError, match="trial_identity_reused"):
        campaign.capture(directory, repeated, context)
    assert not (directory / "bundles" / f"{OTHER_CASE}.json").exists()


@pytest.mark.parametrize(
    "stop", ["request", "tokens", "spend", "unsettled", "unknown_usage", "outbound_bound"]
)
def test_finite_worker_reserves_and_enforces_limits(campaign, state, trial, stop, monkeypatch):
    from e10_campaign_worker import Authorization, RECOVERY_CASES, RequestGuard
    from cloudops.features.diagnosis.schemas import CallKind, Usage

    directory, _ = state
    monkeypatch.setitem(RECOVERY_CASES, "controlled-test", (CASE,))
    binding, context = trial
    admission = campaign.TrialAdmission.model_validate(
        binding.model_dump(exclude={"context_digest"})
    )
    model = context.profile_identity.requested_model
    profile = SimpleNamespace(
        identity=lambda: context.profile_identity,
        model=model,
        output_limit=SimpleNamespace(value=8 if stop == "outbound_bound" else 2),
    )
    deployment = SimpleNamespace(
        profile=profile, identity=lambda: {"model": model}, denied=lambda: None
    )
    manifest = campaign.source_manifest(directory)
    campaign._write_once(directory / campaign.SOURCE_MANIFEST, manifest)
    authority = directory / "authority"
    authority.mkdir(mode=0o700)
    auth = Authorization(
        schema_version="E10S6Authorization/v1",
        approval_reference="controlled-test",
        source_revision="a" * 40,
        campaign_source_sha256=content_sha256(manifest),
        deployment_digest=content_sha256(deployment.identity()),
        profile_digest=binding.profile_digest,
        workload_uid=binding.workload_uid,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_requests=1 if stop == "request" else 2,
        max_requests_per_case=2,
        max_tokens=10 if stop == "tokens" else 20,
        token_reserve_per_request=10,
        max_spend_microusd=25 if stop == "spend" else 50,
        spend_reserve_microusd_per_request=25,
    )
    active = directory / "trial.json"
    active.write_text(
        json.dumps(
            {
                "scope": {"project": "cloudops-e10-s3-lab"},
                "scenario_kind": "recovery",
                "scenario_id": "controlled-test",
                "status": "observed",
                "trial_id": str(binding.trial_id),
                "run_id": str(binding.lab_run_id),
                "window_id": str(binding.lab_window_id),
                "target_uid": binding.workload_uid,
                "source_binding_sha256": binding.source_binding_sha256,
                "qualification": {"profile_digest": "f" * 64},
                "phases": {
                    "baseline": binding.window_start.isoformat(),
                    "inject": binding.window_start.isoformat(),
                    "detect": binding.window_end.isoformat(),
                },
                "load": {
                    "state": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "seconds": 200,
                },
            }
        ),
        encoding="utf-8",
    )
    guard = RequestGuard(directory, auth, admission, active, authority)
    if stop == "outbound_bound":
        with pytest.raises(campaign.CampaignError, match="request_bound_exceeds_token_reserve"):
            guard.before_request(context, deployment, "b" * 64, 5)
        assert not (authority / "provider").exists()
        return
    guard.before_request(context, deployment, "b" * 64, 5)
    assert (directory / f"bundles/{CASE}.json").is_file()
    if stop != "unsettled":
        guard.after_request(
            SimpleNamespace(
                kind=CallKind.TEXT,
                usage=None if stop == "unknown_usage" else Usage(4, 5, 9),
                served_model=model,
                http_status=200,
            ),
            deployment,
        )
    restart = RequestGuard(directory, auth, admission, active, authority)
    expected = {
        "unsettled": "unsettled_provider_request",
        "unknown_usage": "provider_usage_unavailable",
    }.get(stop, "provider_bounds_reached")
    with pytest.raises(campaign.CampaignError, match=expected):
        restart.before_request(context, deployment, "c" * 64, 5)
    assert len(list((authority / "provider").glob("*.request.json"))) == 1


def test_s6_scorecard_counts_missing_cases_in_all_fifteen_slots(campaign, tmp_path):
    from e10_campaign_plan import inputs
    from e10_campaign_report import summarize

    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    plans, labels, rubric = inputs("s6-controlled")
    assert len(plans.cases) == 25
    assert rubric.rubric_id == "e10-s6-release/v2"
    for case in plans.cases:
        expected = labels.cases[case.case_id].incident
        if case.treatment == "positive":
            assert expected.state == "expected" and expected.expected_disposition == "resolved"
        else:
            assert expected.state == "not_applicable" and expected.expected_disposition is None
    hostile = labels.cases["negative-untrusted-log"]
    clean = labels.cases[hostile.clean_control]
    assert hostile.allowed_candidates == clean.allowed_candidates == ["restore_deployment_template"]
    paths = [tmp_path / name for name in ("cohort.json", "labels.json", "rubric.json")]
    for path, value in zip(paths, (plans, labels, rubric), strict=True):
        path.write_text(value.model_dump_json(), encoding="utf-8")
    seal.freeze(directory, *paths, "a" * 40)
    slot = campaign.RECOVERY_CASES["recovery-oom-memory"][0]
    campaign.record_outcome(directory, slot, "blocked", "qualification_unavailable")
    report = summarize(directory)
    assert report["planned"] == 15
    assert report["pairs"]["missing"] == 15
    assert report["methods"]["investigator"]["rca_passes"] == 0
    assert report["methods"]["investigator"]["timing"]["analyze"]["missing"] == 15
    assert report["cases"][slot]["outcome"]["disposition"] == "blocked"
    assert not report["investigator_rca_target_met"]


def test_fixture_scores_never_count_as_live_recovery(campaign, tmp_path):
    from e10_campaign_plan import main
    from e10_campaign_report import summarize

    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    assert (
        main(
            ["--state-dir", str(directory), "--cohort-id", "control", "--source-revision", "a" * 40]
        )
        == 0
    )
    ledger = seal.load_ledger(directory)
    slots = {case for cases in campaign.RECOVERY_CASES.values() for case in cases}
    for entry in ledger.cases:
        if entry.case_id not in slots:
            continue
        result = receipt(entry.case_id, "investigator", "scored", passes=("cause_category",))
        raw = result.model_dump_json().encode("utf-8")
        path = directory / "receipts/investigator" / f"{entry.case_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entry.receipt_sha256["investigator"] = hashlib.sha256(raw).hexdigest()
    seal.write_ledger(directory, ledger)
    controls = directory / "campaign/controls"
    controls.mkdir(parents=True)
    for gate, evidence_class in (
        ("zero_uncontrolled_writes", "fixture"),
        ("no_paid_fallback", "live"),
    ):
        artifact = json.dumps(
            {"control": gate, "evidence_class": evidence_class, "outcome": "pass"}
        ).encode()
        (controls / f"{gate}.evidence.json").write_bytes(artifact)
        (controls / f"{gate}.json").write_text(
            json.dumps(
                {
                    "source_revision": "a" * 40,
                    "evidence_class": evidence_class,
                    "outcome": "pass",
                    "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    report = summarize(directory)
    assert report["qualifying_live_trials"] == 0
    assert report["methods"]["investigator"]["rca_passes"] == 0
    assert not report["investigator_rca_target_met"]
    assert report["controls"]["zero_uncontrolled_writes"]["outcome"] == "unverified"
    assert report["controls"]["no_paid_fallback"]["outcome"] == "unverified"


def test_campaign_manifest_detects_drift_in_transport_code(campaign, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "seal.json").write_bytes(b"controlled seal")
    transport = tmp_path / "services/backend/src/cloudops/features/diagnosis/transport.py"
    transport.parent.mkdir(parents=True)
    transport.write_bytes(b"original")
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.setattr(campaign, "S6_SOURCES", ())
    campaign._write_once(state / campaign.SOURCE_MANIFEST, campaign.source_manifest(state))
    campaign.verify_source_manifest(state)
    transport.write_bytes(b"drifted")
    with pytest.raises(campaign.CampaignError, match="campaign_source_drift"):
        campaign.verify_source_manifest(state)


def test_incomplete_recovery_counts_only_when_baseline_was_restored(
    campaign, trial, tmp_path, monkeypatch
):
    import release_scenarios

    binding, _ = trial
    scenario = "recovery-oom-memory"
    binding = binding.model_copy(update={"case_id": campaign.RECOVERY_CASES[scenario][0]})
    reset = {
        "scope": {"project": campaign.OWNED_PROJECT},
        "scenario_id": scenario,
        "trial_id": str(binding.trial_id),
        "run_id": str(binding.lab_run_id),
        "window_id": str(binding.lab_window_id),
        "target_uid": binding.workload_uid,
        "source_binding_sha256": binding.source_binding_sha256,
        "phases": {"reset": "2026-11-24T00:00:00Z"},
        "residual_eligible_actions": 0,
        "duplicate_targeted_loads": 0,
        "receipt_status": "incomplete",
        "evidence": {"lab_state": "retained_for_inspection"},
    }
    path = tmp_path / "reset.json"
    monkeypatch.setattr(release_scenarios, "validate_trial_receipt", lambda evidence: evidence)
    path.write_text(json.dumps(reset), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="reset_receipt_binding_mismatch"):
        campaign.bound_reset(path, binding)
    reset["evidence"]["lab_state"] = "restored_baseline"
    path.write_text(json.dumps(reset), encoding="utf-8")
    assert campaign.bound_reset(path, binding) == reset


@pytest.mark.parametrize(
    ("source_case", "family"),
    [
        ("c07", "pending_unschedulable"),
        ("c08", "pending_unschedulable"),
        ("c09", "image_pull"),
        ("c10", "image_pull"),
    ],
)
def test_controlled_rules_variants_stay_manual(source_case, family):
    from cloudops.features.evaluation.rules import compare

    result = compare(context_for_case(source_case, with_runbook=True))
    assert result.status == "supported"
    assert result.cause_category == family
    assert result.candidate_template_id is None
    assert result.supporting_evidence_ids and result.next_steps


@pytest.mark.skipif(os.name != "posix", reason="POSIX private directory permissions")
def test_capture_refuses_a_shared_state_directory(campaign, state, trial):
    directory, _ = state
    directory.chmod(0o755)
    with pytest.raises(campaign.CampaignError, match="private_state_directory_required"):
        campaign.capture(directory, *trial)
