import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.contracts import SignalUnavailable
from trading.research import weekly_liquidity_inputs as inputs
from trading.research import weekly_reversal as weekly
from trading.research.low_risk import portfolio
from trading.research.price_boundaries import RENAME_SHA256
from trading.rules import stock_board


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def saved(tmp_path):
    # Deliberately tiny cross-section. The first 242 source sessions reproduce
    # the original formation warmup relationship without loading market data.
    before = pd.bdate_range("2023-01-03", "2023-12-29").strftime("%Y-%m-%d").tolist()
    calendar = before[:241] + [before[-1]]
    calendar += pd.bdate_range(weekly.START, weekly.END).strftime("%Y-%m-%d").tolist()
    decisions = weekly.decision_days(calendar)
    last = decisions[-1]
    previous = calendar[calendar.index(last) - 1]
    codes = ["sh.600000", "sh.600004", "sh.600007", "sh.689009", "sz.302132"]
    universe = codes + ["sz.300114"]
    price, raw, event, listing = (tmp_path / name for name in ("price", "raw", "event", "listing.csv"))
    for folder in (price, raw, event):
        folder.mkdir()
    panel = price / "canonical_daily.parquet"
    writer = None
    try:
        for i, day in enumerate(calendar):
            rows = []
            for j, code in enumerate(codes):
                if code == "sh.600007" and day == previous:
                    continue  # Exact missing session, never a next-available observation.
                close = str(10 * np.exp(.0003 * i + .015 * np.sin(i * .17 + j)))
                row = {"date": day, "code": code, "close": close, "volume": "1000000",
                       "amount": "50000000", "turn": "0.500000", "tradestatus": "1",
                       "isST": "0", "adjustflag": "2", "alias_shadow": False}
                if code == "sh.600004" and day == last:
                    row.update(close="", volume="0", amount="0", turn="", tradestatus="0")
                if code == "sz.302132" and day == previous:
                    row["turn"] = "-1"
                if code == "sh.600000" and day == last:
                    row["turn"] = "0"
                rows.append(row)
            table = pa.Table.from_pylist(rows)
            if writer is None:
                writer = pq.ParquetWriter(panel, table.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    plan = {"days": calendar, "kind": "stock", "execution_admitted": False}
    write(raw / "plan.json", plan)
    write(raw / "receipts.json", {"updated_at": "2026-10-01T15:03:23+08:00"})
    rawsha = "a" * 64
    raw_result = {"status": "source_cache_complete", "kind": "stock", "missing_days": [],
                  "expected_days": len(calendar), "acquired_days": len(calendar),
                  "execution_admitted": False, "raw_daily_sha256": rawsha,
                  "plan_sha256": inputs._digest(raw / "plan.json"),
                  "receipts_sha256": inputs._digest(raw / "receipts.json")}
    write(raw / "result.json", raw_result)
    rename = {"old": "sz.300114", "new": "sz.302132", "effective_date": "2025-02-17",
              "published_date": "2025-02-14", "disclosure_sha256": RENAME_SHA256,
              "share_count_unchanged_on_rename": True, "pre_rename_forward_scale": ".986265"}
    result = {"status": "research_reconstruction_completed", "raw_view": str(raw),
              "execution_admitted": False, "cutoff": weekly.END, "dates": len(calendar),
              "canonical_panel_file": panel.name, "canonical_panel_sha256": inputs._digest(panel),
              "canonical_symbols": len(codes), "canonical_rows": len(calendar) * len(codes) - 1,
              "source_hashes": {"raw": rawsha}, "rename": rename,
              "started_at": "2026-10-01T15:34:51+08:00", "finished_at": "2026-10-01T15:53:21+08:00"}
    write(price / "result.json", result)
    pd.DataFrame({"date": calendar}).to_csv(event / "source_input_calendar.csv", index=False)
    pd.DataFrame({"code": universe}).to_csv(event / "source_input_universe.csv", index=False)
    pd.DataFrame([{"code": c, "ipoDate": "2020-01-01", "outDate": "", "type": "1", "status": "1"}
                  for c in universe]).to_csv(listing, index=False)
    unsupported = {}
    for code in codes:
        symbol = inputs.normalize_symbol(code).vt_symbol
        try:
            stock_board(symbol)
        except ValueError as error:
            unsupported[symbol] = str(error)
    config = portfolio().to_dict()
    source = {"price_view": str(price), "raw_view": str(raw), "event_view": str(event),
              "listing_master": str(listing), "source_codes": len(universe), "canonical_codes": len(codes),
              "unsupported_instruments": unsupported, "limitations": ["synthetic non-PIT source"]}
    run = {"status": "completed_weekly_reversal_diagnostic", "research_family": "weekly_reversal",
           "scope": inputs.BULK_SCOPE, "symbols": len(codes), "start": weekly.START, "end": weekly.END,
           "sessions": len([d for d in calendar if weekly.START <= d <= weekly.END]),
           "strategy_validated": False, "live_ready": False, "inputs": source,
           "completed_at": "2026-10-03T11:50:00+08:00",
           "protocol": {"id": "five_session_reversal_momentum_pair_entry_notices_v3",
                        "signals": list(weekly.NOTICE_VARIANTS), "portfolio": config,
                        "signal_portfolio_configs": {s: config for s in weekly.NOTICE_VARIANTS},
                        "bulk_inputs": source, "decision_cadence": "every_five_sessions_anchored_20240102",
                        "execution_admitted": False, "tuning": False},
           "results": [{"signal": s, "status": "completed",
                        "completed_sessions": len([d for d in calendar if weekly.START <= d <= weekly.END])}
                       for s in weekly.NOTICE_VARIANTS]}
    runfile = tmp_path / "run.json"
    write(runfile, run)
    return {"run": runfile, "price": price, "panel": panel, "calendar": calendar,
            "decisions": decisions, "last": last, "previous": previous, "raw": raw}


def test_one_projected_read_original_pool_and_unavailable_warmup(saved, monkeypatch):
    original = pq.ParquetFile
    reads = []

    class ProjectedOnce:
        def __init__(self, path):
            self.value = original(path)

        def __getattr__(self, key):
            return getattr(self.value, key)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.value.close()

        def read(self, **kwargs):
            reads.append(kwargs["columns"])
            return self.value.read(**kwargs)

        def read_row_group(self, *args, **kwargs):
            pytest.fail("The loader must not repeat per-day panel reads")

    monkeypatch.setattr(inputs.pq, "ParquetFile", ProjectedOnce)
    before = {p: p.read_bytes() for p in saved["run"].parent.rglob("*") if p.is_file()}
    loaded = inputs.load_inputs_from_saved_weekly(saved["run"])
    assert reads == [list(inputs.BASE_COLUMNS) + ["alias_shadow"]]
    assert loaded["decision_days"] == saved["decisions"]
    first = loaded["decision_days"][:2]
    assert all(isinstance(loaded["pools"][d], SignalUnavailable) for d in first)
    assert all(loaded["pools"][d].reason == "low_risk_252_session_warmup" for d in first)
    assert {r["symbol"] for r in loaded["pools"][saved["last"]]} == {"600000.SSE", "302132.SZSE"}
    assert "300114.SZSE" not in loaded["base"].symbols
    assert "689009.SSE" in loaded["base"].unsupported_instruments
    day = saved["last"]
    close = loaded["base"].closes["600000.SSE"]
    i = saved["calendar"].index(day)
    row = next(r for r in loaded["pools"][day] if r["symbol"] == "600000.SSE")
    assert row["momentum"] == pytest.approx(close.iloc[i-21] / close.iloc[i-252] - 1)
    assert row["reversal"] == pytest.approx(-(close.iloc[i] / close.iloc[i-5] - 1))
    assert row["median_amount"] == 50000000
    assert loaded["load_observation"]["canonical_data_reads"] == 1
    assert loaded["load_observation"]["raw_data_reads"] == 0
    assert {p: p.read_bytes() for p in before} == before
    assert set(p for p in saved["run"].parent.rglob("*") if p.is_file()) == set(before)


def test_reported_percent_halt_blank_and_exact_missing_session_stay_unknown(saved):
    loaded = inputs.load_inputs_from_saved_weekly(saved["run"])
    last, previous = saved["last"], saved["previous"]
    assert loaded["reported_turnover"].at[last, "600000.SSE"] == 0
    assert loaded["turnover_raw"].at[last, "600000.SSE"] == "0"
    assert loaded["reported_turnover"].at[previous, "600000.SSE"] == .5
    assert pd.isna(loaded["reported_turnover"].at[last, "600004.SSE"])
    assert loaded["turnover_observation"].at[last, "600004.SSE"] == "blank_unknown"
    assert pd.isna(loaded["turnover_active"].at[last, "600004.SSE"])
    assert loaded["turnover_raw"].at[previous, "302132.SZSE"] == "-1"
    assert pd.isna(loaded["reported_turnover"].at[previous, "302132.SZSE"])
    assert loaded["turnover_observation"].at[previous, "302132.SZSE"] == "negative_unknown"
    assert not loaded["row_present"].at[previous, "600007.SSE"]
    assert loaded["turnover_observation"].at[previous, "600007.SSE"] == "missing_row_unknown"
    assert pd.isna(loaded["features"]["volatility"].at[last, "600007.SSE"])
    assert loaded["source"]["reported_turn_unit"] == "percent"
    assert loaded["source"]["independent_circulating_shares_verified"] is False


def test_metadata_only_before_data_read_and_changed_panel_rejected(saved, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata inspection must not open the price panel")
    monkeypatch.setattr(inputs.pq, "ParquetFile", forbidden)
    source = inputs.inspect_saved_weekly_inputs(saved["run"])
    assert source["canonical_panel"]["sha256"] == inputs._digest(saved["panel"])
    assert source["raw_file_read"] is False
    with saved["panel"].open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="panel bytes changed"):
        inputs.load_inputs_from_saved_weekly(saved["run"])
