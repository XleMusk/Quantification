import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.research import leader_environment_risk_diagnostic as diagnostic
from trading.research.leader_environment_policy import STRENGTH, MIXED, WEAKNESS


DAYS = [f"2025-01-{value:02d}" for value in range(2, 9)]


def raw_frame(symbols=("000001.SZSE",)):
    rows = []
    closes = [100, 110, 90, 105, 108, 115, 120]
    lows = [99, 95, 85, 98, 107, 110, 118]
    for symbol in symbols:
        for index, day in enumerate(DAYS):
            op = 100 if index == 1 else closes[index]
            rows.append({"date": day, "symbol": symbol, "open": str(op),
                "high": str(max(op, closes[index]) + 2), "low": str(lows[index]),
                "close": str(closes[index]), "volume": "100000", "tradestatus": "1", "isST": "0"})
    return pd.DataFrame(rows)


def test_stream_projection_preserves_headers_verifies_all_bytes(tmp_path):
    path = tmp_path / "panels.json"
    payload = {day: {"day": day, "available_at": "2026-10-18T01:00:00+08:00",
        "environment": {"source_complete": True, "breadth": .3, "median_return_pct": 1.0},
        "types": [{"other_large_data": {"day": "not-a-top-level-day", "environment": {"breadth": -1}}}]}
        for day in DAYS}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    projected = diagnostic.read_environment_projection(path, diagnostic._digest(path))
    assert list(projected) == DAYS
    assert all(set(row) == {"day", "available_at", "environment"} for row in projected.values())
    assert projected[DAYS[0]]["environment"]["breadth"] == .3
    with pytest.raises(ValueError, match="Pinned environment bytes"):
        diagnostic.read_environment_projection(path, "0" * 64)


def test_exact_open_to_close_adverse_and_close_drawdown():
    views, members = diagnostic.prepare_raw(raw_frame(), DAYS, ["000001.SZSE"])
    eligible, _ = diagnostic.origin_eligibility(views, 0)
    one, _ = diagnostic.outcomes(views, source_days=DAYS, members=members, calendar=DAYS,
        day=DAYS[0], eligible=eligible, horizon=1, period="fixture", primary=True)
    five, saved = diagnostic.outcomes(views, source_days=DAYS, members=members, calendar=DAYS,
        day=DAYS[0], eligible=eligible, horizon=5, period="fixture", primary=True)
    assert one["mean_endpoint_price_change_pct"] == pytest.approx(10)
    assert one["mean_stock_adverse_excursion_pct"] == pytest.approx(-5)
    assert five["mean_endpoint_price_change_pct"] == pytest.approx(15)
    assert five["mean_stock_adverse_excursion_pct"] == pytest.approx(-15)
    assert five["mean_stock_close_drawdown_pct"] == pytest.approx((90 / 110 - 1) * 100)
    assert five["execution_day"] == DAYS[1] and five["horizon_end_day"] == DAYS[5]
    assert saved["raw_open_E"] == [100.0]


def test_halts_missing_rows_and_future_ST_never_rewrite_D_universe():
    symbols = ["000001.SZSE", "000002.SZSE", "000003.SZSE"]
    raw = raw_frame(symbols)
    raw.loc[(raw.symbol == symbols[0]) & (raw.date == DAYS[2]), "tradestatus"] = "0"
    raw = raw.loc[~((raw.symbol == symbols[1]) & (raw.date == DAYS[3]))]
    raw.loc[(raw.symbol == symbols[2]) & (raw.date >= DAYS[1]), "isST"] = "1"
    views, members = diagnostic.prepare_raw(raw, DAYS, symbols)
    eligible, counts = diagnostic.origin_eligibility(views, 0)
    assert counts == {"eligible": 3}
    summary, stocks = diagnostic.outcomes(views, source_days=DAYS, members=members, calendar=DAYS,
        day=DAYS[0], eligible=eligible, horizon=5, period="fixture", primary=True)
    assert summary["D_eligible_count"] == 3
    assert summary["known_complete_path_count"] == 1
    assert stocks["status"] == ["inactive_or_halted_path", "missing_raw_row", "known_complete_raw_price_path"]
    assert stocks["endpoint_price_change_pct"][:2] == [None, None]
    assert summary["future_ST_observed_count"] == 1
    assert stocks["endpoint_price_change_pct"][2] == pytest.approx(15)


