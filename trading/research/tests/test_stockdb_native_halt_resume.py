"""Native halt quotes and versioned continuation semantics, using tiny local fixtures."""
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import stockdb_rolling_low_volatility as rolling


LEGACY = "volume_derived_v1"
NATIVE = "native_supplement_status_v2"
VARIANT = "low_vol_half_momentum_20"
FIRST, HALT = "2026-09-30", "2026-10-08"


def bar(day=FIRST, code="000582", volume=100_000.):
    return dict(code=code, symbol=code + (".SZSE" if code.startswith("0") else ".SSE"),
                date=day, open=8.97, high=8.97, low=8.97, close=8.97,
                pre_close=8.97, volume=volume, amount=897_000., is_st=False)


def snapshot(path, rows):
    path.mkdir()
    bars = path / "bars.parquet"
    rows[list(rolling.SOURCE_COLUMNS)].to_parquet(bars, index=False)
    manifest = dict(status="complete_inventory_only", execution_admitted=False,
                    start=FIRST, chunks_expected=1,
                    chunks=[dict(parquet_file=bars.name,
                                 parquet_sha256=sha256(bars.read_bytes()).hexdigest())])
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def supplement(path, *, status="0", adjustment="3", volume="", amount=""):
    path.mkdir()
    raw = dict(date=HALT, code="sz.000582", open="8.9700", high="8.9700",
               low="8.9700", close="8.9700", preclose="8.9700", volume=volume,
               amount=amount, tradestatus=status, adjustflag=adjustment, isST="0")
    pd.DataFrame([raw]).to_parquet(path / "raw_daily.parquet", index=False)
    return path


def read_halt(tmp_path, **kwargs):
    source = snapshot(tmp_path / "source", pd.DataFrame([
        bar(), bar(HALT, "600000"), bar(FIRST, "600000", volume=0.)]))
    raw = supplement(tmp_path / "supplement", **kwargs)
    return rolling._read_snapshot(source, raw, end=HALT)[2]


def test_native_halt_preserves_explicit_status_adjustment_and_unknown_amounts(tmp_path):
    frame = read_halt(tmp_path)
    native = frame.loc[frame.symbol.eq("000582.SZSE") & frame.date.eq(HALT)].iloc[0]
    assert str(native["tradestatus"]) == "0"
    assert str(native["adjustflag"]) == "3"
    assert np.isnan(native.volume) and np.isnan(native.amount)
    feed = rolling.RollingStockDBFeed(frame, HALT, variant=VARIANT)
    quote = feed.rows.get(HALT)["000582.SZSE"]
    assert quote["tradestatus"] == 0 and quote["adjustflag"] == "3"
    assert np.isnan(quote["volume"]) and np.isnan(quote["amount"])
    assert feed.rows.get(FIRST)["600000.SSE"]["tradestatus"] == 0
    assert feed.rows.get(HALT)["600000.SSE"]["tradestatus"] == 1


@pytest.mark.parametrize("status,volume,expected", [("0", "100000", 0), ("1", "", 1)])
def test_native_effective_status_is_not_inferred_from_volume(tmp_path, status, volume, expected):
    frame = read_halt(tmp_path, status=status, volume=volume)
    feed = rolling.RollingStockDBFeed(frame, HALT, variant=VARIANT)
    assert feed.rows.get(HALT)["000582.SZSE"]["tradestatus"] == expected


def test_overlapping_raw_view_only_annotates_execution_reference_without_replacing_stockdb_price(tmp_path):
    source = snapshot(tmp_path / "source", pd.DataFrame([
        bar(), bar(HALT, "000582", volume=321_000.),
    ]))
    raw = tmp_path / "supplement"
    raw.mkdir()
    pd.DataFrame([{
        "date": HALT, "code": "sz.000582", "open": "7.00", "high": "7.00",
        "low": "7.00", "close": "7.00", "preclose": "7.00", "volume": "",
        "amount": "", "tradestatus": "0", "adjustflag": "3", "isST": "0",
    }]).to_parquet(raw / "raw_daily.parquet", index=False)
    _, _, frame, receipt = rolling._read_snapshot(source, raw, end=HALT)
    row = frame.loc[frame.symbol.eq("000582.SZSE") & frame.date.eq(HALT)].iloc[0]
    assert row["close"] == pytest.approx(8.97)
    assert row["volume"] == pytest.approx(321_000.)
    assert str(row["tradestatus"]) == "0" and str(row["adjustflag"]) == "3"
    assert receipt["rows_added"] == 0 and receipt["rows_annotated"] == 1


@pytest.mark.parametrize("adjustment", ["1", "2", "", None])
def test_native_nonraw_or_unknown_adjustment_is_rejected(tmp_path, adjustment):
    with pytest.raises(ValueError, match="adjustflag|raw quote"):
        read_halt(tmp_path, adjustment=adjustment)


@pytest.mark.parametrize("status", ["", "2", None])
def test_native_status_must_be_explicit_zero_or_one(tmp_path, status):
    with pytest.raises(ValueError, match="tradestatus|explicit status"):
        read_halt(tmp_path, status=status)


