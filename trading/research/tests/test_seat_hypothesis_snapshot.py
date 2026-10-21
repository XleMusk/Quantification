"""Tiny ready-cache snapshots plus one explicit synthetic shared-Engine integration."""
from copy import deepcopy
import gzip
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pandas as pd
import pytest

from trading.research import seat_hypothesis_snapshot as module
from trading.research import seat_hypothesis_panel_replay as consumer
from trading.research.seats import REPORTS, URL


DAY = "2025-09-26"
A = "600000.SSE"
SINGLE = "日涨幅偏离值达到7%的前5只证券"
SECOND_SINGLE = "日换手率达到20%的前5只证券"
MULTI = "连续三个交易日内涨幅偏离值累计达到20%的证券"
PAGE_CLOCKS = {"summary": "2025-09-26T17:01:00+08:00", "buy": "2025-09-26T17:02:00+08:00",
               "sell": "2025-09-26T17:03:00+08:00"}
COMPLETED = "2025-09-26T17:04:00+08:00"


def _hash(file):
    return sha256(Path(file).read_bytes()).hexdigest()


def _save(file, value):
    Path(file).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _seat(code, buy, sell, *, symbol="600000.SH", turnover=1000., name=None, reason=SINGLE):
    return {"TRADE_DATE": DAY + " 00:00:00", "SECUCODE": symbol, "SECURITY_CODE": symbol[:6],
        "EXPLANATION": reason, "OPERATEDEPT_NAME": name or f"Synthetic broker branch {code}",
        "OPERATEDEPT_CODE": code, "BUY": buy, "SELL": sell, "NET": buy - sell,
        "ACCUM_AMOUNT": turnover, "TRADE_ID": "synthetic-single-board", "RISE_PROBABILITY_3DAY": 999,
        "D5_CLOSE_ADJCHRATE": 999}


@pytest.fixture(autouse=True)
def no_runtime_integrations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Snapshot tests may not use HTTP or SQLite")
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)


@pytest.fixture
def cache(tmp_path):
    def make(name="case", *, extra_buy=(), extra_sell=(), extra_summaries=(),
             seat_turnovers=(1000., 1000.), summary_turnovers=(1000., 1000.),
             summary_reasons=(SINGLE, SECOND_SINGLE), page_clocks=None, completed=COMPLETED):
        root = tmp_path / name
        source = root / "ready_cache"
        source.mkdir(parents=True)
        base = [_seat("named-a", 200., 20., turnover=seat_turnovers[0]),
                _seat("named-b", 150., 10., turnover=seat_turnovers[1])]
        # Duplicate listings across two reasons and both sides are original raw observations.
        buys = base + [{**r, "EXPLANATION": SECOND_SINGLE} for r in base] + list(extra_buy)
        sells = deepcopy(base) + [{**r, "EXPLANATION": SECOND_SINGLE} for r in base] + list(extra_sell)
        summaries = [{"TRADE_DATE": DAY, "SECUCODE": "600000.SH", "SECURITY_CODE": "600000",
            "EXPLANATION": reason, "ACCUM_AMOUNT": turnover} for reason, turnover in zip(summary_reasons, summary_turnovers)]
        summaries += [{"TRADE_DATE": DAY, "SECUCODE": "600000.SH", "SECURITY_CODE": "600000",
            "EXPLANATION": MULTI, "ACCUM_AMOUNT": 3000.}] + list(extra_summaries)
        rows = {"summary": summaries, "buy": buys, "sell": sells}
        clocks = {**PAGE_CLOCKS, **(page_clocks or {})}
        for kind, values in rows.items():
            sort = "TRADE_DATE,SECURITY_CODE" + (",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL" if kind != "summary" else "")
            document = {"request": {"reportName": REPORTS[kind], "columns": "ALL",
                "filter": f"(TRADE_DATE>='{DAY}')(TRADE_DATE<='{DAY}')", "pageSize": 500, "pageNumber": 1,
                "sortColumns": sort, "sortTypes": ",".join("1" for _ in sort.split(",")), "source": "WEB", "client": "WEB"},
                "response": {"success": True, "result": {"pages": 1, "count": len(values), "data": values}},
                "retrieved_at": clocks[kind], "published_at": None}
            with gzip.open(source / f"{kind}_0001.json.gz", "wt", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False)
        manifest = {"query": {"source": "Eastmoney", "url": URL, "start": DAY, "end": DAY,
            "codes": [], "pagination_version": 2}, "status": "ready",
            "reports": {kind: {"pages": 1, "rows": len(values)} for kind, values in rows.items()},
            "publication_timestamps_available": False, "completeness": "provider_pages_only", "completed_at": completed}
        _save(source / "manifest.json", manifest)
        return SimpleNamespace(source=source, out=root / "new_snapshot", manifest=manifest,
            original={p.name: p.read_bytes() for p in source.iterdir() if p.is_file()})
    return make


