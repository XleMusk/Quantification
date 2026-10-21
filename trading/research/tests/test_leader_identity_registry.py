import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_identity_registry as r
from trading.research.leader_environment_run import BOUNDARY


def clock(value):
    return lambda: datetime.fromisoformat(value)


def fixture(tmp_path, count=2):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                   "closed_ranges": [], "open_sessions": []}), encoding="utf8")
    qualification = {"evidence_sha256": "frozen", "pit_verified": False}
    symbols = [f"600{i:03}.SSE" for i in range(count)]

    def make(day, *, sign=-1, closes=None, pcts=None, absent=(), reference=None, tag=""):
        folder = tmp_path / (day + tag)
        folder.mkdir()
        rows = []
        for i, symbol in enumerate(symbols):
            if symbol in absent:
                continue
            rows.append({"date": day, "symbol": symbol, "close": (closes or {}).get(symbol, 10.),
                         "pct_chg": (pcts or {}).get(symbol, 1.),
                         "preclose_for_limits": (reference or {}).get(symbol, 10.),
                         "listing_qualification_status": "supported"})
        for i in range(25):
            rows.append({"date": day, "symbol": f"002{i:03}.SZSE", "close": 10., "pct_chg": sign * 3.,
                         "preclose_for_limits": 10., "listing_qualification_status": "supported"})
        frame = pd.DataFrame(rows)
        file = folder / "features.csv"
        frame.to_csv(file, index=False)
        created = day + "T15:10:00+08:00"
        preview = {"schema": "leader_complete_close_preview_v1", "status": "complete_source_close_research_preview_only",
                   "source_inventory_complete": True, "day": day, "created_at": created,
                   "calendar": str(calendar), **BOUNDARY}
        (folder / "preview.json").write_text(json.dumps(preview), encoding="utf8")
        archetypes = [{"id": id_, "name": name, "status": "not_matched", "candidates": [], "candidate_count": 0} for id_, name in r.ARCHETYPES]
        candidates = [{"symbol": s, "name": s, "source_day": day, "trade_ready": False,
                       "evidence": {"proxy": True}, "invalidation": ["lost_structure"]}
                      for s in symbols if s not in absent]
        for index in (0, 1):
            archetypes[index]["status"] = "matched"
            archetypes[index]["candidates"] = candidates
            archetypes[index]["candidate_count"] = len(candidates)
        pct = frame.pct_chg
        breadth = (int(pct.gt(0).sum()) - int(pct.lt(0).sum())) / len(frame)
        phase = "broad_strength_snapshot" if breadth >= .2 else "broad_weakness_snapshot" if breadth <= -.2 else "mixed_breadth_snapshot"
        analysis = {"implementation_revision": "test_v1", "day": day, "observed_at": created,
                    "status": "complete_close_descriptive_research_only", "archetypes": archetypes,
                    "market_environment": {"phase": phase, "label": phase, "evidence": {
                        "active_non_st_rows": len(frame), "median_return_pct": float(pct.median()), "breadth": breadth}},
                    "frozen_rule_spec": {"breadth_strength_threshold": .2, "breadth_weakness_threshold": -.2},
                    **BOUNDARY, "signal_admitted": False}
        result = {"schema": r.ANALYSIS_SCHEMA, "status": "completed_research_environment_analysis",
                  "day": day, "created_at": created, "analysis": analysis,
                  "calendar": str(calendar), "calendar_sha256": r.digest(calendar), "ipo_qualification": qualification,
                  "input_previews": [{"features_match_current_active_raw": True, "preview": str(folder / "preview.json"),
                      "preview_sha256": r.digest(folder / "preview.json"), "feature_file": str(file),
                      "feature_sha256": r.digest(file), "raw_daily_sha256": "test_raw",
                      "feature_qualification": {"method_id": r.METHOD_ID, "decision_as_of": day + "T15:05:00+08:00",
                                                "verification": qualification}}],
                  "provider_contacted": False, "account_created": False, "current_signal_admitted": False,
                  "research_only": True, **BOUNDARY}
        (folder / "result.json").write_text(json.dumps(result), encoding="utf8")
        return folder / "result.json"
    return make, symbols


