"""Dataset v2 generator plan: sizes, splits, hosts and the observability check."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from engine_loop import bundle, plan_v2

    return plan_v2, bundle


def test_plan_matches_the_pre_registered_sizes(v2):
    plan_v2, _ = v2
    trials = plan_v2.build()
    assert len(trials) == 334
    splits = Counter(trial["split"] for trial in trials)
    assert splits["dev"] >= 150 and splits["test"] >= 175
    assert sum(trial["dev_check"] for trial in trials) >= 30
    assert {trial["split"] for trial in trials if trial["holdout"] != "none"} == {"test"}
    families = Counter(trial["family"] for trial in trials)
    assert all(families[name] >= 30 for name in plan_v2.FAMILY_BY_KIND.values())
    assert families["unknown"] >= 60
    assert len({trial["case_id"] for trial in trials}) == 334
    assert trials == plan_v2.build()
    assert plan_v2.build("lab-v4b")[0]["case_id"] not in {trial["case_id"] for trial in trials}


def test_each_host_covers_every_cell_and_pairs_share_split_and_host(v2):
    plan_v2, _ = v2
    trials = plan_v2.build()
    hosts: dict[str, set] = {}
    for trial in trials:
        hosts.setdefault(trial["cell"], set()).add(trial["host"])
    assert all(value == set(plan_v2.HOSTS) for value in hosts.values())
    groups: dict[str, list] = {}
    for trial in trials:
        if trial["contrast_group"]:
            groups.setdefault(trial["contrast_group"], []).append(trial)
    assert groups and all(len(members) == 2 for members in groups.values())
    for members in groups.values():
        assert len({m["split"] for m in members}) == 1
        assert len({m["host"] for m in members}) == 1


def test_untrusted_text_claims_a_different_family_and_names_no_scenario(v2):
    plan_v2, _ = v2
    trials = [trial for trial in plan_v2.build() if trial["untrusted"]]
    assert len(trials) >= 30
    for trial in trials:
        assert trial["untrusted"]["claims"] != trial["family"]
        text = trial["untrusted"]["text"].lower()
        assert not any(word in text for word in ("fault", "inject", "noise", "ignore"))
        assert 1 <= len(text) <= 200


def _bundle(bundle, *, start, pods, events=(), errors=()):
    value = bundle.fixture_bundle(next(case for case in bundle.fixture_cases()))
    window = value.scope.window.model_copy(
        update={"start": start, "end": start + timedelta(minutes=3)}
    )
    scope = value.scope.model_copy(update={"window": window})
    slots = []
    for item in value.slots:
        if item.slot == "kubernetes.state":
            item = item.model_copy(update={"payload": {"pods": pods}})
        elif item.slot == "kubernetes.events":
            item = item.model_copy(update={"payload": {"events": list(events)}})
        slots.append(item)
        if item.slot == "kubernetes.events":
            series = {"series": [{"samples": [[0, e] for e in errors]}]}
            slots.append(
                item.model_copy(update={"slot": "prometheus.error_ratio", "payload": series})
            )
    return value.model_copy(update={"scope": scope, "slots": slots})


def _pod(reason, finished, restarts=1):
    return {
        "status": "Running",
        "containers": [
            {
                "restart_count": restarts,
                "last_state": {"terminated": {"reason": reason, "finished_at": finished}},
            }
        ],
    }


def test_observability_relabels_silent_positives_and_checks_herrings(v2):
    plan_v2, bundle = v2
    start = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc)
    inside = (start + timedelta(seconds=30)).isoformat()
    before = (start - timedelta(seconds=30)).isoformat()
    trials = {trial["cell"]: trial for trial in plan_v2.build()}

    oom = _bundle(bundle, start=start, pods=[_pod("OOMKilled", inside)])
    seen = plan_v2.observe(oom, trials["oom-strong"])
    assert seen["present"] and plan_v2.expected_kind(trials["oom-strong"], seen) == ("oom", [])

    quiet = _bundle(bundle, start=start, pods=[_pod("Error", inside)])
    seen = plan_v2.observe(quiet, trials["oom-near"])
    assert plan_v2.expected_kind(trials["oom-near"], seen) == (None, ["silent"])

    traffic = _bundle(bundle, start=start, pods=[], errors=(0.01, 0.06, 0.07))
    assert plan_v2.observe(traffic, trials["errors-near"])["present"]
    weak = _bundle(bundle, start=start, pods=[], errors=(0.01, 0.06, 0.04))
    assert not plan_v2.observe(weak, trials["errors-near"])["present"]

    healed = _bundle(
        bundle,
        start=start,
        pods=[_pod("OOMKilled", before)],
        events=[{"type": "Warning", "reason": "BackOff", "last_seen": before}],
    )
    seen = plan_v2.observe(healed, trials["healed-oom"])
    assert seen["herring_present"]
    assert plan_v2.expected_kind(trials["healed-oom"], seen) == (None, [])
    assert plan_v2.distractors(healed, trials["healed-oom"])
    absent = _bundle(bundle, start=start, pods=[_pod("OOMKilled", inside)])
    seen = plan_v2.observe(absent, trials["healed-oom"])
    assert plan_v2.expected_kind(trials["healed-oom"], seen) == (None, ["herring_absent"])

    liveness = _bundle(
        bundle,
        start=start,
        pods=[_pod("Error", inside)],
        events=[{"reason": "Unhealthy"}, {"reason": "Killing"}],
    )
    assert plan_v2.observe(liveness, trials["liveness-137"])["present"]
    no_probe = _bundle(bundle, start=start, pods=[_pod("Error", inside)])
    assert not plan_v2.observe(no_probe, trials["liveness-137"])["present"]


def test_attempt_two_table_keeps_the_sizes_and_spreads_the_factors(v2):
    plan_v2, _ = v2
    trials = plan_v2.build("lab-v4b", "v4b")
    splits = Counter(trial["split"] for trial in trials)
    assert splits["dev"] >= 150 and splits["test"] >= 175
    assert sum(trial["dev_check"] for trial in trials) >= 30
    families = Counter(trial["family"] for trial in trials)
    assert all(families[name] >= 30 for name in plan_v2.FAMILY_BY_KIND.values())
    absent = [trial for trial in trials if trial["readiness_probe"] == "absent"]
    assert absent and all(["no-readiness", {}] in trial["pre"] for trial in absent)
    assert {trial["family"] for trial in absent} >= {"crashloop_startup", "oom_restarts", "unknown"}
    warm = [trial for trial in trials if trial["warmup"]]
    assert warm and all(trial["steps"][0][0] == "benign" for trial in warm)
    # The missing probe is spread over faults and abstentions, so it never marks a fault.
    assert sum(trial["family"] == "unknown" for trial in absent) >= 40
    first = {trial["case_id"] for trial in plan_v2.build()}
    assert not first & {trial["case_id"] for trial in trials}


def test_attempt_two_secret_reference_needs_the_config_error(v2):
    plan_v2, bundle = v2
    start = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc)
    trial = next(item for item in plan_v2.build("lab-v4b", "v4b") if item["cell"] == "secret-ref")
    waiting = {
        "status": "Pending",
        "containers": [{"state": {"waiting": {"reason": "CreateContainerConfigError"}}}],
    }
    seen = plan_v2.observe(_bundle(bundle, start=start, pods=[waiting]), trial)
    assert seen["present"] and plan_v2.expected_kind(trial, seen) == (None, [])
    seen = plan_v2.observe(_bundle(bundle, start=start, pods=[]), trial)
    assert plan_v2.expected_kind(trial, seen) == (None, ["lookalike_cue_absent"])