def test_calendar_missing_session_and_D_unknown_not_zero():
    raw = raw_frame(("000001.SZSE", "000002.SZSE"))
    raw.loc[(raw.symbol == "000002.SZSE") & (raw.date == DAYS[0]), "isST"] = ""
    source = [day for day in DAYS if day != DAYS[3]]
    raw = raw.loc[raw.date.isin(source)]
    views, members = diagnostic.prepare_raw(raw, source, ["000001.SZSE", "000002.SZSE"])
    eligible, counts = diagnostic.origin_eligibility(views, 0)
    assert eligible.tolist() == [True, False]
    assert counts["D_unknown_ST_status"] == 1
    summary, stocks = diagnostic.outcomes(views, source_days=source, members=members, calendar=DAYS,
        day=DAYS[0], eligible=eligible, horizon=5, period="fixture", primary=True)
    assert summary["known_complete_path_count"] == 0
    assert summary["breadth"] is None
    assert stocks["status"] == ["missing_exact_source_day"]


def test_aggregation_uses_origin_means_and_never_shifts_nonoverlap_origin():
    origins = []
    for index, (phase, val, count) in enumerate([(STRENGTH, -1, 1), (STRENGTH, -9, 100),
            (MIXED, -10, 50), (WEAKNESS, -20, 50), ("unknown", -30, 10), (WEAKNESS, -4, 2)]):
        row = {"known_complete_path_count": count, "unknown_path_count": 0,
            "D_eligible_count": count, "status_counts": {"known_complete_raw_price_path": count}}
        row.update({metric: val for metric in diagnostic.SUMMARY_METRICS})
        origins.append({"phase": phase, "two_close_confirmation": phase != "unknown",
            "mode": "NO_CHANGE", "five_session_primary_origin": index % 5 == 0,
            "outcomes": {"1": row, "5": row}})
    summary = diagnostic.summarize_origins(origins, 1, confirmed_only=True)
    strength = summary["groups"][0]
    assert strength["origin_count"] == 2
    assert strength["origin_mean"]["mean_stock_adverse_excursion_pct"] == -5
    assert strength["known_stock_origin_observations_not_independent_N"] == 101
    primary = diagnostic.summarize_origins(origins, 5, nonoverlap=True, confirmed_only=True)
    assert primary["origin_count_before_phase_filter"] == 2
    assert primary["groups"][1]["origin_count"] == 0
    assert primary["confirmed_risk_ordering_descriptive_only"]["breadth"] is None


def test_registered_small_source_runs_without_accounts_and_refuses_repeat(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    raw = source / "raw.parquet"; raw_frame().to_parquet(raw, index=False)
    panels = source / "panels.json"
    panels.write_text(json.dumps({day: {"day": day, "available_at": "2026-10-18T01:00:00+08:00",
        "environment": {"source_complete": True, "breadth": .3, "median_return_pct": 1.0}, "types": []}
        for day in DAYS}, indent=2), encoding="utf-8")
    calendar = source / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2025,
        "closed_ranges": [], "open_sessions": DAYS}), encoding="utf-8")
    manifest = source / "manifest.json"
    manifest.write_text(json.dumps({"schema": diagnostic.INPUT_SCHEMA, "source_days": DAYS,
        "scope_members": ["000001.SZSE"], "execution_admitted": False, "created_at": "2026-10-18T01:00:00+08:00",
        "start": DAYS[0], "end": DAYS[1], "through": DAYS[-1], "raw_file": str(raw),
        "raw_sha256": diagnostic._digest(raw), "raw_rows": len(DAYS), "panels_file": str(panels),
        "panels_sha256": diagnostic._digest(panels), "calendar": str(calendar),
        "calendar_sha256": diagnostic._digest(calendar)}), encoding="utf-8")
    registered = tmp_path / "registered"
    diagnostic.register([("fixture", manifest)], registered)
    output = tmp_path / "out"
    result = diagnostic.run(registered / "registration.json", output)
    assert result["status"] == "completed_conditional_saved_environment_risk_diagnostic"
    assert result["engine_advanced"] is False and result["account_created"] is False
    origins = json.loads(Path(result["periods"][0]["origins_file"]).read_text(encoding="utf-8"))
    assert origins[0]["two_close_confirmation"] is False
    assert origins[1]["two_close_confirmation"] is True
    assert origins[0]["five_session_primary_origin"] is True
    assert origins[1]["five_session_primary_origin"] is False
    assert not list(output.rglob("account.json"))
    with pytest.raises(FileExistsError):
        diagnostic.run(registered / "registration.json", output)


def test_open_sessions_are_overrides_not_the_full_exchange_calendar():
    sessions = diagnostic.calendar_sessions({"schema": "cn_a_share_exchange_calendar_v1", "year": 2023,
        "closed_ranges": ["2023-01-01/2023-01-02", "2023-04-05/2023-04-05"], "open_sessions": []})
    assert sessions[0] == "2023-01-03"
    assert "2023-01-07" not in sessions
    assert "2023-04-05" not in sessions
    assert "2023-04-06" in sessions
