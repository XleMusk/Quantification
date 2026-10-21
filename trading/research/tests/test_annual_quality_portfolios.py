from datetime import datetime
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.research import annual_quality_portfolios as module
from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import choose_targets
from trading.research.annual_quality_portfolios import AnnualQualityFeed, CASES, quality_pool, ranked
from trading.research.low_risk import features, portfolio


def inputs():
    symbols = [f"{index + 1:06d}.SZSE" for index in range(40)]
    annual = pd.DataFrame({"symbol": symbols, "report_day": "2025-12-31",
                          "revenue_cost_spread_assets": np.arange(40) / 100 - .1,
                          "operating_cashflow_assets": np.arange(40)[::-1] / 200 - .05})
    for field in ("revenue_cost_spread_assets", "operating_cashflow_assets"):
        annual[field + "_status"] = "available_numeric"
        annual[field + "_received_at"] = pd.Timestamp("2026-10-04T14:00:00+00:00")
    market = [{"symbol": symbol, "momentum": i / 100, "volatility": (i + 1) / 100,
               "median_amount": 3e7} for i, symbol in enumerate(symbols)]
    return annual, market


def test_same_pool_fixed_rank_weights_and_json_safe_provenance():
    annual, market = inputs()
    annual.loc[0, "operating_cashflow_assets"] = np.nan
    current, counts = quality_pool(market, annual)
    assert counts["common_pool"] == 39
    assert "000001.SZSE" not in current.index
    config = portfolio()
    for case in CASES:
        candidates = ranked(current, case)
        assert {row["symbol"] for row in candidates} <= set(current.index)
        json.dumps(candidates, allow_nan=False)
        weights = choose_targets(candidates, [], config)
        assert len(weights) == 20
        assert set(weights.values()) == {.04}
        assert abs(sum(weights.values()) - .8) < 1e-12


def test_no_historical_signal_and_first_new_session_with_shared_pool():
    annual, market = inputs()
    base = SimpleNamespace(calendar=["2026-09-30", "2026-10-08", "2026-10-09"],
                           day_index={"2026-09-30": 252, "2026-10-08": 253, "2026-10-09": 254})
    context = RunContext("unbound_test", TradingMode.REPLAY)
    feed = AnnualQualityFeed(base, {}, annual, CASES[3], "2026-10-09",
                             "2026-10-04T22:40:00+08:00", "2026-10-04T22:20:00+08:00")
    feed.market = SimpleNamespace(pool=lambda day, config: market)
    old = feed.signal_frame("2026-09-30", portfolio(), context)
    assert isinstance(old, SignalUnavailable)
    assert old.reason == "annual_quality_strategy_not_declared_by_decision"
    current = feed.signal_frame("2026-10-08", portfolio(), context)
    assert isinstance(current, SignalFrame)
    assert len(current.signals) == 40
    json.dumps(current.to_dict(), allow_nan=False)
    assert isinstance(feed.signal_frame("2026-10-09", portfolio(), context), SignalUnavailable)


def test_latest_date_features_preserve_original_monthly_default():
    days = pd.bdate_range("2025-09-01", periods=280).strftime("%Y-%m-%d").tolist()
    closes = pd.DataFrame({"000001.SZSE": np.exp(np.arange(280) * .001)}, index=days)
    base = SimpleNamespace(calendar=days, day_index={day: i for i, day in enumerate(days)},
                           closes=closes, volumes=closes * 100, amounts=closes * 2e7)
    default = features(base, days[-1])
    explicit = features(base, days[-1], feature_days=[days[-1]])
    assert default["momentum"].index[-1] != days[-1]
    assert explicit["momentum"].index.tolist() == [days[-1]]
    assert np.isfinite(explicit["momentum"].iloc[0, 0])


def test_real_feature_table_asof_column_reader(tmp_path):
    annual, _ = inputs()
    annual["asof"] = "2026-10-04T22:20:00+08:00"
    annual.to_parquet(tmp_path / "annual_quality_features.parquet", index=False)
    receipt = {"definition_id": module.FEATURE_ID, "status": "completed_annual_quality_features",
               "execution_admitted": False, "rows": len(annual), "asof": annual["asof"].iloc[0]}
    module.write_json(tmp_path / "result.json", receipt)
    read, result = module.load_quality(tmp_path, "2025-12-31")
    assert len(read) == 40
    assert result["asof"] == receipt["asof"]