def _panels(item):
    result = module.build(item.source, item.out)
    return result, pd.read_csv(item.out / "legacy_repeated_sum/panel.csv"), pd.read_csv(item.out / "stockday_unique/panel.csv")


def _event(panel, prefix="600000"):
    rows = panel.loc[panel.symbol.str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_fixed_denominators_deduplicate_raw_listings_and_archive_exact_bytes(cache):
    item = cache()
    result, legacy, unique = _panels(item)
    old, new = _event(legacy), _event(unique)
    assert old.eligible_seat_events == new.eligible_seat_events == 2
    assert old.buy_seats == new.buy_seats == 2
    assert old.net_buy_cny == new.net_buy_cny == 320.
    assert old.turnover_cny == 2000. and new.turnover_cny == 1000.
    assert old.net_buy_to_reported_turnover == pytest.approx(.16)
    assert new.net_buy_to_reported_turnover == pytest.approx(.32)
    assert bool(old.hypothesis_h1) and bool(new.hypothesis_h1)
    archive = item.out / "source_bundle.zip"
    with ZipFile(archive) as saved:
        names = saved.namelist()
        assert saved.read("archive_manifest.json") == (item.out / "source_bundle_manifest.json").read_bytes()
        for basename, original in item.original.items():
            matched = [name for name in names if Path(name).name == basename]
            assert len(matched) == 1 and saved.read(matched[0]) == original
    assert all((item.source / name).read_bytes() == data for name, data in item.original.items())
    for folder, expected_id, expected_schema in (("legacy_repeated_sum", "lhb_identifiable_buy_concentration_v1", "seat_candidate_availability_evidence_v1"),
            ("stockday_unique", module.STOCKDAY_HYPOTHESIS_ID, module.STOCKDAY_AVAILABILITY_SCHEMA)):
        proof = json.loads((item.out / folder / "availability_evidence.json").read_text(encoding="utf-8"))
        candidates = item.out / folder / "candidates.csv"
        assert Path(proof["candidates_file"]) == candidates.resolve()
        assert proof["candidates_sha256"] == _hash(candidates)
        assert Path(proof["panel_file"]) == (item.out / folder / "panel.csv").resolve()
        assert proof["panel_sha256"] == _hash(item.out / folder / "panel.csv")
        assert proof["hypothesis_id"] == proof["snapshot_method_id"] == expected_id
        assert proof["schema"] == expected_schema
        assert len(proof["records"]) == 1
        row = proof["records"][0]
        assert row["event_date"] == DAY and row["symbol"] == A
        assert Path(row["source_file"]) == archive.resolve() and row["source_sha256"] == _hash(archive)
        assert row["retrieved_at"] == COMPLETED and row["published_at"] is None
        assert row["raw_ids"]
        assert proof["execution_admitted"] is False and proof["historical_publication_verified"] is False
    assert not any(field in legacy.columns or field in unique.columns for field in ("RISE_PROBABILITY_3DAY", "D5_CLOSE_ADJCHRATE"))
    assert result["execution_admitted"] is False


@pytest.mark.parametrize("case", ["buy_sell_negative", "sell_only_positive"])
def test_stockday_net_once_differs_from_legacy_listed_side_components(cache, case):
    if case == "buy_sell_negative":
        negative = _seat("named-negative", 10., 60.)
        item = cache(extra_buy=[negative], extra_sell=[deepcopy(negative)])
        expected_old, expected_new = 220., 270.
    else:
        item = cache(extra_sell=[_seat("named-sell-only-positive", 80., 20.)])
        expected_old, expected_new = 320., 380.
    _, legacy, unique = _panels(item)
    old, new = _event(legacy), _event(unique)
    assert old.eligible_seat_events == new.eligible_seat_events == 3
    assert old.net_buy_cny == expected_old and new.net_buy_cny == expected_new
    assert old.turnover_cny == 3000. and new.turnover_cny == 1000.
    assert new.net_buy_to_reported_turnover == pytest.approx(expected_new / 1000.)
    assert new.buy_seats == 2


def test_aggregate_generic_and_nonordinary_instruments_never_enter_v2_candidates(cache):
    aggregate = _seat("category", 900., 0., name="自然人")
    generic = _seat("0", 700., 0., name="机构专用")
    nonordinary, summaries = [], []
    for symbol in ("127096.SZ", "920001.BJ"):
        nonordinary.extend([_seat("other-a", 200., 20., symbol=symbol), _seat("other-b", 150., 10., symbol=symbol)])
        summaries.append({"TRADE_DATE": DAY, "SECUCODE": symbol, "SECURITY_CODE": symbol[:6],
            "EXPLANATION": SINGLE, "ACCUM_AMOUNT": 1000.})
    item = cache(extra_buy=[aggregate, generic] + nonordinary,
        extra_sell=[deepcopy(aggregate), deepcopy(generic)] + deepcopy(nonordinary), extra_summaries=summaries)
    _, legacy, unique = _panels(item)
    assert _event(legacy).net_buy_cny == _event(unique).net_buy_cny == 320.
    assert _event(unique).eligible_seat_events == 2
    for prefix in ("127096", "920001"):
        assert _event(unique, prefix).hypothesis_status == "scope_excluded"
    candidates = pd.read_csv(item.out / "stockday_unique/candidates.csv")
    assert candidates.symbol.tolist() == [A]


@pytest.mark.parametrize("case", ["seat_conflict", "summary_conflict", "summary_missing", "multi_only"])
def test_stockday_denominator_conflict_or_missing_stays_unknown_not_max_or_zero(cache, case):
    kwargs = {"seat_turnovers": (1000., 1200.)} if case == "seat_conflict" else (
        {"summary_turnovers": (1000., 1200.)} if case == "summary_conflict" else
        {"summary_turnovers": (None, None)} if case == "summary_missing" else
        {"summary_reasons": (MULTI, MULTI)})
    item = cache(**kwargs)
    _, _, unique = _panels(item)
    row = _event(unique)
    assert row.hypothesis_status == "unknown" and str(row.unknown_reasons)
    assert pd.isna(row.turnover_cny) and pd.isna(row.net_buy_to_reported_turnover)
    assert pd.read_csv(item.out / "stockday_unique/candidates.csv").empty


def test_latest_page_clock_not_manifest_mtime_controls_common_retrieval(cache):
    later = "2025-09-26T17:06:00+08:00"
    item = cache(page_clocks={"sell": later})
    _panels(item)
    for mode in ("legacy_repeated_sum", "stockday_unique"):
        proof = json.loads((item.out / mode / "availability_evidence.json").read_text(encoding="utf-8"))
        assert all(row["retrieved_at"] == later and row["published_at"] is None for row in proof["records"])


@pytest.mark.parametrize("case", ["partial", "missing_page", "naive_page_clock", "future_completed"])
def test_cache_not_ready_or_invalid_clock_rejected_without_output(cache, case):
    item = cache()
    manifest = deepcopy(item.manifest)
    if case == "partial":
        manifest["status"] = "collecting"
        _save(item.source / "manifest.json", manifest)
    elif case == "missing_page":
        (item.source / "buy_0001.json.gz").unlink()
    elif case == "future_completed":
        manifest["completed_at"] = "2099-01-01T17:00:00+08:00"
        _save(item.source / "manifest.json", manifest)
    else:
        page = item.source / "buy_0001.json.gz"
        with gzip.open(page, "rt", encoding="utf-8") as stream:
            document = json.load(stream)
        document["retrieved_at"] = "2026-10-15T17:02:00"
        with gzip.open(page, "wt", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False)
    with pytest.raises((ValueError, FileNotFoundError)):
        module.build(item.source, item.out)
    assert not item.out.exists()


def test_existing_output_is_not_overwritten_and_nested_cache_output_rejected(cache):
    item = cache()
    item.out.mkdir()
    marker = item.out / "user_file.txt"
    marker.write_bytes(b"unchanged")
    with pytest.raises(FileExistsError):
        module.build(item.source, item.out)
    assert marker.read_bytes() == b"unchanged"
    nested = item.source / "derived_snapshot"
    with pytest.raises(ValueError):
        module.build(item.source, nested)
    assert not nested.exists()


def test_v2_snapshot_uses_real_engine_and_old_schema_cannot_self_promote(cache):
    item = cache()
    _panels(item)
    bars = item.out.parent / "separate_synthetic_bars.csv"
    pd.DataFrame([{"symbol": A, "date": day, "available": True, "open": 10., "high": 10.2,
        "low": 9.8, "close": 10., "volume": 1_000_000, "amount": 10_000_000.,
        "pre_close": 10., "is_st": False} for day in (DAY, "2025-09-29", "2025-09-30", "2025-10-09")]).to_csv(bars, index=False)
    candidates = item.out / "stockday_unique/candidates.csv"
    sidecar = item.out / "stockday_unique/availability_evidence.json"
    feed, _, symbols, _ = consumer.load_panel(candidates, bars, lag=1, availability_evidence=sidecar)
    assert symbols == [A]
    assert feed.hypothesis_id == consumer.STOCKDAY_AVAILABILITY_ID
    assert feed.availability["historical_publication_verified"] is False
    audit = item.out.parent / "synthetic_source_bar_pair_audit.json"
    _save(audit, {"schema": "seat_candidate_source_bar_pair_audit_v1", "status": "completed_research_only",
        "execution_admitted": False, "summary": {"paired_rows": 4,
            "price_stats": {field: {"mismatch_rows": 0} for field in ("open", "high", "low", "close", "pre_close")},
            "amount": {"mismatch_rows_gt_0_1pct": 0}}})
    replay = consumer.replay(candidates, bars, lag=1, source_audit=audit, availability_evidence=sidecar)
    assert replay["hypothesis_id"] == consumer.STOCKDAY_AVAILABILITY_ID
    assert replay["engine_ledger_replay_completed"] is True
    assert replay["orders"] and {row["day"] for row in replay["orders"]} == {"2025-09-30"}
    assert all(row["side"] == "buy" and row["symbol"] == A for row in replay["orders"])
    assert replay["fills"] and sum(row["fees"] for row in replay["fills"]) > 0
    assert all(row["available_day"] == "2025-10-09" for row in replay["fills"])
    assert replay["state"]["cash"] < 10_000_000
    assert replay["execution_admitted"] is False and replay["selection_enabled"] is False
    # A schema change alone cannot promote the saved v2 method through the v1 path.
    proof = json.loads(sidecar.read_text(encoding="utf-8"))
    proof["schema"] = "seat_candidate_availability_evidence_v1"
    wrong = item.out.parent / "wrong_schema.json"
    _save(wrong, proof)
    with pytest.raises(ValueError):
        consumer.load_panel(candidates, bars, lag=1, availability_evidence=wrong)


def _followup_prices(item):
    days = [DAY, "2025-09-29", "2025-09-30", "2025-10-09", "2025-10-10"]
    bars = item.out.parent / "followup_synthetic_bars.csv"
    pd.DataFrame([{"symbol": A, "date": day, "available": True, "open": 10., "high": 10.2,
        "low": 9.8, "close": 10., "volume": 1_000_000, "amount": 10_000_000.,
        "pre_close": 10., "is_st": False} for day in days]).to_csv(bars, index=False)
    audit = item.out.parent / "followup_synthetic_audit.json"
    _save(audit, {"schema": "seat_candidate_source_bar_pair_audit_v1", "status": "completed_research_only",
        "execution_admitted": False, "summary": {"paired_rows": len(days),
            "price_stats": {field: {"mismatch_rows": 0} for field in ("open", "high", "low", "close", "pre_close")},
            "amount": {"mismatch_rows_gt_0_1pct": 0}}})
    return bars, audit


def _followup_zero_snapshot(cache):
    item = cache("zero_candidates")
    # Keep a real ready cache and two eligible seats, but only one buy-side
    # positive net: both fixed recipes must publish an actual empty CSV.
    for kind in ("buy", "sell"):
        file = item.source / f"{kind}_0001.json.gz"
        with gzip.open(file, "rt", encoding="utf-8") as stream:
            document = json.load(stream)
        for row in document["response"]["result"]["data"]:
            if row["OPERATEDEPT_CODE"] == "named-b":
                row.update(BUY=10., SELL=20., NET=-10.)
        with gzip.open(file, "wt", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False)
    module.build(item.source, item.out)
    return item


@pytest.mark.parametrize("mode", ["legacy_repeated_sum", "stockday_unique"])
def test_followup_producer_empty_batch_preserves_real_held_ledger_NO_CHANGE(cache, mode):
    from trading.contracts import RunContext, TradingMode
    item = _followup_zero_snapshot(cache)
    bars, audit = _followup_prices(item)
    candidates = item.out / mode / "candidates.csv"
    sidecar = item.out / mode / "availability_evidence.json"
    assert pd.read_csv(candidates).empty
    proof = json.loads(sidecar.read_text(encoding="utf-8"))
    assert proof["records"] == [] and proof["source_available_at"] == COMPLETED
    assert proof["source_archive"]["sha256"] == _hash(item.out / "source_bundle.zip")
    feed, selected, symbols, _ = consumer.load_panel(candidates, bars, lag=1, availability_evidence=sidecar)
    assert selected.empty and symbols == [A] and feed.signals == {}
    expected = consumer.AVAILABILITY_HYPOTHESIS_ID if mode == "legacy_repeated_sum" else consumer.STOCKDAY_AVAILABILITY_ID
    assert feed.hypothesis_id == expected
    fresh = consumer.replay(candidates, bars, lag=1, source_audit=audit, availability_evidence=sidecar)
    assert fresh["metrics"] is None and not fresh["orders"] and not fresh["fills"]
    assert fresh["target_holdings_generated"] is False and fresh["signal_days"] == []

    # Establish shares through the same actual Engine using a real nonempty
    # producer snapshot, then JSON-restore and apply the empty event update.
    seed = cache("seed_nonempty")
    module.build(seed.source, seed.out)
    seed_feed = consumer.load_panel(seed.out / mode / "candidates.csv", bars, lag=1,
        availability_evidence=seed.out / mode / "availability_evidence.json")[0]
    assert seed_feed.hypothesis_id == feed.hypothesis_id
    config = consumer._config(100000, 5)
    context = RunContext("synthetic-empty-followup:" + mode, TradingMode.REPLAY)
    state = consumer.new_state(config.initial_cash)
    consumer.advance(state, seed_feed, "2025-09-29", config, 0, context=context)
    consumer.advance(state, seed_feed, "2025-09-30", config, 1, context=context)
    assert state["fills"] and sum(lot["qty"] for lot in state["positions"][A]) > 0
    restored = json.loads(json.dumps(state))
    before = {key: deepcopy(restored[key]) for key in ("cash", "positions", "orders", "fills")}
    row = consumer.advance(restored, feed, "2025-10-09", config, 2, context=context)
    assert row["new_orders"] == 0 and restored["pending_targets"] is None
    assert all(restored[key] == value for key, value in before.items())
    assert restored["events"][-1]["kind"] == "no_decision"
    assert restored["events"][-1]["reason"] == "seat_availability_no_eligible_event"


def test_followup_v2_without_availability_evidence_is_rejected(cache):
    item = cache()
    module.build(item.source, item.out)
    bars, _ = _followup_prices(item)
    with pytest.raises(ValueError, match="requires explicit availability_evidence"):
        consumer.load_panel(item.out / "stockday_unique/candidates.csv", bars, lag=1)


@pytest.mark.parametrize("changed", ["empty_csv", "archive"])
def test_followup_empty_sidecar_still_checks_exact_csv_and_archive(cache, changed):
    item = _followup_zero_snapshot(cache)
    bars, _ = _followup_prices(item)
    candidates = item.out / "stockday_unique/candidates.csv"
    file = candidates if changed == "empty_csv" else item.out / "source_bundle.zip"
    file.write_bytes(file.read_bytes() + b"\n")  # Still parsable, but not the saved bytes.
    with pytest.raises(ValueError):
        consumer.load_panel(candidates, bars, lag=1,
            availability_evidence=item.out / "stockday_unique/availability_evidence.json")


def test_followup_complete_legacy_BSE_evidence_outside_scope_not_foreign_record(cache):
    bj = [_seat("bj-a", 200., 20., symbol="920001.BJ"), _seat("bj-b", 150., 10., symbol="920001.BJ")]
    summary = {"TRADE_DATE": DAY, "SECUCODE": "920001.BJ", "SECURITY_CODE": "920001",
        "EXPLANATION": SINGLE, "ACCUM_AMOUNT": 1000.}
    item = cache(extra_buy=bj, extra_sell=deepcopy(bj), extra_summaries=[summary])
    module.build(item.source, item.out)
    bars, audit = _followup_prices(item)
    candidates = item.out / "legacy_repeated_sum/candidates.csv"
    sidecar = item.out / "legacy_repeated_sum/availability_evidence.json"
    assert set(pd.read_csv(candidates).symbol) == {A, "920001.BJ"}
    proof = json.loads(sidecar.read_text(encoding="utf-8"))
    assert {row["symbol"] for row in proof["records"]} == {A, "920001.BJ"}
    feed, selected, symbols, _ = consumer.load_panel(candidates, bars, lag=1, availability_evidence=sidecar)
    assert symbols == [A] and selected.symbol.tolist() == [A]
    assert feed.availability["outside_scope_events"] == 1
    outside = next(row for row in feed.availability["schedule"] if row["symbol"] == "920001.BJ")
    assert outside["status"] == "outside_scope"
    assert outside["reason"] == "source_candidate_outside_existing_SH_SZ_feed_scope"
    assert outside["signal_day"] is None and outside["execution_day"] is None
    assert {row["symbol"] for rows in feed.signals.values() for row in rows} == {A}
    replay = consumer.replay(candidates, bars, lag=1, source_audit=audit, availability_evidence=sidecar)
    assert replay["orders"] and {row["symbol"] for row in replay["orders"]} == {A}
    foreign = deepcopy(proof)
    foreign["records"].append({**foreign["records"][0], "symbol": "000001.SZSE"})
    wrong = item.out.parent / "foreign_event_sidecar.json"
    _save(wrong, foreign)
    with pytest.raises(ValueError, match="unknown or duplicate candidate event"):
        consumer.load_panel(candidates, bars, lag=1, availability_evidence=wrong)
