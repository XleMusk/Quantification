"""Official positive status vetoes quote contradictions without filling unknowns."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.contracts import RunContext, TradingMode
from trading.native_intraday_feed import NativeQuoteFeed, import_quote, load_quote
from trading.research import current_risk_warning_source


D, E = "2026-10-15", "2026-10-16"
MEMBER, OTHER = "002103.SZSE", "600000.SSE"
AVAILABLE = E + "T10:50:00+08:00"
AT = E + "T10:51:00+08:00"


def _stamp(value):
    return datetime.fromisoformat(value)


def _source(tmp_path, monkeypatch):
    source = tmp_path / "official-positive"
    source.mkdir()
    receipt = source / "receipt.json"
    receipt.write_text(json.dumps({"fixture": "complete-same-day-positive"}), encoding="utf-8")
    state = {"available": True}

    def load(path, *, as_of):
        assert Path(path).resolve() == source.resolve()
        ready = state["available"] and _stamp(AVAILABLE) <= as_of
        return {
            "status": "saved_same_day_official_positive_risk_warning_report" if ready else "unavailable",
            "day": E,
            "source_available_at": AVAILABLE,
            "ordinary_risk_symbols": [MEMBER] if ready else [],
            "full_report_complete": ready,
            "current_ST_false_inference_admitted": False,
            "tradable_inference_admitted": False,
        }

    monkeypatch.setattr(current_risk_warning_source, "load", load)
    return source, receipt, state


def _csv(tmp_path, name, *, member_st="1", other_st="0"):
    path = tmp_path / (name + ".csv")
    path.write_text(
        "symbol,market,price_rmb,preclose_rmb,is_st,tradable\n"
        f"002103,SZSE,10.00,9.90,{member_st},0\n"
        f"600000,SSE,12.34,12.20,{other_st},1\n",
        encoding="utf-8",
    )
    return path


def _import(csv, out, source, *, at=AT, phase="opening"):
    return import_quote(
        csv, source="synthetic-export", source_kind="public_export",
        quote_asof=at, trading_day=E, phase=phase, out=out, now=at,
        risk_warning_source=source,
    )


def test_positive_veto_keeps_original_prices_status_and_protects_the_source(tmp_path, monkeypatch):
    source, _, _ = _source(tmp_path, monkeypatch)
    conflict = _csv(tmp_path, "conflict", member_st="0")
    original = conflict.read_bytes()
    rejected = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError, match="non-ST claim conflicts"):
        _import(conflict, rejected, source)
    assert not rejected.exists() and conflict.read_bytes() == original

    csv = _csv(tmp_path, "accepted")
    path = tmp_path / "accepted.json"
    record = _import(csv, path, source)
    saved, ref = load_quote(path, "opening", E, _stamp(AT))
    assert saved == record
    assert saved["prices"][MEMBER] == {
        "price_rmb": "10.00", "preclose_rmb": "9.90", "source": "synthetic-export",
        "is_st": True, "tradable": False,
    }
    assert saved["prices"][OTHER]["is_st"] is False
    assert saved["prices"][OTHER]["tradable"] is True
    check = saved["official_positive_risk_warning_check"]
    assert check["positive_quote_symbols"] == [MEMBER]
    assert check["non_ST_or_tradable_inference_admitted"] is False
    assert check["original_quote_prices_and_status_unchanged"] is True
    assert saved["broker_quote"] is False and saved["execution_admitted"] is False

    # A nonmember with no ST field stays unknown when that field is optional at close.
    closing_csv = _csv(tmp_path, "closing", other_st="")
    closing = _import(closing_csv, tmp_path / "closing.json", source,
                      at=E + "T15:01:00+08:00", phase="closing")
    assert "is_st" not in closing["prices"][OTHER]
    assert closing["prices"][MEMBER]["is_st"] is True

    prepared = SimpleNamespace(
        calendar=[D, E], loaded={"source_day": D, "execution_day": E,
                                 "record": {"policy_id": "synthetic-policy"}},
        prior={"original_rows": {MEMBER: {"volume": "100000"}, OTHER: {"volume": "200000"}}},
        qualification=SimpleNamespace(records={MEMBER: {"ipo_date": "2000-01-01"},
                                                OTHER: {"ipo_date": "2000-01-01"}}),
        input_paths=[],
    )
    feed = NativeQuoteFeed(prepared, saved, ref, "opening", RunContext("risk-fixture:lr", TradingMode.PAPER))
    assert source.resolve() in [Path(p).resolve() for p in feed.input_paths]


def test_bound_receipt_csv_and_source_availability_are_rechecked(tmp_path, monkeypatch):
    source, receipt, state = _source(tmp_path, monkeypatch)
    csv = _csv(tmp_path, "bound")
    path = tmp_path / "bound.json"
    _import(csv, path, source)
    original_receipt, original_csv = receipt.read_bytes(), csv.read_bytes()

    receipt.write_text('{"fixture":"changed"}', encoding="utf-8")
    with pytest.raises(ValueError, match="official positive risk-warning evidence changed"):
        load_quote(path, "opening", E, _stamp(AT))
    receipt.write_bytes(original_receipt)

    csv.write_bytes(original_csv.replace(b"10.00", b"10.01"))
    with pytest.raises(ValueError, match="raw CSV changed"):
        load_quote(path, "opening", E, _stamp(AT))
    csv.write_bytes(original_csv)

    state["available"] = False
    with pytest.raises(ValueError, match="evidence is unavailable"):
        load_quote(path, "opening", E, _stamp(AT))
    state["available"] = True
    premature = tmp_path / "premature.json"
    with pytest.raises(ValueError, match="evidence is unavailable"):
        _import(csv, premature, source, at=E + "T10:49:59+08:00")
    assert not premature.exists()