def test_saved_price_capacity_uses_engine_lots_minima_and_fee_floor():
    weights = {CASES[0]: {"000001.SZSE": .04, "600000.SSE": .04, "688001.SSE": .04}}
    quotes = {symbol: {"close": price, "adjustflag": "3", "tradestatus": 1, "isST": 0}
              for symbol, price in zip(weights[CASES[0]], (10., 100., 30.))}
    rows, summaries = module.capacity_rows(weights, quotes, "2026-09-30", portfolio())
    rows = rows.set_index("symbol")
    assert rows.at["000001.SZSE", "engine_quantity"] == 500
    assert rows.at["000001.SZSE", "legal_quantity"] == 500
    assert rows.at["000001.SZSE", "buy_fees_cents"] == 505
    assert rows.at["000001.SZSE", "minimum_commission_premium_cents"] == 350
    assert rows.at["600000.SSE", "legal_quantity"] == 0
    assert rows.at["688001.SSE", "engine_quantity"] == 186
    assert rows.at["688001.SSE", "legal_quantity"] == 0
    assert rows.at["688001.SSE", "minimum_buy_quantity"] == 200
    assert rows.at["688001.SSE", "minimum_entry_capital_cents"] == 15_000_000
    summary = summaries[0]
    assert summary["known_sized_names"] == 1
    assert summary["below_minimum_names"] == 2
    assert summary["targets_outside_existing_ths_scope"] == 1
    assert summary["rounded_gross"] == 500_000 / 14_000_000
    assert summary["cash_after_close_cost_cents"] == 14_000_000 - 500_000 - 505


def test_capacity_unknown_quotes_are_not_zero_quantity_or_total():
    weights = {CASES[0]: {"000001.SZSE": .04, "000002.SZSE": .04, "000003.SZSE": .04}}
    quotes = {"000001.SZSE": {"close": None, "adjustflag": "3", "tradestatus": 1, "isST": 0},
              "000002.SZSE": {"close": 10., "adjustflag": "2", "tradestatus": 1, "isST": 0}}
    rows, summaries = module.capacity_rows(weights, quotes, "2026-09-30", portfolio())
    assert rows.legal_quantity.isna().all()
    assert rows.status.tolist() == ["unknown_or_invalid_raw_close", "not_explicit_raw_price", "missing_saved_raw_quote"]
    assert summaries[0]["unknown_targets"] == 3
    assert summaries[0]["rounded_gross"] is None
    assert summaries[0]["cash_after_close_cost_cents"] is None


def test_capacity_allocator_uses_existing_ledger_without_orders_or_past_intents():
    weights = {CASES[0]: {"000001.SZSE": .04, "600000.SSE": .04, "688001.SSE": .04}}
    quotes = {symbol: {"close": price, "adjustflag": "3", "tradestatus": 1, "isST": 0}
              for symbol, price in zip(weights[CASES[0]], (10., 100., 30.))}
    allocated, events = module.allocated_capacity_weights(weights, quotes, portfolio(), "2026-10-04T23:54:00+08:00")
    assert weights[CASES[0]] == {"000001.SZSE": .04, "600000.SSE": .04, "688001.SSE": .04}
    assert set(allocated[CASES[0]]) == set(weights[CASES[0]])
    assert sum(allocated[CASES[0]].values()) <= .12
    assert allocated[CASES[0]]["600000.SSE"] == 0
    rows, summaries = module.capacity_rows(allocated, quotes, "2026-09-30", portfolio())
    assert summaries[0]["known_sized_names"] == 2
    assert rows.loc[rows.symbol.eq("600000.SSE"), "status"].tolist() == ["explicit_zero_allocated_weight"]
    assert len(events) == 1 and events[0]["kind"] == "close_lot_allocation"
    assert events[0]["day"] == "2026-10-04"
    assert events[0]["policy"] == module.LOT_ALLOCATION
    assert events[0]["squared_error_after"] < events[0]["squared_error_before"]


