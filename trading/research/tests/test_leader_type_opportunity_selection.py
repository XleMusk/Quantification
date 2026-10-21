"""Only the new pure skip-known-empty rule; no Engine or market inputs."""
from copy import deepcopy

import pytest

from trading.research.leader_type_opportunity_selection import TYPE_IDS, select_opportunity


DAY, CAPACITY = "2026-10-19", "uncapped"


def inputs():
    return {"cycle": list(TYPE_IDS), "capacity": CAPACITY, "members": {"600001.SSE", "600002.SSE"},
        "modes": {identity: "ALLOW_FIXED_CANDIDATES" for identity in TYPE_IDS},
        "panel": {"day": DAY, "types": [{"id": identity, "status": "not_matched", "unresolved_symbols": []}
            for identity in TYPE_IDS]},
        "paths": {identity: {CAPACITY: {DAY: {"candidates": []}}} for identity in TYPE_IDS}}


def test_only_known_empty_is_skipped_and_first_nonempty_keeps_exact_original_order_without_mutation():
    args = inputs()
    selected = [{"symbol": "600002.SSE", "score": 2.}, {"symbol": "600001.SSE", "score": 1.}]
    args["panel"]["types"][1]["status"] = "matched"
    args["paths"][TYPE_IDS[1]][CAPACITY][DAY]["candidates"] = selected
    before = deepcopy(args)
    value = select_opportunity(DAY, **args)
    assert value["selected_type"] == TYPE_IDS[1] and value["selected_candidates"] == selected
    assert [row["reason"] for row in value["considered_types"]] == [
        "known_empty_original_capacity_path", "first_complete_nonempty_original_path"]
    assert value["decision_kind"] == "CANDIDATE_INPUT" and value["execution_admitted"] is False
    value["selected_candidates"][0]["symbol"] = "not_original"
    assert args == before


@pytest.mark.parametrize("fault", ["unknown_item", "unresolved_member", "missing_path", "null_candidates"])
def test_unknown_blocks_at_that_type_instead_of_falling_through_to_later_opportunity(fault):
    args = inputs()
    args["panel"]["types"][2]["status"] = "matched"
    args["paths"][TYPE_IDS[2]][CAPACITY][DAY]["candidates"] = [{"symbol": "600001.SSE", "score": 9.}]
    item = args["panel"]["types"][1]
    if fault == "unknown_item":
        item["status"] = "unknown"
    elif fault == "unresolved_member":
        item["unresolved_symbols"] = ["600001.SSE"]
    elif fault == "missing_path":
        del args["paths"][TYPE_IDS[1]][CAPACITY][DAY]
    else:
        args["paths"][TYPE_IDS[1]][CAPACITY][DAY]["candidates"] = None
    value = select_opportunity(DAY, **args)
    assert value["status"] == "blocked_unknown" and value["selected_type"] == TYPE_IDS[1]
    assert value["decision_kind"] == "NO_CHANGE" and value["selected_candidates"] is None
    assert len(value["considered_types"]) == 2


def test_environment_excluded_types_are_not_inspected_and_all_known_empty_is_NC_not_empty_FULL():
    args = inputs()
    args["modes"][TYPE_IDS[0]] = "NO_CHANGE"
    args["panel"]["types"][0]["status"] = "unknown"
    del args["paths"][TYPE_IDS[0]]
    # An unresolved security outside the original fixed members is irrelevant.
    args["panel"]["types"][1]["unresolved_symbols"] = ["outside_fixed_scope"]
    value = select_opportunity(DAY, **args)
    assert value["status"] == "known_all_empty" and value["selected_type"] is None
    assert value["decision_kind"] == "NO_CHANGE" and value["selected_candidates"] is None
    assert value["considered_types"][0]["reason"] == "original_environment_not_allowed"


def test_only_exact_D_is_read_and_original_rotation_is_required():
    args = inputs()
    args["cycle"] = list(TYPE_IDS[3:]+TYPE_IDS[:3])
    class OwnDayOnly(dict):
        def get(self, day, default=None):
            assert day == DAY
            return super().get(day, default)
    for identity in TYPE_IDS:
        args["paths"][identity][CAPACITY] = OwnDayOnly(args["paths"][identity][CAPACITY])
    assert select_opportunity(DAY, **args)["status"] == "known_all_empty"
    args["cycle"][0], args["cycle"][1] = args["cycle"][1], args["cycle"][0]
    with pytest.raises(ValueError, match="original complete cyclic"):
        select_opportunity(DAY, **args)