def registered(tmp_path, count=2, when="2026-10-15T12:00:00+08:00"):
    make, symbols = fixture(tmp_path, count)
    out = tmp_path / "cohort"
    r.register(make("2026-10-14"), out, _clock=clock(when))
    return make, symbols, out / "registry.json"


def append(path, make, day, **kwargs):
    return r.observe(path, make(day, **kwargs), _clock=clock(day + "T16:00:00+08:00"))


def test_full_union_has_all_twenty_members_and_all_labels_not_display_cap(tmp_path):
    _, symbols, path = registered(tmp_path, 20)
    registry = r._json(path)
    assert [m["symbol"] for m in registry["members"]] == symbols
    assert registry["member_count"] == 20
    assert all(len(m["labels"]) == 2 for m in registry["members"])
    assert registry["first_eligible_close_day"] == "2026-10-15"
    assert registry["source"]["day"] == "2026-10-14"
    assert registry["registered_at"].startswith("2026-10-15T12:")
    assert registry["baseline_is_entry_price"] is False
    with pytest.raises(FileExistsError):
        r.register(registry["source"]["analysis"], path.parent, _clock=clock("2026-10-15T12:01:00+08:00"))


def test_future_source_registration_and_old_default_feature_method_rejected(tmp_path):
    make, _ = fixture(tmp_path)
    analysis = make("2026-10-15")
    with pytest.raises(ValueError, match="actually available"):
        r.register(analysis, tmp_path / "bad", _clock=clock("2026-10-15T12:00:00+08:00"))
    value = r._json(analysis)
    value["input_previews"][0].pop("feature_qualification")
    analysis.write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="production listing-feature"):
        r.register(analysis, tmp_path / "bad", _clock=clock("2026-10-15T16:00:00+08:00"))
    assert not (tmp_path / "bad").exists()


def test_after_close_first_eligible_advances_and_stale_context_cannot_create_episode(tmp_path):
    make, _, path = registered(tmp_path, when="2026-10-15T15:00:00+08:00")
    assert r._json(path)["first_eligible_close_day"] == "2026-10-16"
    append(path, make, "2026-10-16", sign=1)
    report = r.report(path, _clock=clock("2026-10-16T16:01:00+08:00"))
    assert report["episode"] is None


def test_no_backfill_or_skipping_exact_prospective_sessions(tmp_path):
    make, _, path = registered(tmp_path)
    for day in ("2026-10-14", "2026-10-16"):
        analysis = make(day, tag="duplicate" if day.endswith("14") else "")
        with pytest.raises(ValueError, match="next exact prospective close"):
            r.observe(path, analysis, _clock=clock("2026-10-16T16:00:00+08:00"))
    assert not (path.parent / "observations").exists()


def test_first_episode_three_closes_means_two_intervals_and_anchor_return_excluded(tmp_path):
    make, symbols, path = registered(tmp_path)
    one, two = symbols
    # The -90% confirmation-day figure includes registration-before-close time;
    # it must NEVER be treated as a measured post-registration interval.
    append(path, make, "2026-10-15", sign=1, pcts={one: -90., two: 90.})
    append(path, make, "2026-10-16", sign=-1, closes={one: 10.2, two: 9.8}, pcts={one: 2., two: -2.})
    append(path, make, "2026-10-19", sign=-1, closes={one: 10.3, two: 9.7}, pcts={one: .98, two: -1.02},
           reference={one: 10.2, two: 9.8})
    report = r.report(path, _clock=clock("2026-10-19T16:01:00+08:00"))
    a, b = report["members"]
    assert report["episode"]["confirmation_close_day"] == "2026-10-15"
    assert report["episode"]["fixed_window_close_days"] == ["2026-10-15", "2026-10-16", "2026-10-19"]
    assert a["status"] == "price_proxy_conditions_matched"
    assert a["unadjusted_endpoint_change_pct"] == pytest.approx(3.)
    assert len(a["intervals"]) == 2
    assert a["intervals"][0]["from_close_day"] == "2026-10-15"
    assert b["status"] == "price_proxy_conditions_not_matched"
    assert sum(report["status_counts"].values()) == 2
    assert report["cross_regime_leader_verified"] is False
    assert report["rule"]["economic_return_or_PnL"] is False
    # A later opposite transition may not replace the first episode.
    append(path, make, "2026-10-20", sign=1)
    later = r.report(path, _clock=clock("2026-10-20T16:01:00+08:00"))
    assert later["episode"] == report["episode"]
    assert later["members"] == report["members"]


