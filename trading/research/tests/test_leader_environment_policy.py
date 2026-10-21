from copy import deepcopy

import pytest

from trading.research.leader_environment_policy import evaluate_environment_path, STRENGTH, WEAKNESS, MIXED

DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
AS_OF = "2026-10-15T18:30:00+08:00"


def obs(day, breadth=.3, median=1):
    return {"day": day, "available_at": day + "T18:00:00+08:00", "source_complete": True,
            "breadth": breadth, "median_return_pct": median}


def test_fixed_two_close_confirmation_and_eight_distinct_applicability_rules():
    result = evaluate_environment_path([obs(DAYS[0]), obs(DAYS[1])], DAYS, AS_OF)
    assert result["phase"] == STRENGTH and result["two_close_confirmation"]
    assert len(result["per_type"]) == 8 and result["execution_not_before"] == DAYS[2]
    assert result["per_type"]["relay_continuation_leader"]["mode"] == "ALLOW_FIXED_CANDIDATES"
    assert result["per_type"]["rebound_repair_candidate"]["mode"] == "EXIT_ALL"
    assert result["selection_enabled"] is result["execution_admitted"] is False


def test_known_weakness_exits_immediately_even_without_confirmation_or_type_evidence():
    result = evaluate_environment_path([obs(DAYS[1], -.3, -1)], DAYS, AS_OF)
    assert result["phase"] == WEAKNESS and not result["two_close_confirmation"]
    assert all(row["mode"] == "EXIT_ALL" for row in result["per_type"].values())


@pytest.mark.parametrize("rows", [[obs(DAYS[1])], [obs(DAYS[0]), obs(DAYS[2])],
    [obs(DAYS[0]), obs(DAYS[1], 0, .1)]])
def test_new_phase_or_missing_adjacent_day_cannot_confirm_or_create_cash_exit(rows):
    result = evaluate_environment_path(rows, DAYS, AS_OF)
    assert result["mode"] == "NO_CHANGE" and not result["two_close_confirmation"]


def test_mixed_confirmation_allows_repair_but_exits_relay_and_unknown_is_not_weakness():
    result = evaluate_environment_path([obs(DAYS[0], 0, 0), obs(DAYS[1], .1, .1)], DAYS, AS_OF)
    assert result["phase"] == MIXED
    assert result["per_type"]["rebound_repair_candidate"]["mode"] == "ALLOW_FIXED_CANDIDATES"
    assert result["per_type"]["relay_continuation_leader"]["mode"] == "EXIT_ALL"
    row = obs(DAYS[2], -.5, -2); row["source_complete"] = False
    unknown = evaluate_environment_path([row], DAYS, AS_OF)
    assert unknown["phase"] == "unknown" and unknown["mode"] == "NO_CHANGE"


@pytest.mark.parametrize("change", ["future", "intraday", "phase", "duplicate"])
def test_false_clocks_labels_and_duplicate_observations_rejected(change):
    rows = [obs(DAYS[0]), obs(DAYS[1])]
    if change == "future":
        rows[-1]["available_at"] = "2026-10-16T18:00:00+08:00"
    elif change == "intraday":
        rows[-1]["available_at"] = DAYS[1] + "T14:59:00+08:00"
    elif change == "phase":
        rows[-1]["phase"] = WEAKNESS
    else:
        rows.append(deepcopy(rows[-1]))
    with pytest.raises(ValueError):
        evaluate_environment_path(rows, DAYS, AS_OF)