def test_integer_capacity_does_not_round_trip_exact_lots_through_float_weights():
    weights = {CASES[3]: {"000895.SZSE": .04, "600750.SSE": .04}}
    quotes = {symbol: {"close": price, "adjustflag": "3", "tradestatus": 1, "isST": 0}
              for symbol, price in zip(weights[CASES[3]], (23.66, 22.53))}
    quantities, events = module.integer_capacity_quantities(weights, quotes, portfolio(), "2026-10-05T00:12:00+08:00")
    assert quantities[CASES[3]] == {"000895.SZSE": 200, "600750.SSE": 200}
    rows, summaries = module.capacity_rows(weights, quotes, "2026-09-30", portfolio(), quantities=quantities)
    assert rows.legal_quantity.tolist() == [200, 200]
    assert summaries[0]["rounded_gross"] == (23.66 * 200 + 22.53 * 200) / 140000
    assert events[0]["kind"] == "open_lot_allocation"
    assert events[0]["day"] == "2026-10-05"


def test_capacity_consumes_saved_targets_without_an_account(tmp_path, monkeypatch):
    root = tmp_path / "prepared"
    root.mkdir()
    annual, market = inputs()
    pool, _ = quality_pool(market, annual)
    config = portfolio()
    candidates = [dict(market_day="2026-09-30", case=case, rank=i, **row)
                  for case in CASES for i, row in enumerate(ranked(pool, case)[:30], 1)]
    pd.DataFrame(candidates).to_parquet(root / "preview_rankings.parquet", index=False)
    weights = {case: choose_targets(ranked(pool, case), [], config) for case in CASES}
    module.write_json(root / "preview_weights.json", {"mode": "known_market_preview_only", "held": [],
                                                     "account_created": False, "cases": weights})
    module.write_json(root / "protocol.json", {"id": "fixed", "definition_id": module.ID,
                "signals": list(CASES), "formulas": module.FORMULAS, "execution_admitted": False,
                "portfolio": config.to_dict(), "stock_inputs": {}, "preview_market_day": "2026-09-30",
                "preview_counts": {"common_pool": len(pool)},
                "declared_at": "2026-10-04T22:40:00+08:00", "feature_asof": "2026-10-04T22:20:00+08:00"})
    (root / "head.json").write_text('{"sessions":0}', encoding="utf-8")
    quotes = {row["symbol"]: {"close": 10., "adjustflag": "3", "tradestatus": 1, "isST": 0} for row in market}
    base = SimpleNamespace(calendar=["2026-09-30"], rows={"2026-09-30": quotes}, close=lambda: None)
    monkeypatch.setattr(module, "BulkResearchFeed", lambda **kwargs: base)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    destination, result = module.capacity(root)
    assert result["account_created"] is False
    assert result["execution_admitted"] is False
    assert result["orders"] == result["fills"] == 0
    assert len(pd.read_csv(destination / "capacity.csv")) == 80
    assert all(case["known_sized_names"] == 20 for case in result["cases"])
    assert (root / "head.json").read_text(encoding="utf-8") == '{"sessions":0}'


