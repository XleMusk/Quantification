"""Saved fixed-account consumers on tiny local artifacts, without Engine trades."""
import csv
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.research import next_session_preview as m
from trading.research.stockdb_rolling_low_volatility import CALENDAR_MONTH_END_METHOD
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE


A, B, C = "600001.SSE", "000001.SZSE", "300001.SZSE"
D, E, SIGNAL = "2026-10-09", "2026-10-12", "low_volatility_252_20"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def csv_rows(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "ROOT", tmp_path / "project")
    monkeypatch.setattr(m, "DATA_ROOT", tmp_path / "data")
    artifacts = tmp_path / "artifacts"
    account_dir = artifacts / SIGNAL
    account_dir.mkdir(parents=True)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([dict(date=20261009, code=s[:6], close=p, volume=1000,
        amount=p*1000, is_st=False) for s, p in [(A, 10.), (B, 20.), (C, 30.)]]
        ).to_parquet(snapshot / "quotes.parquet", index=False)
    write(snapshot / "manifest.json", {"chunks": [{"parquet_file": "quotes.parquet"}]})
    config = StrategyConfig(holdings=2, retain_rank=2, single_limit=.6)
    schedule = {"method_id": CALENDAR_MONTH_END_METHOD, "calendars": []}
    protocol = {"full_exit_lifecycle": FULL_EXIT_LIFECYCLE,
                "month_end_schedule": schedule, "snapshot": str(snapshot)}
    source = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "end": D,
        "artifacts": str(artifacts), "protocol": protocol, "portfolio": config.to_dict(),
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "month_end_schedule": schedule,
        "calendar_verification": {"fixed_open_sessions": [D, E]},
        "results": [{"signal": SIGNAL, "status": "completed"}]}
    account = {"day": D, "cash": 9500000,
        "positions": {A: [{"qty": 100}], B: [{"qty": 200}]},
        "pending_targets": None, "targets": {C: .8},
        "full_exit_lifecycle": {"schema_version": 1, "method_id": FULL_EXIT_LIFECYCLE,
            "target_version": 1, "latest_target": {"version": 1, "weights": {C: .8},
                "source": "old_saved_target", "decided_on": "2026-09-30"},
            "remaining_full_exits": {}}}
    receipt = {"full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "status": "completed",
        "config": config.to_dict(), "last_completed_session": D,
        "account_basis": "last_successfully_completed_session"}
    csv_rows(account_dir / "daily.csv", [{"date": D, "equity": "100000.00"}])
    csv_rows(account_dir / "orders.csv", [{"day": "2026-10-08", "symbol": A}])
    csv_rows(artifacts / f"{SIGNAL}_ranks.csv", [
        {"date": "2026-09-30", "rank": i, "symbol": s,
         "volatility": .01*i, "momentum": .1, "median_amount": 100000000}
        for i, s in enumerate([C, B], 1)])
    paths = {"run": tmp_path / "run.json", "account": account_dir / "account.json",
             "protocol": artifacts / "protocol.json", "receipt": account_dir / "replay.json"}
    objects = {"run": source, "account": account, "protocol": protocol, "receipt": receipt}
    def flush():
        for name, path in paths.items():
            write(path, objects[name])
    flush()
    return dict(paths=paths, objects=objects, flush=flush)


def preview(saved, **kwargs):
    before = {key: path.read_bytes() for key, path in saved["paths"].items()}
    output, result = m.build(saved["paths"]["run"], SIGNAL, D, E, **kwargs)
    assert {key: path.read_bytes() for key, path in saved["paths"].items()} == before
    rows = lambda name: m._read_csv(result["artifacts"][name])
    handoff = m._read_json(result["artifacts"]["ths_manual_handoff"])
    assert rows("target_holdings") == rows("provisional_target_holdings")
    assert rows("order_plan") == rows("provisional_order_plan")
    assert handoff["rank_template"] == [] and not handoff["observed_account_sizing_supported"]
    assert result["execution_admitted"] is False and result["selection_enabled"] is False
    return result, rows("target_holdings"), rows("order_plan"), handoff


@pytest.mark.parametrize("pending", [{A: .4}, {}])
def test_new_full_target_including_empty_uses_saved_weights_not_saved_rank(saved, pending):
    saved["objects"]["account"]["pending_targets"] = pending
    saved["flush"]()
    result, targets, orders, handoff = preview(saved)
    assert result["trade_status"] == "PAPER_TARGET_PENDING"
    actual = {row["symbol"]: int(row["target_qty"]) for row in targets}
    assert actual == {A: 4000 if pending else 0, B: 0}
    assert C not in actual
    assert {r["symbol"]: r["target_qty"] for r in handoff["target_template"]} == actual
    assert all(r["sellable_evidence_required"] == "True" for r in orders if r["side"] == "sell")


