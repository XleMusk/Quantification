"""One local input assembly, with exact dated prefixes and unchanged raw values."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_eight_type_inputs as inputs
from trading.research import leader_dated_turnover
from trading.research.baostock_daily_slice import digest


DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]
A, B, C = "000001.SZSE", "000002.SZSE", "000003.SZSE"


@pytest.fixture
def source(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    payload = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
    calendar.write_text(json.dumps(payload))
    sessions = inputs._calendar_view(payload, "2026-01-01", "2026-12-31", [])[0]
    q = SimpleNamespace(calendar=tuple(sessions), verification={"synthetic": True})
    history = tmp_path / "history"
    history.mkdir()
    (history / "manifest.json").write_text("{\"synthetic\":true}")
    protocol = tmp_path / "protocol.json"
    frozen = {"schema": "leader_eight_type_fixed_comparison_protocol_v1", "case_count": 32,
        "config": inputs._config(100000, 5).to_dict(), "cases": [{"id": f"{identity}__{guard}__{cap}"}
            for identity in inputs.TYPE_IDS for guard in inputs.GUARDS for cap, _ in inputs.CAPS],
        **{key: False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score")}}
    protocol.write_text(json.dumps(frozen))
    actual, loaded = {}, {}
    proofs = [{"day": day, "source_folder": str(tmp_path / day), "source_updated_at": "2026-10-15T11:00:00+08:00",
        "raw_daily_sha256": day} for day in DAYS]
    for index, day in enumerate(DAYS):
        raw = pd.DataFrame([{"date": day, "code": "sz." + symbol[:6], "open": "10", "high": "10.1", "low": "9.9",
            "close": "10", "preclose": "10", "volume": "" if symbol == C else "1000000",
            "amount": "" if symbol == C else "100000000", "pctChg": "" if symbol == C else "0",
            "tradestatus": "0" if symbol == C else "1", "isST": "1" if symbol == B else "0", "adjustflag": "3"}
            for symbol in (A, B, C)])
        features = pd.DataFrame([{"date": day, "symbol": A, "amount": 100000000., "listing_qualification_status": "supported",
            "sma20": 9., "sma60": 8., "ret20": .1, "ret60": .2, "amount_rank_pct": 1.}])
        lineage = {"shared_view_id": "fixed", "base_history_max_observed_day": "2026-10-09",
            "original_history_snapshot": str(history), "original_history_manifest_sha256": digest(history / "manifest.json"),
            "source_proofs": proofs[:index + 1]}
        envelope = {"day": day, "created_at": "2026-10-15T12:00:00+08:00", "calendar": str(calendar),
            "source": {"source_folder": str(tmp_path / day)}, "tape": {"breadth": .5, "median_return_pct": 1.}}
        loaded[day] = (envelope, features, {"recent_source_view": lineage, "feature_qualification": {"synthetic": True}})
        actual[str(tmp_path / day)] = (raw, {"ordinary_roster": [{"symbol": symbol} for symbol in (A, B, C)], "day": day})
    calls = {"preview": [], "missing": [], "environment": []}
    def preview(folder, **kwargs):
        calls["preview"].append(folder)
        return deepcopy(loaded[folder])
    monkeypatch.setattr(inputs, "read_preview", preview)
    monkeypatch.setattr(inputs, "load_complete", lambda folder: deepcopy(actual[folder]))
    monkeypatch.setattr(inputs, "source_proof", lambda raw, metadata, h: deepcopy(proofs[DAYS.index(metadata["day"])]))
    monkeypatch.setattr(inputs, "load_ipo_qualification", lambda *a, **k: q)
    monkeypatch.setattr(inputs, "_now", lambda: datetime.fromisoformat("2026-10-15T18:00:00+08:00"))
    monkeypatch.setattr(inputs, "report", lambda p: {"registry": "registry", "registry_sha256": "identity", "read_at": "2026-10-15T17:30:00+08:00",
        "members": [{"symbol": A}], "registered_at": "2026-10-15T11:54:00+08:00"})
    monkeypatch.setattr(inputs, "_industry", lambda *a: {"membership": pd.DataFrame({"symbol": [A, B, C], "industry": ["x"] * 3}),
        "source_file": "industry", "source_sha256": "mapping", "snapshot_date": "2026-09-30", "available_at": "2026-10-04T12:00:00+08:00",
        "theme_membership": False, "member_count": 3})
    def environment(features, envelopes, calendar, observed, **kwargs):
        calls["environment"].append([value["day"] for value in envelopes])
        return {"day": envelopes[-1]["day"], "archetypes": [{"id": identity,
            "status": "matched" if identity in inputs.OLD_TYPES else "unknown", "candidates": [{"symbol": A, "amount_cny": 100000000.}] if identity in inputs.OLD_TYPES else [],
            "unresolved_stage_symbols": [], "unresolved_rebound_symbols": []} for identity in inputs.TYPE_IDS]}
    monkeypatch.setattr(inputs, "build_environment", environment)
    def missing(**kwargs):
        day = kwargs["day"]
        calls["missing"].append(kwargs)
        return {"day": day, "types": [{"id": identity, "status": "unknown", "candidates": [],
            "unresolved_symbols": [A], "availability": {"source_available_at": "2026-10-15T12:00:00+08:00"}}
            for identity in inputs.TYPE_IDS if identity not in inputs.OLD_TYPES]}
    monkeypatch.setattr(inputs, "build_missing_type_candidates", missing)
    monkeypatch.setattr(leader_dated_turnover, "load_turnover_source", lambda path, **kwargs: {"day": str(path),
        "available_at": "2026-10-15T17:28:00+08:00", "unit": "percent", "units_verified": True, "rows": []})
    return {"calendar": calendar, "protocol": protocol, "loaded": loaded, "actual": actual, "calls": calls, "out": tmp_path / "inputs"}


def run(source, turnovers=DAYS[:2]):
    return inputs.assemble(DAYS, source["calendar"], "ipo", "master", "registry", "industry", turnovers,
        source["protocol"], source["out"])


def test_assembly_uses_first_full_roster_and_each_day_own_prefix_keeps_halt_nulls(source):
    result = run(source)
    assert result["scope_members"] == [A, B, C] and result["scope_frozen_at"] == "2026-10-15T18:00:00+08:00"
    assert source["calls"]["preview"] == DAYS
    assert source["calls"]["environment"] == [DAYS[:1], DAYS[:2], DAYS]
    for index, call in enumerate(source["calls"]["missing"]):
        assert [view["day"] for view in call["complete_views"]] == DAYS[:index + 1]
        assert all(batch["day"] <= call["day"] for batch in call["turnover_batches"])
        assert call["scope_members"] == [A, B, C] and call["identity_tracking"]["registry_sha256"] == "identity"
    saved = pd.read_parquet(result["raw_file"])
    assert len(saved) == 9 and saved.loc[saved.code.eq("sz.000003"), "volume"].tolist() == [""] * 3
    assert result["protocol_sha256"] == digest(source["protocol"])
    assert [row["source_days_used"] for row in result["panels"]] == [DAYS[:1], DAYS[:2], DAYS]
    assert all([item["id"] for item in row["types"]] == list(inputs.TYPE_IDS) for row in result["panels"])
    assert result["old_features_recomputed"] is result["account_created"] is result["orders_generated"] is False
    assert result["panels"][0]["types"][0]["known_exclusions"][C] == "known_current_ST_or_suspension"
    assert (source["out"] / "identity_report.json").is_file() and (source["out"] / "manifest.json").is_file()


@pytest.mark.parametrize("problem", ["mixed_view", "protocol_cost", "duplicate_turn"])
def test_incoherent_or_ambiguous_inputs_fail_before_output(source, problem):
    if problem == "mixed_view":
        source["loaded"][DAYS[1]][2]["recent_source_view"]["shared_view_id"] = "different"
    elif problem == "protocol_cost":
        record = json.loads(source["protocol"].read_text())
        record["config"]["slippage_bps"] = 0
        source["protocol"].write_text(json.dumps(record))
    with pytest.raises(ValueError):
        run(source, [DAYS[0]] * 2 if problem == "duplicate_turn" else DAYS[:2])
    assert not source["out"].exists()


def test_missing_old_fixed_feature_is_unknown_not_known_empty():
    features = pd.DataFrame([{"symbol": A, "amount": 100000000., "listing_qualification_status": "supported",
        "sma20": None, "sma60": 9., "ret20": .1, "ret60": .2}])
    raw = pd.DataFrame([{"code": "sz.000001", "isST": "0", "tradestatus": "1"}])
    result = inputs._old_type({"id": "trend_leader", "candidates": [], "status": "not_matched"}, features, raw, [A])
    assert result["unresolved_symbols"] == [A] and result["scope_complete"] is False


def test_industry_loader_keeps_entire_saved_membership_and_actual_known_time(tmp_path):
    folder = tmp_path / "industry"
    folder.mkdir()
    path = folder / "2026-09-30.parquet"
    pd.DataFrame({"date": ["2026-09-30"] * 3, "symbol": [A, B, C], "industry": ["x", "x", "y"],
        "industryClassification": ["broad"] * 3, "updateDate": ["2026-09-30"] * 3}).to_parquet(path)
    record = {"status": "completed_requested_industry_history", "source": "BaoStock.query_stock_industry(date=decision_day)",
        "execution_admitted": False, "entries": [{"date": "2026-09-30", "file": path.name, "rows": 3,
        "retrieved_at": "2026-10-04T12:00:00+08:00"}]}
    (folder / "result.json").write_text(json.dumps(record))
    result = inputs._industry(path, "2026-10-15T18:00:00+08:00")
    assert result["membership"].symbol.tolist() == [A, B, C]
    assert result["source_sha256"] == digest(path) and result["available_at"] == record["entries"][0]["retrieved_at"]
    assert result["theme_membership"] is result["revision_safe_PIT_verified"] is False
    with pytest.raises(ValueError, match="actually been retrieved"):
        inputs._industry(path, "2026-10-03T18:00:00+08:00")