@pytest.mark.parametrize("checkpoint_saved", [False, True])
@pytest.mark.parametrize("allocation", [None, module.OPEN_LOT_ALLOCATION])
@pytest.mark.parametrize("comparison", [module.ID, module.INVESTMENT_ID])
def test_interrupted_batch_carries_all_case_cursors(tmp_path, monkeypatch, checkpoint_saved, allocation, comparison):
    cases, formulas, _ = module.comparison_definition(comparison)
    root = tmp_path / "prepared"
    root.mkdir()
    price = tmp_path / "price"
    price.mkdir()
    module.write_json(price / "result.json", {"status": "research_reconstruction_completed", "cutoff": "2026-10-13"})
    protocol = {"id": "test", "definition_id": comparison, "signals": list(cases), "formulas": formulas,
                "execution_admitted": False, "portfolio": portfolio().to_dict(),
                "stock_inputs": {"price_view": str(price)}, "declared_at": "2026-10-04T22:40:00+08:00",
                "preview_market_day": "2026-09-30", "quality_run": "unused", "report_day": "2025-12-31",
                "feature_id": "fixed", "feature_asof": "2026-10-04T22:20:00+08:00", "manifest": {}, "end": "2026-12-31",
                "lot_allocation": allocation}
    module.write_json(root / "protocol.json", protocol)
    module.publish_head(root, {"id": "test", "execution_admitted": False, "sessions": 0,
                              "lot_allocation": allocation,
                              "results": [{"signal": case, "completed_sessions": 0} for case in cases]})
    annual, _ = inputs()
    base = SimpleNamespace(calendar=["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"],
                           manifest={}, close=lambda: None)
    monkeypatch.setattr(module, "BulkResearchFeed", lambda **kwargs: base)
    monkeypatch.setattr(module, "load_quality", lambda *args: (annual, {"id": "fixed", "asof": protocol["feature_asof"]}))
    monkeypatch.setattr(module, "validate_extension", lambda *args: None)
    monkeypatch.setattr(module, "features", lambda *args, **kwargs: {})
    monkeypatch.setattr(module, "report", lambda *args: None)
    monkeypatch.setattr(module, "AnnualQualityFeed", lambda *args: SimpleNamespace(audit=[], ranking_rows=[], case=args[3]))
    cursors = []
    def load_checkpoint(path, config, dates, **kwargs):
        assert kwargs.get("lot_allocation") == allocation
        cursors.append(str(path))
        return {"prior": True}
    monkeypatch.setattr(module, "load_replay_checkpoint", load_checkpoint)
    def interrupted(feed, config, dates, destination, **kwargs):
        assert kwargs.get("lot_allocation") == allocation
        if feed.case == CASES[1]:
            if checkpoint_saved:
                destination.mkdir()
                module.write_json(destination / "replay.json", {"status": "interrupted", "completed_sessions": 1})
            raise RuntimeError("Interrupted after the first case")
        return {"status": "completed", "completed_sessions": len(dates)}
    monkeypatch.setattr(module, "baseline_ledger_result", interrupted)
    point = datetime.fromisoformat("2026-10-13T18:00:00+08:00")
    with pytest.raises(RuntimeError, match="Interrupted"):
        module.advance(root, current=point)
    saved = json.loads((root / "head.json").read_text(encoding="utf-8"))
    assert saved["sessions"] == 0
    assert [row["signal"] for row in saved["results"]] == list(cases)
    assert saved["results"][0]["completed_sessions"] == 3
    assert [row["completed_sessions"] for row in saved["results"][1:]] == [0] * (len(cases) - 1)
    assert saved["pending_case"]["signal"] == CASES[1]
    def continued(feed, config, dates, destination, **kwargs):
        assert kwargs.get("lot_allocation") == allocation
        return {"status": "completed", "completed_sessions": len(dates)}
    monkeypatch.setattr(module, "baseline_ledger_result", continued)
    _, final = module.advance(root, current=point)
    assert final["status"] == "completed"
    assert final["sessions"] == 3
    assert "pending_case" not in final
    expected = {saved["results"][0]["checkpoint"]}
    if checkpoint_saved:
        expected.add(saved["pending_case"]["destination"])
    assert set(cursors) == expected
    assert len(cursors) == len(expected)
    assert json.loads((Path(final["last_batch"]) / "run.json").read_text(encoding="utf-8"))["lot_allocation"] == allocation


def test_advance_rejects_changed_declared_allocation_before_data_use(tmp_path):
    protocol = {"id": "test", "definition_id": module.ID, "signals": list(CASES),
                "formulas": module.FORMULAS, "execution_admitted": False,
                "lot_allocation": module.OPEN_LOT_ALLOCATION}
    module.write_json(tmp_path / "protocol.json", protocol)
    module.write_json(tmp_path / "head.json", {"id": "test", "execution_admitted": False,
                       "lot_allocation": None, "results": [{"signal": case} for case in CASES]})
    with pytest.raises(ValueError, match="fixed annual quality comparison"):
        module.advance(tmp_path)


