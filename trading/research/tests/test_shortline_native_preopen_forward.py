"""Synthetic source-to-checkpoint integration; real Feed/Engine/Ledger, no fit."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger
from trading.research import baostock_all_stock_probe as roster
from trading.research import baostock_bulk_close as bulk
from trading.research import native_history_chain_source as chain
from trading.research import shortline_native_event_panel as producer
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_scoring as scoring
from trading.research.baostock_daily_slice import digest
from trading.research.baostock_native_history import RAW_FIELDS
from trading.research.shortline_panel_replay import _config
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture
from trading.research.tests.test_native_history_source import save, read, write_panel


P, D, E, F = "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"
SYMBOLS = [f"600{i:03d}.SSE" for i in range(1, 11)]
TARGET = {"lr": "600010.SSE", "lgbm": "600001.SSE"}


def rows(day):
    output = []
    opening, closing, previous = {P: (9.09, 9.09, 9.09), D: (10., 10., 9.09),
        E: (10., 11., 10.), F: (11., 12.1, 11.)}[day]
    codes = ["sh." + symbol[:6] for symbol in SYMBOLS]
    if day in (E, F):
        codes.append("sh.600099")
    for code in codes:
        op, cl, pre = (10., 10., 9.09 if day == E else 10.) if code == "sh.600099" else (opening, closing, previous)
        output.append({"date": day, "code": code, "open": str(op), "high": str(max(op, cl) + .1),
            "low": str(min(op, cl) - .1), "close": str(cl), "preclose": str(pre), "volume": "1000000",
            "amount": str((op + cl) / 2 * 1000000), "adjustflag": "3", "turn": "2.3456",
            "tradestatus": "1", "pctChg": str((cl / pre - 1) * 100), "peTTM": "12", "pbMRQ": "1",
            "psTTM": "1", "pcfNcfTTM": "1", "isST": "0"})
    return output


def native_base(folder):
    folder.mkdir()
    calendar = folder / "calendar.csv"
    pd.DataFrame({"date": [P]}).to_csv(calendar, index=False)
    raw = folder / "raw_daily.parquet"
    write_panel(raw, [rows(P)])
    csv = folder / (P + "_stock.csv")
    pd.DataFrame(rows(P), columns=list(RAW_FIELDS)).to_csv(csv, index=False)
    task = {"date": P, "kind": "stock", "method": "query_daily_history_k_AStock", "file": csv.name,
        "fields": ",".join(RAW_FIELDS), "sha256": digest(csv), "error_code": "0", "quality": {"rows": 10, "symbols": 10}}
    save(folder / "plan.json", {"kind": "stock", "days": [P], "calendar_source": str(calendar),
        "calendar_sha256": digest(calendar), "requested_start": P, "requested_end": P,
        "execution_admitted": False, "historical_universe_independently_verified": False})
    save(folder / "receipts.json", {"status": "source_cache_complete", "execution_admitted": False,
        "expected_days": 1, "updated_at": D + "T18:00:00+08:00", "tasks": {P: task}, "failures": []})
    save(folder / "result.json", {"status": "source_cache_complete", "kind": "stock", "expected_days": 1,
        "acquired_days": 1, "rows": 10, "symbols": 10, "missing_days": [],
        "raw_daily_sha256": digest(raw), "panel_file": "raw_daily.parquet", "panel_sha256": digest(raw),
        "plan_sha256": digest(folder / "plan.json"), "receipts_sha256": digest(folder / "receipts.json"),
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False})
    return folder


def strict_close(folder, day, calendar, sdk, *, reference_override=None):
    folder.mkdir()
    probe, status = folder / "probe", folder / "roster"
    probe.mkdir()
    status.mkdir()
    clock = datetime.fromisoformat(day + "T18:30:00+08:00")
    frame = pd.DataFrame(rows(day), columns=list(RAW_FIELDS))
    if reference_override:
        for symbol, value in reference_override.items():
            mask = frame.code.eq("sh." + symbol[:6])
            frame.loc[mask, "preclose"] = str(value)
            frame.loc[mask, "pctChg"] = frame.loc[mask, "close"].astype(float).map(lambda close: str((close / value - 1) * 100))
    file = probe / (day + "_stock.csv")
    frame.to_csv(file, index=False)
    save(probe / "receipt.json", {"status": "completed_probe", "created_at": (clock - timedelta(seconds=5)).isoformat(),
        "finished_at": (clock - timedelta(seconds=3)).isoformat(), "sdk": sdk,
        "implementation_sha256": digest(bulk.daily_source_probe.__file__), "days": [day], "kinds": ["stock"],
        "execution_admitted": False, "full_history_verified": False, "entries": [{"date": day, "kind": "stock",
            "method": "query_daily_history_k_AStock", "error_code": "0", "file": file.name,
            "sha256": digest(file), "quality": {"rows": len(frame), "symbols": len(frame), "issues": []}}]})
    raw_status = pd.DataFrame([[row.code, "1", row.code] for row in frame.itertuples()],
        columns=["code", "tradeStatus", "code_name"])
    roster_file = status / ("query_all_stock_" + day + ".csv")
    raw_status.to_csv(roster_file, index=False)
    save(status / "receipt.json", {"schema": "baostock_historical_all_stock_status_probe_v1", "day": day,
        "response_day": day, "method": "query_all_stock", "kwargs": {"day": day}, "sdk": sdk,
        "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
        "fields_returned": list(raw_status), "execution_admitted": False, "price_bars_generated": False,
        "raw_csv": str(roster_file), "raw_sha256": digest(roster_file), "rows_preserved": len(raw_status),
        "completed_at": (clock - timedelta(seconds=1)).isoformat()})
    out = folder / "complete"
    bulk.import_bulk_close(probe, status, calendar, out, now=clock)
    return out


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    clock = {"value": datetime.fromisoformat("2026-10-20T20:00:00+08:00")}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock["value"]
            return value.astimezone(tz) if tz is not None else value
    for module in (bulk, chain, scoring):
        monkeypatch.setattr(module, "datetime", Clock)
    for module in (producer, policy, forward):
        monkeypatch.setattr(module, "_now", lambda: clock["value"])
    monkeypatch.setattr(engine, "_published_now", lambda: clock["value"])
    calendar = tmp_path / "calendar.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"synthetic provenance only, never loaded")
    sdk_hash = digest(wheel)
    monkeypatch.setattr(bulk, "WHEEL_SHA256", sdk_hash)
    monkeypatch.setattr(roster, "WHEEL_SHA256", sdk_hash)
    sdk = {"wheel": str(wheel), "sha256": sdk_hash, "distribution_version": "0.9.4"}
    closes = {day: strict_close(tmp_path / ("close_" + day), day, calendar, sdk) for day in (D, E, F)}
    base = native_base(tmp_path / "base")
    ipo_dir = tmp_path / "ipo"
    ipo_dir.mkdir()
    ipo, master = write_ipo_fixture(ipo_dir)
    master_record = read(master)
    master_csv = ipo_dir / master_record["master_file"]
    pd.DataFrame([{"canonical_symbol": symbol, "provider_ipo_date": "2026-01-02", "board": "main"}
        for symbol in SYMBOLS]).to_csv(master_csv, index=False)
    master_record.update(master_sha256=digest(master_csv), instrument_count=len(SYMBOLS))
    save(master, master_record)
    model = tmp_path / "model_manifest.json"
    save(model, {"synthetic_score_math_substitute": True, "no_model_fitted": True,
        "feature_contract_id": producer.FEATURE_CONTRACT_ID, "feature_semantics": producer.FEATURE_SEMANTICS})
    monkeypatch.setattr(scoring, "load_generation", lambda path: SimpleNamespace(path=Path(path).resolve()))
    def fixed_score(generation, frame, *, day, model_kind, feature_provenance):
        scores = frame.copy()
        probability = [.05 + i / 100 for i in range(len(frame))]
        scores["model_hit5_probability"] = probability if model_kind == "lr" else list(reversed(probability))
        scores["paper_candidate_only"] = False
        complete = feature_provenance["stage_membership_complete"]
        stamp = clock["value"].isoformat()
        proof = {"schema": "shortline_frozen_single_date_score_v1", "day": day, "model_kind": model_kind,
            "status": "completed_frozen_single_date_probabilities_only" if complete else "partial_known_stage_frozen_single_date_probabilities_only",
            "scored_at": stamp, "score_available_at": stamp, "model_available_at": D + "T21:00:00+08:00",
            "model_refit": False, "score_rows": len(scores), "stage_counts": {str(s): int(scores.stage.eq(s).sum()) for s in (1, 2, 3)},
            "stage_membership_complete": complete, "unknown_stage_symbols": feature_provenance["unknown_stage_symbols"],
            "known_stage_partial_research_only": not complete, "batch_complete_for_target": False,
            "score_only_not_candidate_batch": True, "target_generation_allowed": False,
            "feature_source": feature_provenance, "feature_contract_id": feature_provenance["feature_contract_id"],
            "feature_semantics": feature_provenance["feature_semantics"], "generation_id": "tiny_fixed_generation",
            "manifest_file": str(generation.path), "manifest_sha256": digest(generation.path), **scoring.FLAGS}
        return {"scores": scores, "provenance": proof}
    monkeypatch.setattr(scoring, "score_day", fixed_score)
    def make_plan(day, execution, name, *, model_path=model, calendar_path=calendar):
        root = tmp_path / name
        root.mkdir()
        clock["value"] = datetime.fromisoformat(execution + "T02:00:00+08:00")
        source_folders = [closes[d] for d in (D, E, F) if d <= day]
        with chain.NativeHistoryChainSource(base, [], source_folders, calendar=calendar) as source:
            producer.build_from_source(source, root / "panel", through=day)
        clock["value"] = datetime.fromisoformat(execution + "T03:00:00+08:00")
        scoring.score_saved_generation(model_path, root / "panel/result.json", root / "scores")
        clock["value"] = datetime.fromisoformat(execution + "T03:30:00+08:00")
        policy.build_preopen_plan(root / "scores/publication.json", calendar_path, root / "plan")
        clock["value"] = datetime.fromisoformat("2026-10-20T20:00:00+08:00")
        return root / "plan/plan.json"
    first = make_plan(D, E, "first")
    second = make_plan(E, F, "second")
    return SimpleNamespace(root=tmp_path, clock=clock, calendar=calendar, closes=closes, ipo=ipo, master=master,
        plans=[first, second], run_dir=tmp_path / "persistent", model=model, make_plan=make_plan, sdk=sdk)


def execute(e, *, plans=None, days=(D, E, F), run_dir=None, through=None):
    return forward.run(e.plans if plans is None else plans, [e.closes[d] for d in days], e.ipo, e.master,
        e.run_dir if run_dir is None else run_dir, through=through)


def head(e):
    return read(e.run_dir / "head.json")


def test_saved_plan_strict_sources_trade_two_books_resume_NO_CHANGE_and_idempotency(evidence):
    e = evidence
    loaded = policy.load_preopen_plan(e.plans[0])
    for kind in ("lr", "lgbm"):
        assert loaded["decisions"][kind]["weights"] == {TARGET[kind]: .16}
    assert all(row["decision"] == "NO_CHANGE" and row["weights"] is None
        for row in policy.load_preopen_plan(e.plans[1])["decisions"].values())
    result = execute(e, plans=e.plans[:1], days=(D, E))
    assert result["status"] == "advanced_isolated_native_research_books"
    assert result["newly_committed_case_sessions"] == 2
    assert result["new_order_count"] == 2 and result["orders_generated"] is True
    assert result["metrics"] is None
    first = head(e)
    assert len(first["consumed_plans"]) == len(first["journal_files"]) == 1
    assert set(first["cases"]) == {"lr", "lgbm"}
    assert first["cases"]["lr"]["context"] != first["cases"]["lgbm"]["context"]
    for kind, case in first["cases"].items():
        state = case["state"]
        context = RunContext.from_dict(case["context"])
        book = Ledger(state, _config(100000, 5), context)
        order = state["orders"][E + ":" + TARGET[kind] + ":buy"]
        assert order["status"] == "filled" and order["qty"] == 1600 and order["fees"] == 516
        assert state["cash"] == 8_397_884 and state["day"] == E
        assert book.quantity(TARGET[kind]) == 1600
        assert book.quantity(TARGET["lgbm" if kind == "lr" else "lr"]) == 0
        assert book.available(TARGET[kind], E) == 0 and book.available(TARGET[kind], F) == 1600
        assert state["pending_targets"] is None
    before = (e.run_dir / "head.json").read_bytes()
    waiting = execute(e, days=(D, E))
    assert waiting["status"] == "waiting_for_exact_complete_raw_close"
    assert waiting["missing_source_days"] == [F] and waiting["account_reset"] is False
    assert waiting["new_order_count"] == 0 and waiting["orders_generated"] is False
    assert (e.run_dir / "head.json").read_bytes() == before
    assert Path(waiting["attempt_report"]).exists()
    resumed = execute(e)
    assert resumed["status"] == "advanced_isolated_native_research_books"
    assert resumed["newly_committed_case_sessions"] == 2
    assert resumed["new_order_count"] == 0 and resumed["orders_generated"] is False
    assert resumed["orders_present"] is True
    second = head(e)
    for kind, case in second["cases"].items():
        assert case["state"]["day"] == F and case["state"]["cash"] == 8_397_884
        assert len(case["state"]["orders"]) == 1
        assert case["state"]["fills"] == first["cases"][kind]["state"]["fills"]
        assert len(case["state"]["fills"]) == 2
        assert sum(fill["qty"] for fill in case["state"]["fills"].values()) == 1600
        assert sum(fill["fees"] for fill in case["state"]["fills"].values()) == 516
        assert Ledger(case["state"], _config(100000, 5), RunContext.from_dict(case["context"])).quantity(TARGET[kind]) == 1600
        assert case["daily"][-1]["original_plan_kind"] == "NO_CHANGE"
    before = (e.run_dir / "head.json").read_bytes()
    repeated = execute(e)
    assert repeated["status"] == "already_consumed_original_plan_prefix"
    assert repeated["new_order_count"] == 0 and repeated["orders_generated"] is False
    assert repeated["orders_present"] is True
    assert (e.run_dir / "head.json").read_bytes() == before


def test_missing_first_execution_never_creates_account_and_reference_difference_blocks_both(evidence, monkeypatch):
    e = evidence
    def forbidden(*args, **kwargs):
        raise AssertionError("No account creation before exact E raw exists")
    with monkeypatch.context() as patch:
        patch.setattr(forward, "new_state", forbidden)
        result = execute(e, plans=e.plans[:1], days=(D,))
    assert result["status"] == "waiting_for_exact_complete_raw_close"
    assert result["case_count"] == 0 and result["research_checkpoint_created"] is False
    assert result["missing_source_days"] == [E]
    assert not e.run_dir.exists()
    alternative = strict_close(e.root / "reference_changed", E, e.calendar, e.sdk,
        reference_override={TARGET["lr"]: 10.01})
    blocked_dir = e.root / "reference_blocked"
    blocked = forward.run(e.plans[:1], [e.closes[D], alternative], e.ipo, e.master, blocked_dir)
    assert blocked["status"] == "blocked_required_raw_reference_or_IPO_evidence"
    assert blocked["newly_committed_case_sessions"] == 0 and blocked["metrics"] is None
    assert blocked["new_order_count"] == 0 and blocked["orders_generated"] is False
    assert blocked["session_not_committed"] is True
    assert any("corporate_action_or_reference_change_unresolved_no_cash_imputation" in issue["reasons"]
        for issue in blocked["blocking_evidence"]["lr"]["issues"])
    assert blocked["blocking_evidence"]["lgbm"]["issues"] == []
    assert not (blocked_dir / "head.json").exists()


def test_committed_plan_model_calendar_spec_or_skipped_prefix_cannot_be_replaced(evidence):
    e = evidence
    execute(e, plans=e.plans[:1], days=(D, E))
    original_head = (e.run_dir / "head.json").read_bytes()
    alternate = e.make_plan(D, E, "same_day_other_plan")
    other_model = e.root / "other_model.json"
    save(other_model, {"synthetic_new_generation": True, "feature_contract_id": producer.FEATURE_CONTRACT_ID,
        "feature_semantics": producer.FEATURE_SEMANTICS})
    changed_model = e.make_plan(E, F, "other_model_plan", model_path=other_model)
    other_calendar = e.root / "other_calendar.json"
    other_calendar.write_bytes(e.calendar.read_bytes())
    changed_calendar = e.make_plan(E, F, "other_calendar_plan", calendar_path=other_calendar)
    skipped = e.make_plan(F, "2026-10-20", "skipped_source_day")
    for plans in ([alternate], [e.plans[0], changed_model], [e.plans[0], changed_calendar], [e.plans[0], skipped]):
        with pytest.raises(ValueError):
            execute(e, plans=plans)
        assert (e.run_dir / "head.json").read_bytes() == original_head
    changed = head(e)
    changed["binding"]["rule_spec"]["reference_policy"] = "synthetic_changed_policy"
    save(e.run_dir / "head.json", changed)
    before = (e.run_dir / "head.json").read_bytes()
    with pytest.raises(ValueError):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before


def test_lgbm_failure_after_real_Ledger_mark_discards_both_trials_and_preserves_checkpoint(evidence, monkeypatch):
    e = evidence
    execute(e, plans=e.plans[:1], days=(D, E))
    before = (e.run_dir / "head.json").read_bytes()
    original_mark = Ledger.mark_price
    observed = []
    def real_mark_then_fault(book, symbol, price, day, **kwargs):
        result = original_mark(book, symbol, price, day, **kwargs)
        if day == F and price == 1210 and book.context.account_id.endswith(":lgbm") and symbol == TARGET["lgbm"]:
            assert book.state["prices"][symbol] == 1210
            observed.append("lgbm_trial_actually_marked_close")
            raise ValueError("synthetic lgbm fault after real Ledger mark")
        return result
    monkeypatch.setattr(Ledger, "mark_price", real_mark_then_fault)
    result = execute(e)
    assert observed == ["lgbm_trial_actually_marked_close"]
    assert result["status"] == "failed_engine_session_no_commit"
    assert result["failed_model_kind"] == "lgbm" and result["both_model_trials_discarded"] is True
    assert result["newly_committed_case_sessions"] == 0
    assert result["new_order_count"] == 0 and result["orders_generated"] is False
    assert result["orders_present"] is True
    assert (e.run_dir / "head.json").read_bytes() == before
    assert len(head(e)["journal_files"]) == 1
    assert Path(result["attempt_report"]).exists()


@pytest.mark.parametrize("field", ["raw_sources_sha256", "source_day"])
def test_journal_reference_fields_cannot_be_changed_even_with_rebound_file_digest(evidence, field):
    e = evidence
    execute(e, plans=e.plans[:1], days=(D, E))
    checkpoint = head(e)
    account_states = deepcopy(checkpoint["cases"])
    reference = checkpoint["journal_files"][0]
    journal_file = Path(reference["file"])
    journal = read(journal_file)
    journal[field] = "0" * 64 if field == "raw_sources_sha256" else P
    save(journal_file, journal)
    reference.update(sha256=digest(journal_file), byte_count=journal_file.stat().st_size)
    save(e.run_dir / "head.json", checkpoint)
    before = (e.run_dir / "head.json").read_bytes()
    with pytest.raises(ValueError, match="Independent session journal"):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before
    assert head(e)["cases"] == account_states