def test_engine_marks_exact_native_halt_without_a_fill_or_imputed_volume(tmp_path):
    frame = read_halt(tmp_path)
    feed = rolling.RollingStockDBFeed(frame, HALT, variant=VARIANT)
    config = rolling.portfolio()
    state = new_state(config.initial_cash)
    state["day"] = FIRST
    state["positions"]["000582.SZSE"] = [dict(qty=600, available=FIRST)]
    state["prices"]["000582.SZSE"] = 897
    state["pending_targets"] = {"000582.SZSE": 0.0}
    # No signal ranking is needed to verify the native halt execution/mark path.
    from trading.contracts import SignalUnavailable
    feed.signal_frame = lambda *args: SignalUnavailable("fixture_no_signal")
    result = advance(state, feed, HALT, config, 0,
                     context=RunContext("fixture-native-halt", TradingMode.REPLAY))
    assert not state["orders"] and not state["fills"]
    assert state["prices"]["000582.SZSE"] == 897
    assert state["positions"]["000582.SZSE"][0]["qty"] == 600
    assert "000582.SZSE" in result["stale_symbols"]
    assert any(e["kind"] == "order_blocked" and e.get("reason") == "suspended"
               for e in state["events"])
    assert np.isnan(feed.volumes.at[HALT, "000582.SZSE"])


@pytest.mark.parametrize("volume,status", [(100_000., "1"), (0., "0"), (np.nan, "0")])
def test_legacy_prefix_accepts_only_equal_effective_status(volume, status):
    before = pd.DataFrame([bar(volume=volume)])
    after = before.assign(tradestatus=status, adjustflag="3")
    rolling._validate_resume_prefix(before, after, HALT, HALT, FIRST)


def test_legacy_prefix_rejects_real_status_change_despite_identical_prices():
    before = pd.DataFrame([bar()])
    after = before.assign(tradestatus="0", adjustflag="3")
    with pytest.raises(ValueError, match="quote semantics prefix"):
        rolling._validate_resume_prefix(before, after, HALT, HALT, FIRST)


def test_legacy_raw_status_cannot_be_rewritten_to_match_old_volume_inference():
    before = pd.DataFrame([bar()]).assign(tradestatus="0", adjustflag="3")
    after = before.assign(tradestatus="1")
    with pytest.raises(ValueError, match="native quote semantics prefix"):
        rolling._validate_resume_prefix(before, after, HALT, HALT, FIRST)


def test_native_prefix_preserves_status_independently_of_volume():
    before = pd.DataFrame([bar()]).assign(tradestatus="0", adjustflag="3")
    rolling._validate_resume_prefix(before, before.copy(), HALT, HALT, FIRST,
                                    previous_quote_semantics=NATIVE)
    after = before.assign(tradestatus="1")
    with pytest.raises(ValueError, match="quote semantics prefix"):
        rolling._validate_resume_prefix(before, after, HALT, HALT, FIRST,
                                        previous_quote_semantics=NATIVE)
    with pytest.raises(ValueError, match="quote semantics"):
        rolling._validate_resume_prefix(before, before, HALT, HALT, FIRST,
                                        previous_quote_semantics="unknown")


def test_native_suffix_append_does_not_change_legacy_price_calendar_or_signal_prefix():
    before = pd.DataFrame([bar(), bar(HALT, "600000")])
    after = pd.concat([before, pd.DataFrame([bar(HALT)]).assign(
        volume=np.nan, amount=np.nan, tradestatus="0", adjustflag="3")], ignore_index=True)
    rolling._validate_resume_prefix(before, after, HALT, HALT, FIRST)
    changed = after.copy()
    changed.loc[changed.date.eq(FIRST), "close"] += .01
    with pytest.raises(ValueError, match="raw history prefix"):
        rolling._validate_resume_prefix(before, changed, HALT, HALT, FIRST)
    with pytest.raises(ValueError, match="calendar prefix"):
        rolling._validate_resume_prefix(before, after.loc[after.date.ne(FIRST)], HALT, HALT, FIRST)
    # A later month cannot retrospectively confirm an already processed signal day.
    with pytest.raises(ValueError, match="month-end signal prefix"):
        rolling._validate_resume_prefix(pd.DataFrame([bar()]),
            pd.DataFrame([bar(), bar(HALT)]), FIRST, HALT, FIRST)


def test_new_runs_record_native_quote_semantics_without_touching_source(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    source = snapshot(tmp_path / "source", pd.DataFrame([bar()]))
    original = (source / "manifest.json").read_bytes()
    out, record = rolling.run(source, FIRST, FIRST, variants=(VARIANT,))
    protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["quote_semantics"] == record["quote_semantics"] == NATIVE
    assert (source / "manifest.json").read_bytes() == original


def test_saved_native_semantics_is_used_by_actual_resume_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    source = snapshot(tmp_path / "source", pd.DataFrame([
        bar(FIRST, "600000"), bar(HALT)]))
    raw = supplement(tmp_path / "supplement", volume="100000", amount="897000")
    rows = pd.read_parquet(raw / "raw_daily.parquet")
    rows["date"] = FIRST
    rows.to_parquet(raw / "raw_daily.parquet", index=False)
    out, saved = rolling.run(source, FIRST, FIRST, variants=(VARIANT,), supplemental_raw_view=raw)
    account = (Path(saved["artifacts"]) / VARIANT / "account.json").read_bytes()
    # Explicit status 0 differs from inferred status 1, so this succeeds only if
    # the loader actually uses the saved v2 contract rather than its v1 default.
    _, same = rolling.run(source, FIRST, FIRST, variants=(VARIANT,),
                           supplemental_raw_view=raw, resume_run=out / "run.json")
    assert same["status"] == "no_new_session"
    assert (Path(saved["artifacts"]) / VARIANT / "account.json").read_bytes() == account
    changed = tmp_path / "changed_supplement"
    changed.mkdir()
    rows["tradestatus"] = "1"
    rows.to_parquet(changed / "raw_daily.parquet", index=False)
    with pytest.raises(ValueError, match="native quote semantics prefix"):
        rolling.run(source, FIRST, FIRST, variants=(VARIANT,),
                    supplemental_raw_view=changed, resume_run=out / "run.json")