def test_saved_readers_keep_unknown_capacity_and_reject_external_paths(tmp_path, monkeypatch):
    identifier = "20261005_010000_1234abcd"
    root = tmp_path / "research/annual_quality_portfolios" / identifier
    root.mkdir(parents=True)
    protocol = {"id": identifier, "definition_id": module.ID, "signals": list(CASES), "formulas": module.FORMULAS,
                "execution_admitted": False, "lot_allocation": None, "declared_at": "2026-10-05T01:00:00+08:00",
                "feature_asof": "2026-10-04T22:20:00+08:00", "report_day": "2025-12-31",
                "preview_market_day": "2026-09-30", "preview_counts": {"common_pool": 40},
                "portfolio": portfolio().to_dict()}
    head = {"id": identifier, "execution_admitted": False, "sessions": 0, "account_created": False,
            "status": "waiting_for_post_declaration_market_data", "results": [{"signal": case} for case in CASES]}
    module.write_json(root / "protocol.json", protocol)
    module.write_json(root / "head.json", head)
    module.write_json(root / "preview_weights.json", {"mode": "known_market_preview_only", "account_created": False,
                "cases": {case: {"000001.SZSE": .04} for case in CASES}})
    pd.DataFrame([{"case": CASES[0], "rank": 1, "symbol": "000001.SZSE", "score": .1}]).to_parquet(root / "preview_rankings.parquet", index=False)
    capacity = root / "capacity_previews" / "20261005_010100_5678abcd"
    capacity.mkdir(parents=True)
    rows, summaries = module.capacity_rows({CASES[0]: {"000001.SZSE": .04}}, {}, "2026-09-30", portfolio())
    rows.to_csv(capacity / "capacity.csv", index=False)
    module.write_json(capacity / "result.json", {"id": capacity.name, "definition_id": module.ID,
                "prepared_run": str(root), "account_created": False, "execution_admitted": False,
                "orders": 0, "fills": 0, "status": "partial_unknown_prices", "cases": summaries})
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path)
    assert module.saved_catalog()[0]["sessions"] == 0
    detail = module.saved_detail(identifier, CASES[0])
    json.dumps(detail, allow_nan=False)
    assert detail["capacities"][0]["summary"]["rounded_gross"] is None
    assert detail["capacities"][0]["slots"][0]["legal_quantity"] is None
    assert json.loads((root / "head.json").read_text(encoding="utf-8")) == head
    with pytest.raises(ValueError, match="identifier"):
        module.saved_detail("../outside", CASES[0])


def test_investment_comparison_controls_share_full_financial_intersection():
    annual, market = inputs()
    annual["asset_growth"] = np.arange(40) / 100 - .2
    annual["asset_growth_status"] = "available_numeric"
    annual["asset_growth_received_at"] = pd.Timestamp("2026-10-04T14:00:00+00:00")
    annual.loc[0, "asset_growth_status"] = "missing_prior_period"
    pool, counts = quality_pool(market, annual, include_investment=True)
    assert counts["common_pool"] == 39
    assert "000001.SZSE" not in pool.index
    for case in module.INVESTMENT_CASES:
        candidates = ranked(pool, case)
        assert {row["symbol"] for row in candidates} <= set(pool.index)
        assert len(choose_targets(candidates, [], portfolio())) == 20
    low = ranked(pool, module.INVESTMENT_CASES[3])
    assert low[0]["symbol"] == "000002.SZSE"
    assert low[-1]["symbol"] == "000040.SZSE"
    original, original_counts = quality_pool(market, annual)
    assert original_counts["common_pool"] == 40
    assert "investment" not in original
    assert module.comparison_definition(module.ID) == (CASES, module.FORMULAS, False)
    with pytest.raises(ValueError, match="changed"):
        module.declared_definition({"definition_id": module.INVESTMENT_ID,
            "signals": list(CASES), "formulas": module.FORMULAS})


