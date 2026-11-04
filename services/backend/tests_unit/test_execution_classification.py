import pytest

from cloudops.features.investigations import admission_settings
from cloudops.features.investigations.states import NONTERMINAL_RUN, TERMINAL_RUN
from cloudops.features.recovery.classification import BLOCKING, classify


@pytest.mark.parametrize(
    ("state", "holds_lock", "released", "expected"),
    [
        ("queued", True, False, "active"),
        ("executing", True, False, "active"),
        ("verifying", True, False, "active"),
        ("reconciling", True, False, "active"),
        ("rolling_back", True, False, "active"),
        ("outcome_unknown", True, False, "terminal_unknown_locked"),
        ("outcome_unknown", False, True, "terminal_unknown_released"),
        ("outcome_unknown", False, False, "unrecognized"),
        ("outcome_unknown", True, True, "unrecognized"),
        ("manual_intervention", False, True, "unrecognized"),
        ("manual_intervention", False, False, "terminal"),
        ("succeeded", False, False, "terminal"),
        ("manual_intervention", True, False, "unrecognized"),
        ("uncertain", False, False, "unrecognized"),
        ("", False, False, "unrecognized"),
    ],
)
def test_classification_fails_closed(state, holds_lock, released, expected):
    assert classify(state, holds_lock, released) == expected


def test_only_active_and_unrecognized_block_new_work():
    assert BLOCKING == {"active", "unrecognized"}
    # Terminal unknown permits read-only investigation; its target lock is untouched.
    assert classify("outcome_unknown", True, False) not in BLOCKING


def test_run_state_sets_are_disjoint_and_cover_the_contract():
    assert not set(NONTERMINAL_RUN) & set(TERMINAL_RUN)
    assert "cancelled" in TERMINAL_RUN and "queued" in NONTERMINAL_RUN


@pytest.mark.parametrize("value", ["0", "1001", "-1", "many", ""])
def test_deferred_limit_rejects_invalid_configuration(monkeypatch, value):
    admission_settings.deferred_run_limit.cache_clear()
    monkeypatch.setenv("DEFERRED_RUN_LIMIT", value)
    with pytest.raises(RuntimeError, match="DEFERRED_RUN_LIMIT"):
        admission_settings.deferred_run_limit()
    admission_settings.deferred_run_limit.cache_clear()


def test_deferred_limit_defaults_to_one_hundred(monkeypatch):
    admission_settings.deferred_run_limit.cache_clear()
    monkeypatch.delenv("DEFERRED_RUN_LIMIT", raising=False)
    assert admission_settings.deferred_run_limit() == 100
    monkeypatch.setenv("DEFERRED_RUN_LIMIT", "7")
    admission_settings.deferred_run_limit.cache_clear()
    assert admission_settings.deferred_run_limit() == 7
    admission_settings.deferred_run_limit.cache_clear()
