"""New information policy behavior; synthetic shared Engine, no market replay."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_known_identity_scope as m
from trading.research import leader_eight_type_panel_replay as replay
from trading.research.tests.test_leader_eight_type_panel_replay import (
    A, B, DAYS, SESSIONS, PREVIOUS, raw, panels, qualification,
)


def fixture():
    days = SESSIONS[SESSIONS.index(DAYS[0]):SESSIONS.index(DAYS[0]) + 12]
    p = panels(days, empty=True)
    for d in days:
        p[d]["risk_features"] = {s: {"date": d, "close": 10., "open": 10.,
            "qualified_stage": 0, "qualified_limit_up": False, "ipo_feature_support": True,
            "listing_feature_policy_verified": True} for s in (A, B)}
        p[d]["risk_feature_scope_complete"] = True
        item = next(x for x in p[d]["types"] if x["id"] == m.TYPE_ID)
        item.update(unresolved_symbols=[B], scope_complete=False)
    day = days[2]
    row = {"symbol": A, "date": day, "source_day": day, "score": 100.,
        "available_at": p[day]["available_at"], "historical_identity_method": m.IDENTITY_METHOD,
        "evidence": {"status": "matched", "reason": None, "structure_close": 10.,
            "confirmation_day": day, "episode_close_days": days[:3],
            "episode_id": m.IDENTITY_METHOD + ":" + days[0],
            "original_proxy_identity": {"symbol": A, "labels": ["trend_leader"],
                "identity_source_day": PREVIOUS[days[0]]},
            "legs": [{"from_day": before, "to_day": after, "provider_pct": 0.,
                "prior_raw_close": 10., "native_preclose": 10., "market_median_pct": 0.,
                "sector_median_pct": 0., "sector_denominator": 13,
                "reference_status": "original_one_percent_proxy_not_absence_of_actions"}
                for before, after in zip(days[:2], days[1:3])]}}
    item = next(x for x in p[day]["types"] if x["id"] == m.TYPE_ID)
    item.update(status="matched", candidates=[row])
    return days, p, row


def policy(p, day, rows, *, held=(), pending=(), item=None):
    return m.entry_membership_policy(day=day, identity=m.TYPE_ID,
        item=item or next(x for x in p[day]["types"] if x["id"] == m.TYPE_ID),
        selected=rows, members={A, B}, sessions=SESSIONS, panels=p,
        held_symbols=set(held), pending_symbols=set(pending))


def case(p, days, *, opt_in=True, guard="unguarded", env_override=None, bars=None):
    paths, _ = replay._rank_path(p, {A, B}, days[0], days[4])
    env = replay._environment(p, SESSIONS, max(x["available_at"] for x in p.values()))
    if env_override:
        env_override(env)
    kwargs = {"entry_membership_policy": m.entry_membership_policy} if opt_in else {}
    return replay._case(raw(days) if bars is None else bars, days, SESSIONS, PREVIOUS, p,
        paths[m.TYPE_ID]["causal_first_seen_cap20"], env, {A, B}, qualification(),
        {"day": PREVIOUS[days[0]], "closes": {A: 10., B: 10.}}, days[0], days[4],
        m.TYPE_ID, "causal_first_seen_cap20", guard, "synthetic-known-scope", **kwargs)


def test_unrelated_unknown_is_retained_but_known_candidate_can_enter_and_expire():
    days, p, row = fixture()
    before = deepcopy(p)
    result = case(p, days)
    assert p == before
    assert result["complete_PnL_available"] is True
    buys = [f for f in result["fills"] if f["side"] == "buy"]
    sells = [f for f in result["fills"] if f["side"] == "sell"]
    assert buys and buys[0]["day"] == days[3]
    assert sells and sells[0]["day"] == days[8]
    d = result["decisions"][2]
    assert d["status"] == "FULL_TARGET"
    assert d["entry_membership_evidence"]["original_unresolved_symbols"] == [B]
    assert d["entry_membership_evidence"]["original_scope_complete"] is False
    assert d["entry_membership_evidence"]["full_fixed_scope_complete_claimed"] is False
    assert result["decisions"][3]["status"] == "NO_CHANGE"  # empty does not liquidate
    old = case(p, days, opt_in=False)
    assert old["fills"] == [] and old["metrics"] is None
    assert all("entry_membership_evidence" not in d for d in old["decisions"])
    assert "type_membership_unknown_in_fixed_panel" in old["decisions"][2]["reasons"]


@pytest.mark.parametrize("fault", ["missing_leg", "wrong_prior_identity", "zero_denominator", "ipo_unknown", "structure_broken"])
def test_selected_exact_witness_unknown_remains_no_change_without_refill(fault):
    days, p, row = fixture()
    if fault == "missing_leg":
        row["evidence"]["legs"].pop()
    elif fault == "wrong_prior_identity":
        row["evidence"]["original_proxy_identity"]["identity_source_day"] = days[0]
    elif fault == "zero_denominator":
        row["evidence"]["legs"][0]["sector_denominator"] = 0
    elif fault == "ipo_unknown":
        p[days[0]]["risk_features"][A]["ipo_feature_support"] = False
    else:
        p[days[2]]["risk_features"][A]["close"] = 9.
    proof = policy(p, days[2], [row])
    assert proof["blocking_reasons"] and proof["selected_witnesses"][A]["status"] == "unknown"


@pytest.mark.parametrize("ownership", ["held", "pending", "selected"])
def test_unresolved_selected_owned_and_pending_are_never_waived(ownership):
    days, p, row = fixture()
    item = next(x for x in p[days[2]]["types"] if x["id"] == m.TYPE_ID)
    item["unresolved_symbols"] = [A] if ownership == "selected" else [B]
    proof = policy(p, days[2], [row], held=[B] if ownership == "held" else [],
        pending=[B] if ownership == "pending" else [])
    assert proof["blocking_reasons"]
    assert proof["unknown_members_reclassified"] is False


def test_owned_unknown_prevents_omission_sale_but_known_environment_exit_still_runs():
    days, p, row = fixture()
    # After A's buy, unknown A must not disappear from a new full target.
    item = next(x for x in p[days[3]]["types"] if x["id"] == m.TYPE_ID)
    item.update(status="matched", candidates=[], unresolved_symbols=[A, B])
    result = case(p, days)
    assert "owned_or_pending_identity_unknown_NO_CHANGE" in result["decisions"][3]["reasons"]
    assert not [f for f in result["fills"] if f["side"] == "sell" and f["day"] < days[8]]
    def exits(env):
        env[days[3]]["per_type"][m.TYPE_ID]["mode"] = "EXIT_ALL"
    exit_result = case(p, days, guard="two_close_envguard", env_override=exits)
    assert exit_result["decisions"][3]["status"] == "EXPLICIT_EXIT_ALL"
    assert next(f for f in exit_result["fills"] if f["side"] == "sell")["day"] == days[4]


def test_owned_action_reference_check_remains_before_new_policy():
    days, p, _ = fixture()
    bars = raw(days)
    bars.loc[bars.date.eq(days[4]) & bars.code.eq("sz." + A[:6]), "preclose"] = "9"
    result = case(p, days, bars=bars)
    assert result["blocking_evidence"]["day"] == days[4]
    assert result["blocking_evidence"]["reason"] == "held_or_pending_raw_or_action_reference_unknown"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def bound_fixture(tmp_path):
    source = tmp_path / "source" / "manifest.json"
    manifest = {"start": DAYS[0], "end": DAYS[-1], "source_days": DAYS,
        "scope_members": [A, B], "identity_research": {"explicit": True}}
    write(source, manifest)
    event, reference = tmp_path / "event", tmp_path / "reference"
    for folder in (event, reference):
        write(folder / "result.json", {"synthetic": folder.name})
    accounting = {"event_view": str(event.resolve()), "reference_view": str(reference.resolve()),
        "refs": [{"path": str((folder / "result.json").resolve()), "sha256": m.digest(folder / "result.json")}
            for folder in (event, reference)]}
    cases = [{"case_id": case_id, "type": m.TYPE_ID, "config": m._config(100000, 5).to_dict(),
        "orders": [], "fills": [], "metrics": None, "blocking_evidence": None,
        "engine_sessions_committed": len(DAYS), "decisions": [{"status": "NO_CHANGE"}]}
        for case_id in m.CASE_IDS]
    baseline = tmp_path / "old" / "result.json"
    write(baseline, {"schema": "leader_historical_identity_eight_case_shared_replay_v1",
        "source": {"manifest": str(source.resolve()), "manifest_sha256": m.digest(source)},
        "parent_fixed_protocol": m.PROTOCOL, "start": DAYS[0], "end": DAYS[-1], "source_days": DAYS,
        "cases": cases, "execution_policy": {"accounting": accounting}})
    return source, baseline, event, reference, accounting


def test_registration_binds_original_action_pair_and_never_replays_baseline(tmp_path, monkeypatch):
    source, baseline, event, reference, accounting = bound_fixture(tmp_path)
    before = source.read_bytes(), baseline.read_bytes()
    monkeypatch.setattr(m, "load_inputs", lambda *_: pytest.fail("registration must not load huge panels"))
    folder = tmp_path / "registration"
    record = m.register(source, baseline, event, reference, folder)
    assert record["baseline_replayed"] is False and record["independent_holdout_claimed"] is False
    assert record["case_ids"] == list(m.CASE_IDS) and record["execution_admitted"] is False
    assert m.load_registration(folder / "registration.json")[0] == record
    assert (source.read_bytes(), baseline.read_bytes()) == before
    with pytest.raises(ValueError, match="Exact original accounting"):
        m.register(source, baseline, reference, event, tmp_path / "wrong_pair")
    assert not (tmp_path / "wrong_pair").exists()
    event.joinpath("result.json").write_text("changed")
    with pytest.raises(ValueError, match="pair changed"):
        m.load_registration(folder / "registration.json")