def test_investment_feed_rejects_old_decisions_and_late_asset_inputs():
    annual, market = inputs()
    annual["asset_growth"] = np.arange(40) / 100
    annual["asset_growth_status"] = "available_numeric"
    annual["asset_growth_received_at"] = pd.Timestamp("2026-10-04T14:00:00+00:00")
    base = SimpleNamespace(calendar=["2026-09-30", "2026-10-08", "2026-10-09"],
        day_index={"2026-09-30": 252, "2026-10-08": 253, "2026-10-09": 254})
    args = (base, {}, annual, CASES[0], "2026-10-09",
            "2026-10-05T06:00:00+08:00", "2026-10-04T22:20:00+08:00", True)
    feed = AnnualQualityFeed(*args)
    feed.market = SimpleNamespace(pool=lambda day, config: market)
    context = RunContext("unbound_test", TradingMode.REPLAY)
    assert isinstance(feed.signal_frame("2026-09-30", portfolio(), context), SignalUnavailable)
    assert len(feed.signal_frame("2026-10-08", portfolio(), context).signals) == 40
    annual.loc[0, "asset_growth_received_at"] = pd.Timestamp("2026-10-05T01:00:00+00:00")
    with pytest.raises(ValueError, match="not received"):
        AnnualQualityFeed(*args)


def selective_advance_fixture(tmp_path, monkeypatch):
    cases = module.INVESTMENT_CASES
    root, price = tmp_path / "prepared", tmp_path / "price"
    root.mkdir()
    price.mkdir()
    module.write_json(price / "result.json", {"status": "research_reconstruction_completed", "cutoff": "2026-10-19"})
    protocol = {"id": "fixed", "definition_id": module.INVESTMENT_ID, "signals": list(cases),
        "formulas": module.INVESTMENT_FORMULAS, "execution_admitted": False,
        "portfolio": portfolio().to_dict(), "stock_inputs": {"price_view": str(price)},
        "declared_at": "2026-10-05T06:11:00+08:00", "preview_market_day": "2026-09-30",
        "quality_run": "unused", "report_day": "2025-12-31", "feature_id": "frozen",
        "feature_asof": "2026-10-05T06:10:00+08:00", "manifest": {}, "end": "2026-12-31",
        "lot_allocation": module.OPEN_LOT_ALLOCATION}
    module.write_json(root / "protocol.json", protocol)
    prior = root / "batches/prior"
    prior.mkdir(parents=True)
    module.write_json(prior / "run.json", {"manifest": {}, "end": "2026-10-16",
        "lot_allocation": module.OPEN_LOT_ALLOCATION})
    results = []
    for index, case in enumerate(cases):
        checkpoint = prior / case
        checkpoint.mkdir()
        completed = index in (0, 3)
        row = {"signal": case, "checkpoint": str(checkpoint), "status": "completed" if completed else "failed",
            "completed_sessions": 7 if completed else 4, "final_equity": 140000 - index,
            "financial_unknown": None, "failure_detail": "unchanged original failure" if not completed else None}
        module.write_json(checkpoint / "account.json", {"original_case": case, "cash": 140000 - index})
        results.append(row)
    head = {"id": "fixed", "execution_admitted": False, "sessions": 4, "account_created": True,
        "lot_allocation": module.OPEN_LOT_ALLOCATION, "status": "partial", "last_batch": str(prior), "results": results}
    module.publish_head(root, head)
    calls = SimpleNamespace(consumed=[], loaded=[], validations=[], opened=[])
    base = SimpleNamespace(calendar=["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12",
        "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"],
        manifest={}, close=lambda: None)
    monkeypatch.setattr(module, "lock", lambda path: nullcontext())
    monkeypatch.setattr(module, "BulkResearchFeed", lambda **kwargs: (calls.opened.append(kwargs), base)[1])
    monkeypatch.setattr(module, "load_quality", lambda *args: (pd.DataFrame(), {"id": "frozen", "asof": protocol["feature_asof"]}))
    monkeypatch.setattr(module, "validate_extension", lambda before, after, day: calls.validations.append(day))
    monkeypatch.setattr(module, "features", lambda *args, **kwargs: {})
    monkeypatch.setattr(module, "report", lambda *args: None)
    monkeypatch.setattr(module, "AnnualQualityFeed", lambda *args: SimpleNamespace(audit=[], ranking_rows=[], case=args[3]))
    def checkpoint(path, config, dates, **kwargs):
        assert kwargs == {"lot_allocation": module.OPEN_LOT_ALLOCATION}
        calls.loaded.append(str(path))
        return {"source": str(path)}
    def consume(feed, config, dates, destination, *, checkpoint, **kwargs):
        assert kwargs == {"lot_allocation": module.OPEN_LOT_ALLOCATION}
        calls.consumed.append(feed.case)
        destination.mkdir()
        module.write_json(destination / "account.json", {"continued_from": checkpoint["source"], "day": dates[-1]})
        return {"status": "completed", "completed_sessions": len(dates), "sessions": len(dates)}
    monkeypatch.setattr(module, "load_replay_checkpoint", checkpoint)
    monkeypatch.setattr(module, "baseline_ledger_result", consume)
    return SimpleNamespace(root=root, protocol=protocol, head=head, calls=calls,
        current=datetime.fromisoformat("2026-10-19T18:00:00+08:00"))


