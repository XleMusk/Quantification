"""Independent small-completion proofs; no market data, model or Engine runs."""
from itertools import product
import random

import pytest

from trading.research.leader_rank_certainty import CapacityTracker, certify_topk


def _possible_rankings(known, unknown, k):
    """Enumerate optional membership, including each candidate's actual bound."""
    possibilities = []
    for row in unknown:
        bound = row["upper_score"]
        # The finite oracle uses the maximum as well as every half-step below it.
        values = [None] + [step / 2 for step in range(int(bound * 2) + 1)]
        possibilities.append(values)
    for scores in product(*possibilities):
        completed = [(row["symbol"], float(row["score"])) for row in known]
        completed.extend((row["symbol"], score) for row, score in zip(unknown, scores)
                         if score is not None)
        yield sorted(completed, key=lambda row: (-row[1], row[0]))[:k]


def test_rank_certificate_agrees_with_every_optional_unknown_completion():
    rng = random.Random(731)
    certified_count = uncertain_count = 0
    # Scores are half-integers, so every bound is included in the independent oracle.
    for _ in range(180):
        symbols = list("ABCDEFGH")
        rng.shuffle(symbols)
        count, unknown_count = rng.randrange(6), rng.randrange(3)
        known = [{"symbol": symbol, "score": rng.randrange(7) / 2}
                 for symbol in symbols[:count]]
        unknown = [{"symbol": symbol, "upper_score": rng.randrange(7) / 2}
                   for symbol in symbols[count:count + unknown_count]]
        k = rng.randrange(1, 5)
        certificate = certify_topk(known, unknown, k)
        reported = [(row["symbol"], row["score"]) for row in certificate["ranking"]]
        actual = list(_possible_rankings(known, unknown, k))
        assert reported == next(_possible_rankings(known, [], k))
        if certificate["certified"]:
            certified_count += 1
            assert all(ranking == reported for ranking in actual)
        else:
            uncertain_count += 1
            # The finite domain contains every upper bound and the absent case.
            assert any(ranking != reported for ranking in actual)
    assert certified_count and uncertain_count

    tied = [{"symbol": "B", "score": 2}, {"symbol": "D", "score": 1}]
    assert certify_topk(tied, [{"symbol": "Z", "upper_score": 1}], 2)["certified"]
    assert not certify_topk(tied, [{"symbol": "A", "upper_score": 1}], 2)["certified"]
    assert not certify_topk(tied[:1], [{"symbol": "Z", "upper_score": 0}], 2)["certified"]
    assert not certify_topk(tied, [{"symbol": "Z", "upper_score": None}], 2)["certified"]
    empty = certify_topk([], [], 5)
    assert empty["certified"] and empty["ranking"] == []
    assert empty["target_nonempty_certified"] is False
    assert certify_topk(tied[:1], [], 5)["certified"]

    for bad in (float("nan"), float("inf"), -1, True):
        with pytest.raises(ValueError):
            certify_topk([{"symbol": "A", "score": bad}], [])
        with pytest.raises(ValueError):
            certify_topk([], [{"symbol": "A", "upper_score": bad}])
    for bad_k in (0, -1, True, 1.0):
        with pytest.raises(ValueError):
            certify_topk([], [], bad_k)
    with pytest.raises(ValueError):
        certify_topk([{"symbol": "A", "score": 1}] * 2, [])
    with pytest.raises(ValueError):
        certify_topk([{"symbol": "A", "score": 1}], [{"symbol": "A", "upper_score": 0}])


def test_capacity_ambiguity_is_sticky_and_source_path_rejection_is_atomic():
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    ambiguous = CapacityTracker(cap=2, expected_days=days)
    first = certify_topk([{"symbol": "B", "score": 2}],
                        [{"symbol": "A", "upper_score": 3}], 1)
    initial = ambiguous.observe(days[0], first, [], ["B"])
    assert not initial["capacity_state_certified"] and not initial["entry_targets_certified"]
    known_later = certify_topk([{"symbol": "C", "score": 3}], [], 1)
    later = ambiguous.observe(days[1], known_later, ["B"], ["B", "C"])
    assert not later["capacity_state_certified"] and not later["entry_targets_certified"]
    final = ambiguous.observe(days[2], known_later, ["B", "C"], ["B", "C"])
    assert final["first_capacity_ambiguity_day"] == days[0]
    assert not final["capacity_state_certified"]

    full = CapacityTracker(cap=2, expected_days=days)
    fill = certify_topk([{"symbol": "A", "score": 2}, {"symbol": "B", "score": 1}], [], 2)
    assert full.observe(days[0], fill, [], ["A", "B"])["entry_targets_certified"]
    uncertain = certify_topk([{"symbol": "C", "score": 5}],
                            [{"symbol": "X", "upper_score": 6}], 1)
    for before, after in ((["B", "A"], ["A", "B"]), (["A", "B"], ["A", "B", "C"])):
        with pytest.raises(ValueError):
            full.observe(days[1], uncertain, before, after)
    unchanged = full.observe(days[1], uncertain, ["A", "B"], ["A", "B"])
    assert unchanged["observed_sessions"] == 2
    assert unchanged["capacity_state_certified"] and not unchanged["entry_targets_certified"]
    assert unchanged["observed_pool_after"] == ["A", "B"]
    with pytest.raises(ValueError):
        full.observe(days[1], uncertain, ["A", "B"], ["A", "B"])

    # A not-yet-full pool is safe only if every possible entrant is already inside.
    growing = CapacityTracker(cap=3, expected_days=days)
    growing.observe(days[0], fill, [], ["A", "B"])
    inside = certify_topk([{"symbol": "A", "score": 2}],
                         [{"symbol": "B", "upper_score": 3}], 1)
    safe = growing.observe(days[1], inside, ["A", "B"], ["A", "B"])
    assert safe["capacity_state_certified"] and not safe["entry_targets_certified"]
    # The unknown belongs to the old pool, but may displace the new known X.
    displacement = certify_topk([{"symbol": "X", "score": 2}],
                               [{"symbol": "A", "upper_score": 3}], 1)
    unsafe = growing.observe(days[2], displacement, ["A", "B"], ["A", "B", "X"])
    assert not unsafe["capacity_state_certified"] and not unsafe["entry_targets_certified"]
    assert unsafe["first_capacity_ambiguity_day"] == days[2]
