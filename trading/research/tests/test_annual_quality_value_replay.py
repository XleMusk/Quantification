"""New retrospective runner contracts, with no market data or real Engine runs."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.research import annual_quality_value_replay as m


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return m.pin(path)


def fixture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(m, "DATA_ROOT", data)
    all_days = pd.bdate_range(m.START, m.END).strftime("%Y-%m-%d").tolist()
    removed = set([day for day in all_days if 5 <= int(day[8:]) <= 20][:len(all_days) - m.SESSIONS])
    days = [day for day in all_days if day not in removed]
    decisions = [day for day in m.month_ends(days, m.END) if m.START <= day <= m.END]
    assert len(days) == 424 and len(decisions) == 20
    source = tmp_path / "old_value/run.json"
    quality = tmp_path / "quality/result.json"
    payload = tmp_path / "old_value/saved_pool.parquet"
    source_ref = write(source, {"protocol": {"stock_manifest": {"fixture_original_manifest": True}}})
    quality_ref = write(quality, {"fixture_original_annual_data": True})
    payload.write_bytes(b"fixture saved payload, not a real parquet")
    meta = {"source_run": str(source), "source_refs": {"source": source_ref, "quality": quality_ref},
        "payload_refs": {"pool": m.pin(payload)}, "stock_inputs": {key: str(tmp_path / key) for key in
            ("price_view", "event_view", "reference_view", "listing_master")},
        "config": m.portfolio().to_dict(), "calendar": days, "replay_days": days,
        "decision_days": decisions, "source_clocks": {"quality_asof": "2026-10-05T06:10:26+08:00"}}
    monkeypatch.setattr(m.inputs, "inspect_saved_inputs", lambda *a: deepcopy(meta))
    symbols = [f"{600001+i:06d}.SSE" for i in range(30)]
    frame = pd.DataFrame([{"date": day, "rank": i + 1, "symbol": symbol, "score": 30.0-i}
        for day in decisions for i, symbol in enumerate(symbols)])
    status = {day: {"status": "FULL_TARGET", "reason": None, "joint_pool_members": 30} for day in decisions}
    return SimpleNamespace(data=data, source=source, quality=quality, meta=meta,
        days=days, decisions=decisions, symbols=symbols, ranks=frame, status=status)


def test_register_only_saves_original_pins_code_and_note_before_any_new_scores(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(m.inputs, "build_inputs", lambda *a, **k: pytest.fail("Register cannot build new scores"))
    monkeypatch.setattr(m, "BulkResearchFeed", lambda *a, **k: pytest.fail("Register cannot open the replay feed"))
    monkeypatch.setattr(m, "baseline_ledger_result", lambda *a, **k: pytest.fail("Register cannot advance Engine"))
    note = tmp_path / "notes/source_note.json"
    write(note, {"paper_method_only": True})
    folder = f.data / "registration"
    record = m.register(f.source, f.quality, folder, source_note=note)
    assert record["engine_advanced"] is False and record["new_scores_built"] is False
    assert record["source_note"] == m.pin(note)
    assert record["spec"]["end"] == m.END and record["spec"]["sessions"] == 424
    assert record["source"]["source_clocks"]["quality_asof"] == "2026-10-05T06:10:26+08:00"
    assert record["source"]["config"]["single_limit"] == .2
    assert m._registration(folder / "registration.json") == record
    note.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="source, note or code snapshot"):
        m._registration(folder / "registration.json")


def test_one_feed_four_new_cases_seal_first_and_case_failure_does_not_stop_others(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    registration = f.data / "declaration"
    m.register(f.source, f.quality, registration)
    out = f.data / "new_replay"
    built = {"meta": deepcopy(f.meta), "full_pool": f.ranks.copy(), "pool": f.ranks.copy(),
        "coverage": [{"date": day, "joint_pool_members": 30} for day in f.decisions],
        "status_by_day": f.status, "ranks": {case: f.ranks.copy() for case in m.CASES}}
    calls = {"feed": 0, "close": 0, "cases": []}

    def build(value_run, quality_result, *, expected_meta):
        assert Path(value_run) == f.source and Path(quality_result) == f.quality
        assert expected_meta == f.meta
        return built

    class Base:
        def __init__(self, *args):
            calls["feed"] += 1
            assert (out / "formation_seal.json").is_file()
            self.calendar, self.symbols = f.days, f.symbols
            self.manifest = {"fixture_original_manifest": True}

        def close(self):
            calls["close"] += 1

    def baseline(feed, config, dates, destination):
        case = destination.name
        calls["cases"].append(case)
        assert list(dates) == f.days and config == m.portfolio()
        assert m.read(out / "formation_seal.json")["engine_advanced"] is False
        assert isinstance(feed.signal_frame(f.decisions[0], config, RunContext("fixture", TradingMode.REPLAY)), SignalFrame)
        assert not destination.exists()
        destination.mkdir(parents=True)
        failed = case == "value_only"
        no_fills = case == "matched_momentum"
        state = {"day": f.days[99] if failed else m.END, "cash": 14000000,
            "orders": {} if no_fills else {"fixture_parent": {"qty": 100}},
            "fills": {} if no_fills else {"fixture_callback": {"qty": 100, "fees": 500}},
            "positions": {} if no_fills else {f.symbols[0]: [{"qty": 100, "available": f.days[1]}]},
            "pending_targets": {"fixture_preserved": .04} if failed else None}
        write(destination / "account.json", state)
        write(destination / "replay.json", {"status": "failed" if failed else "completed"})
        return {"status": "failed" if failed else "completed", "completed_sessions": 100 if failed else 424,
            "error": "fixture_unknown_held_event" if failed else None,
            "net_return": None if failed else 0 if no_fills else -.01,
            "max_drawdown": None if failed else -.02, "fees": None if failed else 5}

    monkeypatch.setattr(m.inputs, "build_inputs", build)
    monkeypatch.setattr(m, "BulkResearchFeed", Base)
    monkeypatch.setattr(m, "baseline_ledger_result", baseline)
    result = m.run(registration / "registration.json", out)
    assert calls == {"feed": 1, "close": 1, "cases": list(m.CASES)}
    assert result["status"] == "partial_retrospective_research_accounts"
    assert result["cases"][0]["complete_PnL_available"] is False and result["cases"][0]["metrics"] is None
    assert result["cases"][1]["metrics"] is None and result["cases"][1]["pending_targets"] is not None
    assert result["cases"][2]["metrics"]["net_return"] == -.01
    assert result["cases"][1]["saved_fees_rmb"] == 5
    assert all(row["net_return_difference"] is None for row in result["comparisons"]
        if row["control"] in ("value_only", "matched_momentum"))
    with pytest.raises(FileExistsError, match="exclusive"):
        m.run(registration / "registration.json", out)


def test_saved_joint_unknown_is_NO_CHANGE_without_filler_or_empty_FULL(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    unknown = f.decisions[0]
    statuses = deepcopy(f.status)
    statuses[unknown] = {"status": "NO_CHANGE", "reason": "joint_pool_below30", "joint_pool_members": 0}
    folder = tmp_path / "saved_ranks"
    folder.mkdir()
    f.ranks.loc[f.ranks.date.ne(unknown)].to_csv(folder / "rankings.csv", index=False)
    base = SimpleNamespace(calendar=f.days, symbols=f.symbols)
    config = m.portfolio()
    feed = m.AnnualQualityValueSavedFeed(base, folder, "fixture", config, f.days, statuses)
    assert isinstance(feed.signal_frame(unknown, config, RunContext("fixture", TradingMode.REPLAY)), SignalUnavailable)
    assert isinstance(feed.signal_frame(f.decisions[1], config, RunContext("fixture", TradingMode.REPLAY)), SignalFrame)
    with pytest.raises(ValueError, match="same replay"):
        feed.signal_frame(unknown, config, RunContext("fixture", TradingMode.PAPER))
    statuses.pop(f.decisions[-1])
    with pytest.raises(ValueError, match="every original month end"):
        m.AnnualQualityValueSavedFeed(base, folder, "fixture", config, f.days, statuses)


def test_feed_manifest_allows_only_safe_new_metadata_and_rejects_every_old_change():
    original = {"scope": "original_full_scope", "canonical_codes": 5369,
        "listing_master": "original/provider_instruments.csv", "execution_admitted": False,
        "event_view": "original_events", "reference_view": "original_refs",
        "limitations": ["original limitation"], "historical_price_adjustments": [{"symbol": "600001.SSE", "cash": ".1"}]}
    safe = {**deepcopy(original), "full_canonical_codes": 5369,
        "listing_master_kind": "legacy_provider_listing", "research_scope": None,
        "research_scope_kind": None, "research_scope_excluded_codes": 0}
    assert m._manifest_comparison(original, safe)["compatible"] is True
    for key, value in original.items():
        changed = deepcopy(safe)
        changed[key] = {"changed_original_value": value}
        result = m._manifest_comparison(original, changed)
        assert result["compatible"] is False and key in result["changed_original_keys"]
    # Equal-valued Python bool/int must not bypass exact original JSON types.
    changed = {**deepcopy(safe), "execution_admitted": 0}
    assert m._manifest_comparison(original, changed)["compatible"] is False
    for key, value in (("full_canonical_codes", 5368), ("listing_master_kind", "instrument_master_v1"),
            ("research_scope", "new_scope"), ("research_scope_kind", "new_kind"),
            ("research_scope_excluded_codes", False), ("unknown_added_key", True)):
        changed = {**deepcopy(safe), key: value}
        assert m._manifest_comparison(original, changed)["compatible"] is False