def test_selected_healthy_cases_continue_in_declared_order_and_carry_all_other_results(tmp_path, monkeypatch):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    cases = module.INVESTMENT_CASES
    original_protocol = (saved.root / "protocol.json").read_bytes()
    original_accounts = {row["checkpoint"]: (Path(row["checkpoint"]) / "account.json").read_bytes()
        for row in saved.head["results"]}
    _, head = module.advance(saved.root, current=saved.current, only_cases=[cases[3], cases[0]])
    assert saved.calls.consumed == [cases[0], cases[3]]
    assert saved.calls.loaded == [saved.head["results"][index]["checkpoint"] for index in (0, 3)]
    assert saved.calls.validations == ["2026-09-30", "2026-10-16"]
    assert [row["signal"] for row in head["results"]] == list(cases)
    assert head["status"] == "partial" and head["sessions"] == 4
    for index in (1, 2, 4):
        assert head["results"][index] == saved.head["results"][index]
    assert [head["results"][index]["completed_sessions"] for index in (0, 3)] == [8, 8]
    assert (saved.root / "protocol.json").read_bytes() == original_protocol
    assert all((Path(path) / "account.json").read_bytes() == value for path, value in original_accounts.items())
    record = json.loads((Path(head["last_batch"]) / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed" and record["status_scope"] == "selected_cases_only"
    assert record["comparison_status"] == "partial"
    assert record["selected_cases"] == [cases[0], cases[3]]
    assert record["unselected_cases"] == [cases[1], cases[2], cases[4]]
    assert [row["signal"] for row in record["results"]] == [cases[0], cases[3]]


@pytest.mark.parametrize("selection", [[], ["unknown"], [module.INVESTMENT_CASES[0]] * 2])
def test_bad_case_selection_is_rejected_before_data_or_account_writes(tmp_path, monkeypatch, selection):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in saved.root.rglob("*.json")}
    with pytest.raises(ValueError, match="annual case"):
        module.advance(saved.root, current=saved.current, only_cases=selection)
    assert not saved.calls.opened and not saved.calls.loaded and not saved.calls.consumed
    assert {path: path.read_bytes() for path in saved.root.rglob("*.json")} == before
    assert list((saved.root / "batches").iterdir()) == [saved.root / "batches/prior"]


@pytest.mark.parametrize("include_pending", [False, True])
def test_case_selection_cannot_skip_or_bypass_unreconciled_pending(tmp_path, monkeypatch, include_pending):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    pending = saved.root / "batches/pending"
    pending.mkdir()
    (pending / "account.json").write_text("{}", encoding="utf-8")
    saved.head["pending_case"] = {"signal": module.INVESTMENT_CASES[2], "destination": str(pending)}
    module.publish_head(saved.root, saved.head)
    before = (saved.root / "head.json").read_bytes()
    selection = [module.INVESTMENT_CASES[2 if include_pending else 0]]
    with pytest.raises(ValueError, match="no terminal receipt|saved pending case"):
        module.advance(saved.root, current=saved.current, only_cases=selection)
    assert not saved.calls.loaded and not saved.calls.consumed
    assert (saved.root / "head.json").read_bytes() == before


def test_default_advance_still_calls_every_declared_case(tmp_path, monkeypatch):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    _, head = module.advance(saved.root, current=saved.current)
    assert saved.calls.consumed == list(module.INVESTMENT_CASES)
    assert head["status"] == "completed" and head["sessions"] == 8
    record = json.loads((Path(head["last_batch"]) / "run.json").read_text(encoding="utf-8"))
    assert "status_scope" not in record and len(record["results"]) == 5


def test_unselected_completed_cases_behind_new_endpoint_keep_comparison_partial(tmp_path, monkeypatch):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    for row in saved.head["results"]:
        row.update(status="completed", completed_sessions=7)
    saved.head["sessions"] = 7
    module.publish_head(saved.root, saved.head)
    cases = module.INVESTMENT_CASES
    _, head = module.advance(saved.root, current=saved.current, only_cases=[cases[0], cases[3]])
    assert head["status"] == "partial" and head["sessions"] == 7
    assert [row["completed_sessions"] for row in head["results"]] == [8, 7, 7, 8, 7]
    for index in (1, 2, 4):
        assert head["results"][index] == saved.head["results"][index]


def test_repeated_selected_endpoint_returns_without_new_batch_or_account_writes(tmp_path, monkeypatch):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    selected = [module.INVESTMENT_CASES[0], module.INVESTMENT_CASES[3]]
    _, first = module.advance(saved.root, current=saved.current, only_cases=selected)
    before = {path: path.read_bytes() for path in saved.root.rglob("*") if path.is_file()}
    def unexpected(*args, **kwargs):
        pytest.fail("An already completed selected endpoint must not prepare features or write a new batch/account")
    for name in ("features", "load_replay_checkpoint", "AnnualQualityFeed", "baseline_ledger_result", "publish_head"):
        monkeypatch.setattr(module, name, unexpected)
    _, repeated = module.advance(saved.root, current=saved.current, only_cases=selected)
    assert repeated == first and repeated["status"] == "partial" and repeated["sessions"] == 4
    assert saved.calls.consumed == selected
    assert saved.calls.validations == ["2026-09-30", "2026-10-16", "2026-09-30", "2026-10-19"]
    assert {path: path.read_bytes() for path in saved.root.rglob("*") if path.is_file()} == before


def test_selected_endpoint_noop_still_rejects_changed_last_source_prefix(tmp_path, monkeypatch):
    saved = selective_advance_fixture(tmp_path, monkeypatch)
    selected = [module.INVESTMENT_CASES[0], module.INVESTMENT_CASES[3]]
    for row in saved.head["results"]:
        if row["signal"] in selected:
            row["completed_sessions"] = 8
    module.publish_head(saved.root, saved.head)
    prior = Path(saved.head["last_batch"]) / "run.json"
    batch = json.loads(prior.read_text(encoding="utf-8"))
    batch["end"] = "2026-10-19"
    module.write_json(prior, batch)
    before = {path: path.read_bytes() for path in saved.root.rglob("*") if path.is_file()}
    def validate(before, after, day):
        saved.calls.validations.append(day)
        if day == "2026-10-19":
            raise ValueError("Past source prefix changed")
    monkeypatch.setattr(module, "validate_extension", validate)
    with pytest.raises(ValueError, match="Past source prefix changed"):
        module.advance(saved.root, current=saved.current, only_cases=selected)
    assert saved.calls.validations == ["2026-09-30", "2026-10-19"]
    assert not saved.calls.loaded and not saved.calls.consumed
    assert {path: path.read_bytes() for path in saved.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("selection", [None, [module.INVESTMENT_CASES[3], module.INVESTMENT_CASES[0]]])
def test_advance_cli_passes_repeated_case_selection(tmp_path, monkeypatch, capsys, selection):
    calls = []
    def advance(root, inputs, **kwargs):
        calls.append((root, inputs, kwargs))
        return root, {"status": "partial", "sessions": 4, "account_created": True}
    monkeypatch.setattr(module, "advance", advance)
    argv = ["advance", "--prepared-run", str(tmp_path)]
    for case in selection or []:
        argv.extend(["--case", case])
    assert module.main(argv) == 1
    assert calls == [(tmp_path, None, {"session_calendar": None, "only_cases": selection})]
    assert json.loads(capsys.readouterr().out)["status"] == "partial"