@pytest.mark.parametrize("failure", ["absent", "reference_gap", "ipo_unknown"])
def test_each_failed_leg_retains_member_as_unknown_never_zero_or_survivor_filter(tmp_path, failure):
    make, symbols, path = registered(tmp_path)
    one = symbols[0]
    append(path, make, "2026-10-15", sign=1)
    analysis = make("2026-10-16", absent=(one,) if failure == "absent" else (),
                    reference={one: 9.7} if failure == "reference_gap" else None)
    if failure == "ipo_unknown":
        saved = r._json(analysis)
        item = saved["input_previews"][0]
        frame = pd.read_csv(item["feature_file"])
        frame.loc[frame.symbol.eq(one), "listing_qualification_status"] = "unknown"
        frame.to_csv(item["feature_file"], index=False)
        item["feature_sha256"] = r.digest(item["feature_file"])
        analysis.write_text(json.dumps(saved), encoding="utf8")
    r.observe(path, analysis, _clock=clock("2026-10-16T16:00:00+08:00"))
    append(path, make, "2026-10-19")
    report = r.report(path, _clock=clock("2026-10-19T16:01:00+08:00"))
    assert len(report["members"]) == 2
    assert report["members"][0]["status"].startswith("unknown_")
    assert report["members"][0]["provider_relative_condition"] is None
    assert report["members"][0]["raw_price_condition"] is None


def test_same_day_idempotent_but_changed_analysis_or_method_cannot_replace(tmp_path):
    make, _, path = registered(tmp_path)
    analysis = make("2026-10-15", sign=1)
    record = r.observe(path, analysis, _clock=clock("2026-10-15T16:00:00+08:00"))
    file = path.parent / "observations" / "20261015" / "observation.json"
    before = file.read_bytes()
    assert r.observe(path, analysis, _clock=clock("2026-10-15T16:01:00+08:00")) == record
    assert file.read_bytes() == before
    altered = r._json(analysis)
    altered["revised"] = True
    analysis.write_text(json.dumps(altered), encoding="utf8")
    with pytest.raises(ValueError, match="cannot be replaced"):
        r.observe(path, analysis, _clock=clock("2026-10-15T16:02:00+08:00"))
    next_analysis = make("2026-10-16")
    altered = r._json(next_analysis)
    altered["analysis"]["implementation_revision"] = "changed_method"
    next_analysis.write_text(json.dumps(altered), encoding="utf8")
    with pytest.raises(ValueError, match="cannot be mixed"):
        r.observe(path, next_analysis, _clock=clock("2026-10-16T16:00:00+08:00"))
    assert not (path.parent / "observations" / "20261016").exists()


