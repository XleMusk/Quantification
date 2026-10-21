"""Synthetic availability evidence; real shared Engine/Ledger, never market replay."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.research import seat_hypothesis_panel_replay as module


DAYS = ["2025-09-25", "2025-09-26", "2025-09-29", "2025-09-30",
        "2025-10-09", "2025-10-10", "2025-10-13", "2025-10-14"]
A, B = "600000.SSE", "000001.SZSE"


def _digest(file):
    return sha256(Path(file).read_bytes()).hexdigest()


def _json(file, value):
    Path(file).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def evidence(tmp_path):
    def make(name="case", *, events=None, clocks=None, label_value=0):
        root = tmp_path / name
        inputs, sources = root / "inputs", root / "source_packets"
        inputs.mkdir(parents=True)
        sources.mkdir()
        events = events if events is not None else [(DAYS[0], A), (DAYS[2], B), (DAYS[4], A)]
        candidates = inputs / "candidates.csv"
        pd.DataFrame([{"trade_date": day, "symbol": symbol,
            "net_buy_to_reported_turnover": .03 if symbol == A else .04,
            "hypothesis_h1": True, "D5_CLOSE_ADJCHRATE": label_value,
            "run_max": label_value, "hit5": bool(label_value)} for day, symbol in events]).to_csv(candidates, index=False)
        bars = inputs / "bars.csv"
        pd.DataFrame([{"symbol": symbol, "date": day, "available": True,
            "open": 10., "high": 10.2, "low": 9.8, "close": 10.,
            "volume": 1_000_000, "amount": 10_000_000., "pre_close": 10., "is_st": False}
            for day in DAYS for symbol in (A, B)]).to_csv(bars, index=False)
        audit = inputs / "source_bar_pair_audit.json"
        _json(audit, {"schema": "seat_candidate_source_bar_pair_audit_v1",
            "status": "completed_research_only", "execution_admitted": False,
            "summary": {"paired_rows": len(DAYS) * 2,
                "price_stats": {k: {"mismatch_rows": 0} for k in ("open", "high", "low", "close", "pre_close")},
                "amount": {"mismatch_rows_gt_0_1pct": 0}}})
        clocks = clocks if clocks is not None else {
            (DAYS[0], A): (DAYS[0] + "T16:00:00+08:00", None),
            (DAYS[2], B): (None, None),
        }
        records = []
        for index, (day, symbol) in enumerate(events):
            source = sources / f"synthetic_packet_{index}.json"
            _json(source, {"synthetic_test_only": True, "event_date": day, "symbol": symbol})
            if (day, symbol) in clocks:
                retrieved, published = clocks[(day, symbol)]
                records.append({"event_date": day, "symbol": symbol, "source_file": str(source.resolve()),
                    "source_sha256": _digest(source), "retrieved_at": retrieved, "published_at": published})
        sidecar = inputs / "availability.json"
        record = {"schema": "seat_candidate_availability_evidence_v1",
            "candidates_file": str(candidates.resolve()), "candidates_sha256": _digest(candidates),
            "execution_admitted": False, "historical_publication_verified": False, "records": records}
        _json(sidecar, record)
        return SimpleNamespace(root=root, candidates=candidates, bars=bars, audit=audit,
            sidecar=sidecar, record=record, out=root / "new_replay_output")
    return make


def _replay(item, lag=1):
    return module.replay(item.candidates, item.bars, lag=lag,
        source_audit=item.audit, availability_evidence=item.sidecar)


def _feed(item, lag=1):
    return module.load_panel(item.candidates, item.bars, lag=lag,
        availability_evidence=item.sidecar)[0]


def test_real_ledger_holds_after_missing_event_and_unknown_availability(evidence):
    item = evidence()
    feed = _feed(item)
    assert feed.availability["known_clock_events"] == 1
    assert feed.availability["unknown_clock_events"] == 2
    schedule = {(r["event_date"], r["symbol"]): r for r in feed.availability["schedule"]}
    assert schedule[(DAYS[2], B)]["reason"] == "retrieved_at_unknown"
    assert schedule[(DAYS[4], A)]["reason"] == "availability_record_missing"
    config = module._config(100000, 5)
    context = RunContext("synthetic-seat-held-NC", TradingMode.REPLAY)
    assert isinstance(feed.signal_frame(DAYS[3], config, context), SignalUnavailable)
    assert isinstance(feed.signal_frame(DAYS[5], config, context), SignalUnavailable)
    result = _replay(item)
    buys = [o for o in result["orders"] if o["side"] == "buy"]
    assert buys and {o["symbol"] for o in buys} == {A}
    assert {o["day"] for o in buys} == {DAYS[2]}
    assert not [o for o in result["orders"] if o["side"] == "sell"]
    quantity = sum(lot["qty"] for lot in result["state"]["positions"][A])
    assert quantity == sum(fill["qty"] for fill in result["fills"]) > 0
    assert result["state"]["cash"] < 10_000_000
    assert sum(fill["fees"] for fill in result["fills"]) > 0
    assert all(fill["available_day"] == DAYS[3] for fill in result["fills"])
    assert all(day["holdings"] == 1 for day in result["daily"][1:])
    assert result["future_labels_used_for_score"] is False
    assert result["execution_admitted"] is False
    assert result["selection_enabled"] is False


@pytest.mark.parametrize("lag", [1, 2])
def test_late_retrieval_or_reported_publication_waits_until_observed_close(evidence, lag):
    # Retrieval is known earlier; the later reported publication is not a verified archive.
    item = evidence(events=[(DAYS[0], A)], clocks={
        (DAYS[0], A): (DAYS[0] + "T16:00:00+08:00", DAYS[3] + "T15:01:00+08:00")})
    feed = _feed(item, lag)
    assert set(feed.signals) == {DAYS[4]}
    scheduled = feed.availability["schedule"][0]
    assert scheduled["evidence"]["available_at"] == DAYS[3] + "T15:01:00+08:00"
    assert scheduled["evidence"]["clock_basis"] == "reported_published_after_retrieval_not_independently_verified"
    assert scheduled["derived_signal_available_at"] == DAYS[4] + "T15:00:00+08:00"
    assert feed.availability["historical_publication_verified"] is False
    result = _replay(item, lag)
    assert result["orders"] and {o["day"] for o in result["orders"]} == {DAYS[5]}
    assert all(o["side"] == "buy" for o in result["orders"])


def test_terminal_window_and_all_unknown_remain_no_decision_not_zero_return(evidence):
    terminal = evidence("terminal", events=[(DAYS[0], A)], clocks={
        (DAYS[0], A): (DAYS[-1] + "T14:00:00+08:00", None)})
    unknown = evidence("unknown", events=[(DAYS[0], A)], clocks={})
    for item in (terminal, unknown):
        feed = _feed(item)
        row = feed.availability["schedule"][0]
        assert row["status"] == ("withheld" if item is terminal else "unknown")
        assert row["signal_day"] is None and row["execution_day"] is None
        frame = feed.signal_frame(DAYS[-1], module._config(100000, 5), RunContext("synthetic-terminal", TradingMode.REPLAY))
        assert isinstance(frame, SignalUnavailable)
        result = _replay(item)
        assert not result["orders"] and not result["fills"]
        assert result["metrics"] is None


def test_future_label_columns_never_change_signal_or_economic_ledger(evidence):
    left, right = evidence("labels_zero", label_value=0), evidence("labels_changed", label_value=999)
    assert _feed(left).signals == _feed(right).signals
    a, b = _replay(left), _replay(right)
    for key in ("orders", "fills"):
        assert a[key] == b[key]
    for key in ("cash", "positions"):
        assert a["state"][key] == b["state"][key]


@pytest.mark.parametrize("change", ["candidate_hash", "source_hash", "unrelated_event", "duplicate",
    "naive_retrieved", "future_retrieved", "naive_published", "future_published"])
def test_tampered_or_unsupported_availability_rejected(evidence, change):
    item = evidence()
    record = deepcopy(item.record)
    if change == "candidate_hash":
        item.candidates.write_bytes(item.candidates.read_bytes() + b"\n")
    elif change == "source_hash":
        source = Path(record["records"][0]["source_file"])
        source.write_bytes(source.read_bytes() + b"\n")
    elif change == "unrelated_event":
        record["records"][0]["event_date"] = DAYS[1]
    elif change == "duplicate":
        record["records"].append(deepcopy(record["records"][0]))
    elif change.startswith("naive"):
        record["records"][0]["retrieved_at" if change.endswith("retrieved") else "published_at"] = "2025-09-25T18:00:00"
    else:
        record["records"][0]["retrieved_at" if change.endswith("retrieved") else "published_at"] = "2099-01-01T18:00:00+08:00"
    _json(item.sidecar, record)
    with pytest.raises(ValueError):
        _feed(item)
    assert not item.out.exists()


def test_default_lag_mapping_remains_explicitly_unchanged(evidence):
    item = evidence(events=[(DAYS[0], A)], clocks={
        (DAYS[0], A): (DAYS[3] + "T15:01:00+08:00", None)})
    old = module.load_panel(item.candidates, item.bars, lag=1)[0]
    new = _feed(item)
    assert set(old.signals) == {DAYS[1]}
    assert set(new.signals) == {DAYS[4]}
    assert old.manifest["hypothesis_id"] == module.HYPOTHESIS_ID


def test_cli_writes_new_unknown_result_without_pretending_zero_return(evidence, capsys):
    item = evidence(events=[(DAYS[0], A)], clocks={})
    assert module.main(["--candidates", str(item.candidates), "--bars", str(item.bars),
        "--source-audit", str(item.audit), "--availability-evidence", str(item.sidecar),
        "--lag", "1", "--out", str(item.out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    result = json.loads((item.out / "result.json").read_text(encoding="utf-8"))
    assert printed["net_return"] is None and result["metrics"] is None
    assert result["hypothesis_id"] == module.AVAILABILITY_HYPOTHESIS_ID
    assert not result["orders"] and not result["fills"]
    assert (item.out / "REPORT.md").exists()


def test_nested_output_rejected_before_engine_and_sources_unchanged(evidence, monkeypatch):
    item = evidence()
    original = {file: file.read_bytes() for file in (item.candidates, item.bars, item.audit, item.sidecar)}
    def unexpected_engine(*args, **kwargs):
        raise AssertionError("Engine must not run when output would contaminate the input package")
    monkeypatch.setattr(module, "advance", unexpected_engine)
    nested = item.candidates.parent / "must_not_exist"
    with pytest.raises(ValueError):
        module.main(["--candidates", str(item.candidates), "--bars", str(item.bars),
            "--source-audit", str(item.audit), "--availability-evidence", str(item.sidecar),
            "--lag", "1", "--out", str(nested)])
    assert not nested.exists()
    assert all(file.read_bytes() == data for file, data in original.items())