def test_no_change_non_month_end_keeps_exact_positions_and_has_no_orders(saved):
    result, targets, orders, handoff = preview(saved)
    assert result["trade_status"] == "NO_TRADE"
    assert {r["symbol"]: int(r["target_qty"]) for r in targets} == {A: 100, B: 200}
    assert all(int(r["qty"]) == 0 for r in orders) and handoff["orders"] == []
    assert result["rank_carry_forward_allowed"] is False


def test_saved_account_opening_pack_uses_account_tasks_instead_of_stale_rank(saved, monkeypatch):
    from trading.research import opening_pack
    result, _, _, _ = preview(saved)
    monkeypatch.setattr(opening_pack, "ROOT", m.ROOT)
    monkeypatch.setattr(opening_pack, "DATA_ROOT", m.DATA_ROOT)
    source = Path(result["artifacts"]["target_holdings"]).parent / "result.json"
    _, pack = opening_pack.build([source], strategy_id=SIGNAL, execution_day=E)
    assert pack["candidate_order_count"] == 0 and pack["candidate_target_count"] == 2
    assert pack["release_control"]["state"] == "blocked_research_saved_account_only"
    assert "rank_not_current_for_execution_day" not in pack["release_control"]["blockers"]
    assert pack["release_control"]["execution_release_allowed"] is False
    assert pack["strategies"][0]["trade_status"] == "NO_TRADE"


def test_residual_only_zeroes_unfinished_exit_and_preserves_other_exact_shares(saved):
    saved["objects"]["account"]["full_exit_lifecycle"]["remaining_full_exits"][B] = {
        "remaining_quantity": 200, "requested_on": "2026-10-08"}
    saved["flush"]()
    result, targets, orders, _ = preview(saved)
    assert result["trade_status"] == "PAPER_EXIT_PENDING"
    assert {r["symbol"]: int(r["target_qty"]) for r in targets} == {A: 100, B: 0}
    assert [(r["symbol"], r["side"], r["qty"]) for r in orders if int(r["qty"])] == [(B, "sell", "200")]


def test_mainboard_scope_keeps_outside_positions_even_if_pending_target_empty(saved):
    saved["objects"]["account"]["positions"][C] = [{"qty": 100}]
    saved["objects"]["account"]["pending_targets"] = {}
    saved["flush"]()
    _, targets, orders, _ = preview(saved, execution_scope="mainboard_only")
    assert next(r for r in targets if r["symbol"] == C)["target_qty"] == "100"
    assert next(r for r in orders if r["symbol"] == C)["qty"] == "0"


@pytest.mark.parametrize("missing_symbol", [A, B])
def test_missing_price_is_unknown_and_blocked_never_zero_sized(saved, monkeypatch, missing_symbol):
    saved["objects"]["account"]["pending_targets"] = {A: .4}
    saved["flush"]()
    original = m._quote_rows
    monkeypatch.setattr(m, "_quote_rows", lambda *args: {s: q for s, q in original(*args).items() if s != missing_symbol})
    result, targets, orders, handoff = preview(saved)
    assert next(r for r in targets if r["symbol"] == missing_symbol)["target_qty"] == ("" if missing_symbol == A else "0")
    assert next(r for r in orders if r["symbol"] == missing_symbol)["status"] == "blocked_missing_research_quote"
    if missing_symbol == A:
        # An unknown buy has no invented quantity and is excluded from adapter orders.
        assert A not in {r["symbol"] for r in handoff["orders"]}
    else:
        assert next(r for r in handoff["orders"] if r["symbol"] == B)["status"] == "blocked_missing_research_quote"
    assert result["marked_value_cents"] is None and result["missing_quote_symbols"] == [missing_symbol]


@pytest.mark.parametrize("part", ["protocol", "receipt", "account"])
def test_mismatched_or_removed_method_rejects(saved, part):
    saved["objects"][part]["full_exit_lifecycle"] = None
    saved["flush"]()
    with pytest.raises(ValueError, match="method mismatch|lifecycle"):
        m.build(saved["paths"]["run"], SIGNAL, D, E)


def test_legacy_source_preserves_original_rank_provisional_contract(saved):
    source = saved["objects"]["run"]
    source.pop("full_exit_lifecycle")
    source["protocol"].pop("full_exit_lifecycle")
    saved["objects"]["account"].pop("full_exit_lifecycle")
    saved["flush"]()
    _, result = m.build(saved["paths"]["run"], SIGNAL, D, E)
    handoff = m._read_json(result["artifacts"]["ths_manual_handoff"])
    assert "task_method" not in result and result["trade_status"] == "NO_TRADE"
    assert len(handoff["rank_template"]) == 2
    assert C in {r["symbol"] for r in handoff["target_template"]}