def test_changed_saved_input_or_registry_seal_is_rejected_and_report_is_readonly(tmp_path):
    make, _, path = registered(tmp_path)
    before = {str(p): p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
    report = r.report(path, _clock=clock("2026-10-15T12:01:00+08:00"))
    assert report["status_counts"] == {"waiting_environment_change": 2}
    assert before == {str(p): p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
    analysis = make("2026-10-15")
    input_path = r._json(analysis)["input_previews"][0]["feature_file"]
    with open(input_path, "a", encoding="utf8") as handle:
        handle.write("changed")
    with pytest.raises(ValueError, match="input proof changed"):
        r.observe(path, analysis, _clock=clock("2026-10-15T16:00:00+08:00"))
    changed = r._json(path)
    changed["registered_at"] = "2026-10-14T12:00:00+08:00"
    path.write_text(json.dumps(changed), encoding="utf8")
    with pytest.raises(ValueError, match="seal"):
        r.report(path, _clock=clock("2026-10-15T16:01:00+08:00"))


def test_unknown_identity_or_qualification_cutoff_after_actual_creation_rejected(tmp_path):
    make, _ = fixture(tmp_path)
    analysis = make("2026-10-15")
    saved = r._json(analysis)
    saved["analysis"]["archetypes"][0]["status"] = "unknown"
    analysis.write_text(json.dumps(saved), encoding="utf8")
    with pytest.raises(ValueError, match="known matched"):
        r.register(analysis, tmp_path / "bad", _clock=clock("2026-10-15T16:00:00+08:00"))
    saved["analysis"]["archetypes"][0]["status"] = "matched"
    saved["created_at"] = saved["analysis"]["observed_at"] = "2026-10-15T15:01:00+08:00"
    preview = Path(saved["input_previews"][0]["preview"])
    value = r._json(preview)
    value["created_at"] = saved["created_at"]
    preview.write_text(json.dumps(value), encoding="utf8")
    saved["input_previews"][0]["preview_sha256"] = r.digest(preview)
    analysis.write_text(json.dumps(saved), encoding="utf8")
    with pytest.raises(ValueError, match="decision cutoff"):
        r.register(analysis, tmp_path / "bad", _clock=clock("2026-10-15T15:02:00+08:00"))


@pytest.mark.parametrize("sign", [1, -1])
def test_flat_heavy_scope_retains_core_mixed_phase_even_with_directional_breadth(tmp_path, sign):
    make, _ = fixture(tmp_path)
    analysis = make("2026-10-14")
    saved = r._json(analysis)
    item = saved["input_previews"][0]
    frame = pd.read_csv(item["feature_file"])
    # 27 total: 12 one direction, 2 opposite, 13 flat. Directional breadth
    # exceeds 0.2, but median==0 must remain the core mixed-breadth phase.
    frame["pct_chg"] = [sign] * 12 + [-sign] * 2 + [0] * 13
    frame.to_csv(item["feature_file"], index=False)
    item["feature_sha256"] = r.digest(item["feature_file"])
    env = saved["analysis"]["market_environment"]
    env["phase"] = "mixed_breadth_snapshot"
    env["evidence"].update(median_return_pct=0, breadth=sign * 10 / 27)
    analysis.write_text(json.dumps(saved), encoding="utf8")
    result = r.register(analysis, tmp_path / "cohort", _clock=clock("2026-10-15T12:00:00+08:00"))
    assert result["baseline_environment"]["phase"] == "mixed_breadth_snapshot"


def test_last_observation_body_is_sealed_before_any_successor_can_link_it(tmp_path):
    make, _, path = registered(tmp_path)
    analysis = make("2026-10-15")
    r.observe(path, analysis, _clock=clock("2026-10-15T16:00:00+08:00"))
    file = path.parent / "observations" / "20261015" / "observation.json"
    changed = r._json(file)
    changed["phase"] = "broad_strength_snapshot"
    file.write_text(json.dumps(changed), encoding="utf8")
    with pytest.raises(ValueError, match="Observation chain"):
        r.report(path, _clock=clock("2026-10-15T16:01:00+08:00"))
    with pytest.raises(ValueError, match="Observation chain"):
        r.observe(path, analysis, _clock=clock("2026-10-15T16:01:00+08:00"))
