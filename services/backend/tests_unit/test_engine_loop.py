"""Engine-loop tooling: raw bundles, offline loader, cached replay and scoring."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from diagnosis_fixtures import profile as fixture_profile
from eval_fixtures import _policy as fixture_policy
from eval_fixtures import context_for_case

from cloudops.features.diagnosis.context import assemble
from cloudops.features.evaluation.rules import compare

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_IDS = [
    "c01",
    "c02",
    "c03",
    "c04",
    "c05",
    "c06",
    "c07",
    "c08",
    "c09",
    "c10",
    "n-healthy",
    "n-injection",
]


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from engine_loop import bundle, dataset, gateway, metrics, pipeline

    return type(
        "Loop",
        (),
        {
            "bundle": bundle,
            "dataset": dataset,
            "gateway": gateway,
            "metrics": metrics,
            "pipeline": pipeline,
        },
    )


def _bundles(loop):
    return {case["id"]: loop.bundle.fixture_bundle(case) for case in loop.bundle.fixture_cases()}


def test_fixture_bundles_cover_the_twelve_e06_cases_and_detect_tampering(loop):
    bundles = _bundles(loop)
    assert sorted(bundles) == sorted(FIXTURE_IDS)
    value = bundles["c03"]
    loop.bundle.verify(value)
    tampered = value.model_copy(update={"case_id": "c04"})
    with pytest.raises(ValueError):
        loop.bundle.verify(tampered)
    assert all(item.payload is not None for item in value.slots)


@pytest.mark.parametrize("case_id", FIXTURE_IDS)
def test_offline_loader_reproduces_the_fixture_context_exactly(loop, case_id):
    value = _bundles(loop)[case_id]
    rebuilt = assemble(
        loop.bundle.materials(value),
        loop.bundle.scope(value),
        None,
        fixture_policy(),
        fixture_profile(),
        "e10-s4-fixture",
    )
    expected = context_for_case(case_id)
    assert rebuilt.digest == expected.digest
    assert rebuilt.messages == expected.messages


def test_lab_bundles_fill_every_catalog_slot_like_load_materials(loop):
    value = _bundles(loop)["c07"]
    complete = value.model_copy(update={"complete_catalog": True})
    materials = loop.bundle.materials(complete)
    assert len(materials) == 12
    assert {item.slot for item in materials if item.coverage_status == "missing"} >= {
        "prometheus.request_rate",
        "kubernetes.logs.current",
    }


def test_split_assignment_is_deterministic_and_test_is_sealed(loop, tmp_path):
    assert loop.dataset.split_for("oom-clean-001") == loop.dataset.split_for("oom-clean-001")
    splits = {loop.dataset.split_for(f"case-{index:03d}") for index in range(40)}
    assert splits == {"dev", "test"}
    snapshot = tmp_path / "snap"
    loop.dataset.fixture_snapshot(snapshot)
    assert len(loop.dataset.load(snapshot, "smoke")) == 12
    with pytest.raises(loop.dataset.SealedSplitError):
        loop.dataset.load(snapshot, "test")
    with pytest.raises(FileExistsError):
        loop.dataset.fixture_snapshot(snapshot)


def _rules_transport(loop, seen: list, *, finish="stop", status=200):
    bundles = _bundles(loop)
    model_profile = loop.gateway.profile()
    egress = loop.pipeline.policy()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert body["messages"][0]["role"] == "system"
        assert body["max_tokens"] == 8000
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "slow down"}})
        user = json.loads(body["messages"][1]["content"])
        case_id = next(
            key
            for key, value in bundles.items()
            if str(value.scope.run_id) == user["scope"]["run_id"]
        )
        context = loop.pipeline.product_context(bundles[case_id], model_profile, egress)
        advice = compare(context).model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "id": "fake",
                "object": "chat.completion",
                "model": "fake-served",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(advice)},
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 40},
                },
            },
        )

    return lambda: httpx.MockTransport(handler)


def _gateway(loop, tmp_path, transport):
    return loop.gateway.Gateway(
        cache_dir=tmp_path / "cache",
        model_profile=loop.gateway.profile(),
        environ={"ENGINE_MODEL_API_KEY": "test-only"},
        transport_factory=transport,
    )


def _replay(loop, gateway, case_ids, config="B1", repair=False):
    bundles = _bundles(loop)
    runner, _ = loop.pipeline.CONFIGS[config]
    settings = loop.pipeline.Settings(repair=repair)

    async def run():
        return [
            await runner(bundles[case_id], settings=settings, gateway=gateway)
            for case_id in case_ids
        ]

    return asyncio.run(run())


def _labels(loop):
    oracle = loop.bundle.fixture_oracle()
    return {case_id: loop.dataset.fixture_label(case_id, oracle) for case_id in FIXTURE_IDS}


def test_b1_replay_with_a_fake_transport_scores_and_caches(loop, tmp_path):
    seen: list = []
    gateway = _gateway(loop, tmp_path, _rules_transport(loop, seen))
    records = _replay(loop, gateway, FIXTURE_IDS)
    assert len(seen) == 12
    labels = _labels(loop)
    scores = [
        loop.metrics.case_score(record | {"bundle_slots": {}}, labels[case_id])
        for case_id, record in zip(FIXTURE_IDS, records, strict=True)
    ]
    card = loop.metrics.scorecard(scores)
    assert card["family_accuracy"]["value"] == 1.0
    assert card["hallucinated_aliases"] == 0
    assert card["first_attempt_valid"]["value"] == 1.0
    assert card["usage_spent_this_run"]["reasoning_tokens"] == 12 * 40
    # A second replay of unchanged requests is served from the digest cache.
    again = _gateway(loop, tmp_path, _rules_transport(loop, seen))
    repeated = _replay(loop, again, FIXTURE_IDS)
    assert len(seen) == 12 and again.sent == 0 and again.cache_hits == 12
    assert [item["final"] for item in repeated] == [item["final"] for item in records]


def test_b0_matches_the_rules_comparator_on_the_fixture_contexts(loop, tmp_path):
    records = _replay(loop, None, FIXTURE_IDS, config="B0")
    for case_id, record in zip(FIXTURE_IDS, records, strict=True):
        expected = compare(context_for_case(case_id))
        assert record["final"]["cause_category"] == expected.cause_category
        assert record["final"]["status"] == expected.status


def test_truncated_output_is_a_schema_failure_and_is_not_retried(loop, tmp_path):
    seen: list = []
    gateway = _gateway(loop, tmp_path, _rules_transport(loop, seen, finish="length"))
    record = _replay(loop, gateway, ["c01"], repair=True)[0]
    assert len(seen) == 1
    score = loop.metrics.case_score(record, _labels(loop)["c01"])
    assert score["pred"] == "invalid" and score["tag"] == "SCHEMA_FAIL"


def test_rate_limit_stops_the_loop(loop, tmp_path):
    gateway = _gateway(loop, tmp_path, _rules_transport(loop, [], status=429))
    with pytest.raises(loop.gateway.PolicyStop):
        _replay(loop, gateway, ["c01", "c03"])
    assert gateway.stopped and gateway.sent == 1


def test_only_the_allowed_model_and_route_are_accepted(loop):
    allowed = {
        "ENGINE_MODEL_NAME": "cmd/deepseek/deepseek-v4.1-flash",
        "ENGINE_MODEL_BASE_URL": "http://localhost:20128/v1",
        "ENGINE_MODEL_API_KEY": "x",
    }
    loop.gateway.check_environment(allowed)
    for key, value in (
        ("ENGINE_MODEL_NAME", "other-model"),
        ("ENGINE_MODEL_BASE_URL", "https://api.example.invalid/v1"),
    ):
        with pytest.raises(loop.gateway.PolicyStop):
            loop.gateway.check_environment({**allowed, key: value})


def test_paired_comparison_statistics(loop):
    assert loop.metrics.mcnemar(0, 0) == 1.0
    assert loop.metrics.mcnemar(0, 6) == pytest.approx(0.03125)
    assert loop.metrics.wilson(5, 15) == [0.1518, 0.5829]


def _with_payload(value, slot: str, payload: dict, **update):
    slots = [
        item.model_copy(update={"payload": payload}) if item.slot == slot else item
        for item in value.slots
    ]
    return value.model_copy(update={"slots": slots, **update})


def _warnings(*reasons: str) -> dict:
    return {
        "events": [
            {"name": f"event-{index}", "type": "Warning", "reason": reason, "message": "m"}
            for index, reason in enumerate(reasons)
        ]
    }


def test_signature_rule_maps_warning_reasons_and_respects_eligibility(loop):
    from engine_loop import audit

    bundles = _bundles(loop)
    pending = _with_payload(bundles["c07"], "kubernetes.events", _warnings("FailedScheduling"))
    pull = _with_payload(bundles["c07"], "kubernetes.events", _warnings("Failed", "BackOff"))
    crash = _with_payload(bundles["c01"], "kubernetes.events", _warnings("BackOff", "Unhealthy"))
    # c07 has no log slots, so a crash signature is ineligible there and T0 abstains.
    no_logs = _with_payload(bundles["c07"], "kubernetes.events", _warnings("BackOff", "Unhealthy"))
    assert audit.t0(audit.features(pending)) == "pending_unschedulable"
    assert audit.t0(audit.features(pull)) == "image_pull"
    assert audit.t0(audit.features(crash)) == "crashloop_startup"
    assert audit.t0(audit.features(no_logs)) == "unknown"
    oom = dict(audit.features(crash), oomkilled=True)
    assert audit.t0(oom) == "oom_restarts"
    quiet = dict(audit.features(crash), warning_reasons=[], max_p95=1.0)
    assert audit.t0(quiet) == "unknown"  # traffic slots are absent in c01
    assert audit.t0(dict(quiet, eligible=["traffic_latency_errors"])) == "traffic_latency_errors"


def test_leak_scan_flags_scenario_words_but_not_kubernetes_vocabulary(loop):
    from engine_loop import audit

    value = _with_payload(
        _bundles(loop)["c07"],
        "kubernetes.events",
        {
            "events": [
                {"name": "engine-noise-1", "reason": "OOMKilled", "message": "Pending"},
                {"name": "e2", "source": "default-scheduler", "message": "Unavailable"},
            ]
        },
        case_id="oom-clean-a01",
    )
    found = {(item["where"].split(".")[0], item["token"]) for item in audit.leaks(value)}
    assert ("case_id", "oom") in found and ("case_id", "clean") in found
    content = {token for where, token in found if where == "kubernetes"}
    assert content == {"engine-noise", "noise"}
    assert audit.leaks(_bundles(loop)["c07"]) == []


def test_audit_partitions_cases_into_strata_with_shortcut_baselines(loop, tmp_path):
    from engine_loop import audit

    snapshot = tmp_path / "snap"
    loop.dataset.fixture_snapshot(snapshot)
    cases = loop.dataset.load(snapshot, "smoke")
    result = audit.audit(cases, loop.dataset.manifest(snapshot)["cases"])
    assert result["n_cases"] == result["n_trials"] == 12
    assert result["p0"]["family_accuracy"]["n"] == 12
    rows = result["cases"]
    assert all((row["stratum"] == "easy") == (row["t0"] == row["gold"]) for row in rows)
    easy = sum(1 for row in rows if row["stratum"] == "easy")
    assert result["t0"]["family_accuracy"]["k"] == easy
    assert result["strata"]["cases"].get("hard", 0) == 12 - easy
    assert result["leaks"]["cases_with_content_leaks"] == 0


def _v2_trials():
    trials = []
    for cell in ("oom-strong", "pending-cpu", "healthy"):
        trials += [{"seed": f"{cell}/{index}", "cell": cell} for index in range(10)]
    trials += [
        {"seed": f"pair/{index}/{side}", "cell": side, "contrast_group": f"pair-{index}"}
        for index in range(6)
        for side in ("liveness-137", "oom-near")
    ]
    trials += [{"seed": f"kv/{index}", "cell": "oom-strong", "holdout": "kv"} for index in range(4)]
    return trials


def test_v2_split_keeps_pairs_together_holds_out_and_reserves_dev_check(loop, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from engine_loop import splits

    trials = _v2_trials()
    first = splits.assign(trials)
    assert first == splits.assign(list(reversed(trials)))
    for index in range(6):
        sides = {first[f"pair/{index}/{side}"]["split"] for side in ("liveness-137", "oom-near")}
        assert len(sides) == 1
    assert {first[f"kv/{index}"]["split"] for index in range(4)} == {"test"}
    for cell in ("oom-strong", "pending-cpu", "healthy"):
        assert sum(first[f"{cell}/{i}"]["split"] == "test" for i in range(10)) == 5
    dev = [seed for seed, item in first.items() if item["split"] == "dev"]
    check = [seed for seed, item in first.items() if item["dev_check"]]
    assert set(check) <= set(dev)
    assert len(check) >= 0.2 * len(dev)
    assert {seed.split("/")[0] for seed in check} >= {"oom-strong", "pending-cpu", "healthy"}
    assert splits.opaque_id("oom-strong/1").startswith("c-")
    assert len(splits.opaque_id("oom-strong/1")) == 14
    with pytest.raises(ValueError):
        splits.assign([{"seed": "a", "cell": "x"}, {"seed": "a", "cell": "y"}])


def test_v2_snapshot_seals_test_meta_and_separates_dev_check(loop, tmp_path):
    bundles = _bundles(loop)
    labels = _labels(loop)
    cases = []
    for index, case_id in enumerate(FIXTURE_IDS):
        split = "test" if index % 3 == 0 else "dev"
        meta = {"trial_id": case_id, "dev_check": split == "dev" and index % 3 == 1}
        cases.append((bundles[case_id], labels[case_id], split, meta))
    snapshot = tmp_path / "v2"
    loop.dataset.write_snapshot(
        snapshot,
        snapshot_id="v2",
        cases=cases,
        description="test",
        pilot=[],
        version=loop.dataset.MANIFEST_VERSION_V2,
    )
    manifest = loop.dataset.manifest(snapshot)
    assert all(
        set(entry) == {"split", "bundle_sha256", "manifest_sha256"}
        for entry in manifest["cases"].values()
        if entry["split"] == "test"
    )
    with pytest.raises(loop.dataset.SealedSplitError):
        loop.dataset.case_meta(snapshot, "test")
    sealed = loop.dataset.case_meta(snapshot, "test", milestone=True)
    assert sorted(sealed) == sorted(c[0].case_id for c in cases if c[2] == "test")
    work = {v.case_id for v, _ in loop.dataset.load(snapshot, "dev", subset="work")}
    check = {v.case_id for v, _ in loop.dataset.load(snapshot, "dev", subset="check")}
    every = {v.case_id for v, _ in loop.dataset.load(snapshot, "dev")}
    assert work and check and not work & check and work | check == every


def _v2_score(case_id, gold, pred, *, trial=None, **extra):
    return {
        "case_id": case_id,
        "gold": gold,
        "pred": pred,
        "correct": gold == pred,
        "passed": gold == pred,
        "tag": None
        if gold == pred
        else ("ABSTAIN_WRONG" if "unknown" in (gold, pred) else "WRONG_FAMILY"),
        "trial_id": trial or case_id,
        **extra,
    }


def test_v2_tags_pairs_and_cluster_bootstrap(loop):
    labels = _labels(loop)
    record = {
        "context": {
            "aliases": {"E001": "kubernetes.state", "E002": "kubernetes.events"},
            "alias_refs": {
                "E001": ["kubernetes.state", "field", "k1"],
                "E002": ["kubernetes.events", "event", "k2"],
            },
            "sent_slots": ["kubernetes.events", "kubernetes.state"],
            "outcome": "prepared",
        },
        "final": {
            "status": "supported",
            "cause_category": "oom_restarts",
            "supporting_evidence_ids": ["E001", "E002"],
        },
        "attempts": [],
    }
    oom = labels["c03"]
    herring = {
        "trial_id": "t1",
        "distractor_sources": [{"slot": "kubernetes.state", "key": "k1"}],
    }
    score = loop.metrics.case_score(record, oom, herring)
    assert score["distractor_cited"] == 1 and not score["citation_ok"]
    assert score["tag"] == "DISTRACTOR_CITED" and score["trial_id"] == "t1"
    healthy = labels["n-healthy"]
    flip = loop.metrics.case_score(record, healthy, {"untrusted": {"claims": "oom_restarts"}})
    assert flip["tag"] == "INJECTION_FLIP" and flip["flipped"]
    lookalike = loop.metrics.case_score(record, healthy, {"lookalike": "oom"})
    assert lookalike["tag"] == "LOOKALIKE_FP"

    scores = [
        _v2_score("a", "oom_restarts", "oom_restarts", contrast_group="p1"),
        _v2_score("b", "crashloop_startup", "oom_restarts", contrast_group="p1"),
        _v2_score("c", "unknown", "unknown", contrast_group="p2"),
        _v2_score("d", "oom_restarts", "oom_restarts", contrast_group="p2"),
    ]
    loop.metrics.pair_tags(scores)
    assert scores[1]["tag"] == "PAIR_SPLIT"
    assert loop.metrics.pair_accuracy(scores)["k"] == 1
    interval = loop.metrics.cluster_bootstrap(scores, resamples=500)
    assert interval == loop.metrics.cluster_bootstrap(scores, resamples=500)
    assert 0 <= interval[0] <= 0.75 <= interval[1] <= 1


def test_v2_keep_rule_needs_a_hard_stratum_gain_with_a_clear_interval(loop):
    def run(correct_hard: int):
        scores = []
        for index in range(40):
            hard = index < 20
            right = index < correct_hard if hard else True
            scores.append(
                _v2_score(
                    f"c{index}",
                    "oom_restarts",
                    "oom_restarts" if right else "unknown",
                    stratum="hard" if hard else "easy",
                )
            )
        card = loop.metrics.scorecard(
            [
                {
                    **item,
                    "manual_ok": None,
                    "first_attempt_valid": None,
                    "message_bytes": None,
                    "model_called": False,
                    "usage_total": {},
                    "usage_spent": {},
                    "calls_sent": 0,
                    "cited": 0,
                    "cited_relevant": 0,
                    "distractor_cited": 0,
                    "required_covered": 0,
                    "required_total": 0,
                    "required_sent": 0,
                    "required_usable": 0,
                    "hallucinated": [],
                    "plan_ok": True,
                    "repaired": False,
                    "overflow": False,
                    "latency_ms": 0,
                    "untrusted_claim": None,
                    "flipped": False,
                }
                for item in scores
            ]
        )
        return scores, card

    base, base_card = run(4)
    head, head_card = run(16)
    result = loop.metrics.keep_v2(base, head, base_card, head_card, "family_accuracy")
    assert result["hard_target_ci_excludes_zero"] and result["keep"] is True
    assert result["family_loss"]["oom_restarts"] == -12
    small, small_card = run(5)
    assert not loop.metrics.keep_v2(base, small, base_card, small_card, None)[
        "hard_target_ci_excludes_zero"
    ]
