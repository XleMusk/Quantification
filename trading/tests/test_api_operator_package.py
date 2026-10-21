import json

from trading import settings
from trading.api import (
    _latest_operator_package_path,
    _latest_fixed_idea_frontier,
    _latest_fixed_profitability_diagnostic,
    _latest_alpha158_linear_diagnostic,
    _latest_alpha158_lightgbm_diagnostic,
    _research_evidence_consumer_summary,
    _latest_research_exception_view,
    _latest_saved_comparison_diagnostic,
    _latest_stockdb_forward_compare,
    _normalize_blockers,
    _same_ledger_bundle_reconciliation_view,
    _ths_journal_reconciliation_view,
    operator_package_decision_queue,
    operator_package_lifecycle,
    paper_preview_readiness,
    operator_package,
)


def test_normalize_blockers_preserves_semicolon_delimited_review_artifacts():
    assert _normalize_blockers("fresh_holdout_required; account_observation_required") == [
        "fresh_holdout_required",
        "account_observation_required",
    ]
    assert _normalize_blockers(["cash_missing", "t1_missing"]) == [
        "cash_missing",
        "t1_missing",
    ]


def test_broker_alerts_expose_current_persistent_bundle_boundary(monkeypatch):
    import trading.api as api_module

    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "isolated_rehearsal_only",
        "source_scope": "isolated_rehearsal",
        "source_manifest_status": "verified",
        "rows": [{
            "order_id": "SYN-1",
            "status": "broker_order_without_ledger_plan",
            "issues": [],
        }],
        "ledger_mutated": False,
    }
    journal = {"status": "no_saved_plans", "ledger_mutated": False}
    readiness = {
        "status": "templates_blank",
        "preview": {"status": "templates_blank"},
        "source_manifest_status": "not_ready",
        "source_manifest_issues": [],
        "profile": {
            "mapping_validation": {"status": "shape_valid", "issues": []},
        },
    }
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: journal)
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)
    monkeypatch.setattr(api_module, "ths_observation_bundle_readiness", lambda: readiness)

    result = api_module.operator_package_broker_observation_alerts()

    assert result["status"] == "isolated_observation_alerts_only"
    assert result["alert_source_selection"]["current_persistent_bundle_status"] == "templates_blank"
    assert result["alert_source_selection"]["reconciliation_source_scope"] == "isolated_rehearsal"
    assert result["alert_source_selection"]["reconciliation_is_account_evidence"] is False
    assert result["alert_source_selection"]["synthetic_source_is_not_account_evidence"] is True
    assert result["persistent_bundle_readiness"]["bundle_is_account_evidence"] is False
    assert result["alerts"][0]["live_alert"] is False
    assert "synthetic rehearsal evidence" in result["next_action"]


def test_complete_bundle_selection_prefers_persistent_export_over_newer_rehearsal(tmp_path, monkeypatch):
    import trading.api as api_module
    from trading.ths.structured_observation import build_source_manifest

    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)

    def write_package(root, scope, source):
        export_root = root / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        source_paths = {}
        for kind in ("account", "orders", "trades", "cashflows"):
            path = export_root / f"{kind}.csv"
            path.write_text(f"{kind}\n", encoding="utf-8")
            source_paths[kind] = str(path)
        package = {
            "schema": "ths_structured_observation_package_v1",
            "status": "complete_structured_observation_package",
            "source": source,
            "source_scope": scope,
            "source_paths": source_paths,
            "source_manifest": build_source_manifest(source_paths),
            "account_alias": "test-account",
            "holdings_candidate_semantic_verified": True,
            "ready_for_ledger_reconciliation": True,
            "execution_admitted": False,
            "ledger_mutated": False,
            "order_reconciliation": {
                "status": "ready_for_ledger_reconciliation",
                "order_count": 0,
                "trade_count": 0,
                "cashflow_count": 0,
            },
        }
        package_path = root / "structured_observation_package_v1.json"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        return package_path

    persistent = write_package(
        tmp_path / "ths" / "observation_bundle" / "structured" / "persistent",
        "persistent_user_export",
        "validated_ths_user_export_bundle",
    )
    rehearsal = write_package(
        tmp_path / "research" / "ths_bundle_rehearsal" / "newer",
        "isolated_rehearsal",
        "isolated_ths_export_rehearsal",
    )
    assert rehearsal.exists()

    result = api_module._latest_complete_structured_observation_package()

    assert result["source_scope"] == "persistent_user_export"
    assert result["source_file"] == str(persistent)
    assert result["current_ledger_import_candidate"] is True


def test_latest_operator_package_prefers_execution_day_over_file_mtime(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_ROOT", data_root)
    older = data_root / "research" / "opening_session_runs" / "2026-10-01" / "offline_operator_package_v1.json"
    newer = data_root / "research" / "opening_session_runs_factor_x" / "2026-10-12" / "offline_operator_package_v1.json"
    for path, execution_day in ((older, "2026-10-01"), (newer, "2026-10-12")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "offline_operator_package_v1",
            "execution_day": execution_day,
        }), encoding="utf-8")

    assert _latest_operator_package_path() == newer


def test_latest_stockdb_forward_compare_prefers_selected_strategy(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_ROOT", data_root)
    root = data_root / "research" / "stockdb_forward_compare"
    old = root / "old" / "result.json"
    current = root / "current" / "result.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    current.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({
        "schema": "stockdb_forward_compare_v1",
        "source_cutoff": "2026-10-09",
        "candidates": [{"candidate": "stock_factor_candidate_family_balanced_ridge", "status": "partial", "coverage": 0.05}],
    }), encoding="utf-8")
    current.write_text(json.dumps({
        "schema": "stockdb_forward_compare_v1",
        "source_cutoff": "2026-10-08",
        "candidates": [{"candidate": "low_volatility_252_20", "status": "complete", "coverage": 1.0, "target_symbols": 20, "observed_symbols": 20}],
    }), encoding="utf-8")

    result = _latest_stockdb_forward_compare("low_volatility_252_20")

    assert result["selected_strategy_match"] is True
    assert result["candidates"][0]["candidate"] == "low_volatility_252_20"
    assert result["candidates"][0]["coverage_complete"] is True


def test_latest_saved_comparison_diagnostic_prefers_complete_selected_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    root = tmp_path / "outputs" / "research_comparisons"
    older = root / "older" / "comparison.json"
    current = root / "current" / "comparison.json"
    older.parent.mkdir(parents=True, exist_ok=True)
    current.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "status": "compared_saved_accounts",
        "start": "2026-01-05",
        "end": "2026-09-23",
        "requested_cases": 1,
        "comparable_cases": 1,
        "same_cost_settings": True,
    }
    older.write_text(json.dumps({
        **base,
        "cases": [{"signal": "momentum_12_1_20", "status": "comparable", "metrics": {"net_return": 0.1}}],
    }), encoding="utf-8")
    current.write_text(json.dumps({
        **base,
        "cases": [{
            "signal": "low_volatility_252_20",
            "status": "comparable",
            "metrics": {"net_return": -0.04, "max_drawdown": -0.11},
        }],
    }), encoding="utf-8")

    result = _latest_saved_comparison_diagnostic(
        "low_volatility_252_20",
        {"start": "2026-01-05", "end": "2026-09-23", "same_cost_settings": True},
    )

    assert result["status"] == "completed_research_only"
    assert result["selected_strategy_match"] is True
    assert result["selected_case"]["net_return"] == -0.04
    assert result["execution_admitted"] is False


def test_latest_fixed_idea_frontier_is_research_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "auto_research_frontiers" / "current" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "quant_research_idea_frontier_run_v1",
        "fresh_holdout_claimed": False,
        "frontier": {
            "source_run": "fixed-source",
            "source_period": {"start": "2026-01-01", "end": "2026-09-30"},
            "candidates": [{
                "candidate": "equal_weight",
                "runner": "run_equal_weight",
                "description": "fixed candidate",
                "status": "completed",
                "latest": {
                    "run": "candidate-run",
                    "return": 0.01,
                    "max_drawdown": -0.08,
                    "sharpe_zero_rf_252": 0.2,
                },
            }],
        },
    }), encoding="utf-8")

    result = _latest_fixed_idea_frontier()

    assert result["status"] == "completed_research_only"
    assert result["candidates"][0]["candidate"] == "equal_weight"
    assert result["candidates"][0]["net_return"] == 0.01
    assert result["execution_admitted"] is False


def test_latest_fixed_profitability_diagnostic_is_compact_and_research_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "market_replay" / "current" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "completed_profitability_diagnostics",
        "research_family": "economic_profitability_reporting_calendar_v1",
        "start": "2025-01-02",
        "end": "2026-09-30",
        "protocol": {"financial_receipt": {
            "symbols": 3568,
            "report_rows": 52034,
            "declared_symbols": 5367,
            "symbols_with_reports": 3562,
            "publication_policy": "pubDate strictly earlier than decision session",
            "revision_safe_pit_verified": False,
        }},
        "results": [{
            "signal": "risk_momentum_profitability",
            "status": "completed",
            "sessions": 424,
            "net_return": 0.03,
            "max_drawdown": -0.10,
            "sharpe_zero_rf_252": 0.24,
            "fees": 1605.38,
            "stale_valuation_days": 2,
            "orders": 265,
            "fills": 437,
        }],
    }), encoding="utf-8")

    result = _latest_fixed_profitability_diagnostic()

    assert result["status"] == "completed_research_only"
    assert result["financial_receipt"]["symbols"] == 3568
    assert result["candidates"][0]["net_return"] == 0.03
    assert result["candidates"][0]["execution_admitted"] is False
    assert result["execution_admitted"] is False


def test_latest_alpha158_linear_diagnostic_is_research_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "market_replay" / "current" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "completed_alpha158_linear_diagnostics",
        "research_family": "alpha158_linear_ensemble",
        "dataset": "frozen-alpha158",
        "scope": "full_source_sh_sz_research_only",
        "symbols": 5368,
        "start": "2026-01-05",
        "end": "2026-09-30",
        "sessions": 181,
        "matched_rows": {"evaluation_eligible": 877249},
        "training_dates": 236,
        "last_training_label_exit": "2024-12-31 00:00:00",
        "protocol": {
            "id": "alpha158_fixed_linear_allseed_comparison_v1",
            "segments": {"train": ["2024-01-02", "2024-12-31"], "test": ["2026-01-01", "2026-09-30"]},
            "fresh_holdout_claimed": False,
        },
        "results": [{
            "signal": "alpha158_allseed_rank_mean",
            "status": "completed",
            "completed_sessions": 181,
            "net_return": -0.015,
            "max_drawdown": -0.31,
            "sharpe_zero_rf_252": 0.06,
            "fees": 3104.71,
        }],
        "prediction_diagnostics": [{
            "signal": "alpha158_allseed_rank_mean",
            "prediction_rows": 877249,
            "ic_days": 175,
            "mean_rank_ic": 0.041,
            "hac_t_lag5": 1.93,
        }],
        "strategy_validated": False,
    }), encoding="utf-8")

    result = _latest_alpha158_linear_diagnostic()

    assert result["status"] == "completed_research_only"
    assert result["candidates"][0]["signal"] == "alpha158_allseed_rank_mean"
    assert result["prediction_diagnostics"][0]["mean_rank_ic"] == 0.041
    assert result["fresh_holdout_claimed"] is False
    assert result["execution_admitted"] is False


def test_latest_alpha158_lightgbm_diagnostic_is_research_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "factor_mining" / "current" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "completed",
        "experiment_kind": "alpha158_ingestion_ledger_comparison",
        "config": {
            "dataset": "frozen-prices",
            "segments": {"test": ["2026-01-01", "2026-09-30"]},
        },
        "dataset_manifest": {"scope": "full_source_sh_sz_research_only"},
        "data_quality": {"symbols": 5368},
        "fresh_holdout_claimed": False,
        "strategy_validated": False,
        "model_metrics": [{
            "variant": "B0", "seed": 29, "split": "test", "features": 158,
            "best_iteration": 8, "ic_days": 175, "mean_rank_ic": 0.039,
        }],
        "portfolio_metrics": [{
            "variant": "B0", "seed": 29, "status": "completed",
            "completed_sessions": 181, "net_return": 0.048,
            "max_drawdown": -0.287, "sharpe_zero_rf_252": 0.365,
            "fees": 3063.0, "orders": 281, "fills": 540,
        }],
        "paired_deltas": [{"seed": 29, "delta_net_return": 0.292}],
    }), encoding="utf-8")

    result = _latest_alpha158_lightgbm_diagnostic()

    assert result["status"] == "completed_research_only"
    assert result["model"] == "LightGBM"
    assert result["candidates"][0]["signal"] == "alpha158_seed29"
    assert result["model_metrics"][0]["mean_rank_ic"] == 0.039
    assert result["execution_admitted"] is False


def test_research_evidence_consumer_summary_keeps_replay_research_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    replay_root = tmp_path / "outputs" / "factor_mining" / "current"
    replay_root.mkdir(parents=True, exist_ok=True)
    (replay_root / "run.json").write_text(json.dumps({
        "status": "completed",
        "experiment_kind": "alpha158_ingestion_ledger_comparison",
        "config": {"segments": {"test": ["2026-01-01", "2026-09-30"]}},
        "data_quality": {"symbols": 10},
        "portfolio_metrics": [{
            "variant": "B0", "seed": 29, "status": "completed",
            "completed_sessions": 181, "net_return": 0.048,
        }],
        "model_metrics": [],
        "fresh_holdout_claimed": False,
        "strategy_validated": False,
    }), encoding="utf-8")

    summary = _research_evidence_consumer_summary("alpha158_seed29")

    assert summary["status"] == "research_only"
    assert summary["alpha158_lightgbm"]["selected_strategy_match"] is True
    assert summary["alpha158_lightgbm"]["candidate_count"] == 1
    assert summary["automatic_promotion"] is False
    assert summary["execution_admitted"] is False


def test_ths_journal_reconciliation_view_keeps_same_ledger_receipt_boundary(monkeypatch):
    from trading.ths import journal

    monkeypatch.setattr(journal, "status", lambda: {
        "paused": True,
        "plans": [{
            "id": "plan-1",
            "account_alias": "sim",
            "trading_date": "2026-10-12",
            "status": "observed_filled",
            "contract_id": "broker-1",
            "result": {
                "status": "observed_filled",
                "account_reconciliation": {"status": "matched_cash_and_positions"},
                "account_cash_reconciled": True,
                "account_positions_reconciled": True,
                "orders_observed_at": "2026-10-12T10:00:00+08:00",
                "trades_observed_at": "2026-10-12T10:01:00+08:00",
                "ledger_import": {
                    "run_id": "paper-run",
                    "source_ref": "receipt-1",
                    "ledger_imported": True,
                },
            },
        }],
    })

    result = _ths_journal_reconciliation_view()

    assert result["status"] == "ledger_imported"
    assert result["ledger_mutated"] is True
    assert result["plans"][0]["account_reconciliation_status"] == "matched_cash_and_positions"
    assert result["plans"][0]["ledger_imported"] is True
    assert result["execution_admitted"] is False
    assert result["client_operation"] is False


def _complete_bundle_for_ledger_join():
    return {
        "schema": "ths_structured_observation_package_v1",
        "status": "complete_structured_observation_package",
        "source_scope": "persistent_user_export",
        "source_file": "bundle.json",
        "source_manifest_status": "verified",
        "source_manifest_issues": [],
        "account_alias": "sim",
        "observation_window": {"trading_date": "2026-10-12"},
        "ready_for_ledger_reconciliation": True,
        "order_reconciliation": {
            "status": "ready_for_ledger_reconciliation",
            "linkage": {
                "order_count": 1,
                "trade_count": 1,
                "cashflow_count": 1,
                "order_summaries": [{
                    "order_id": "O1",
                    "symbol": "000001.SZSE",
                    "side": "buy",
                    "order_qty": 100,
                    "order_filled_qty": 100,
                    "trade_filled_qty": 100,
                    "trade_gross_cents": 100000,
                    "fees_cents": 500,
                    "cash_change_cents": -100500,
                    "status": "matched",
                }],
            },
        },
    }


def test_same_ledger_bundle_reconciliation_joins_imported_receipt(monkeypatch):
    import trading.api as api_module

    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: _complete_bundle_for_ledger_join())
    from trading.ths import journal

    monkeypatch.setattr(journal, "status", lambda: {
        "plans": [{
            "id": "P1",
            "account_alias": "sim",
            "trading_date": "2026-10-12",
            "contract_id": "O1",
            "symbol": "000001.SZSE",
            "side": "buy",
            "qty": 100,
            "status": "observed_filled",
            "result": {
                "filled_qty": 100,
                "gross_executed_cents": 100000,
                "confirmed_account_reconciliation": {
                    "fees_cents": 500,
                    "cash_change_cents": -100500,
                },
                "ledger_import": {"ledger_imported": True, "run_id": "R1"},
            },
        }],
    })

    result = _same_ledger_bundle_reconciliation_view()

    assert result["status"] == "ledger_reconciled"
    assert result["summary"]["matched_count"] == 1
    assert result["summary"]["ledger_imported_count"] == 1
    assert result["rows"][0]["status"] == "matched_imported"
    assert result["rows"][0]["tca"]["effective_fill_price_rmb"] == "10.0000"
    assert result["rows"][0]["tca"]["fees_bps_on_gross"] == "50.0000"
    assert result["execution_admitted"] is False


def test_same_ledger_bundle_reconciliation_blocks_changed_source_manifest(monkeypatch):
    import trading.api as api_module

    package = _complete_bundle_for_ledger_join()
    package["source_manifest_status"] = "source_changed"
    package["source_manifest_issues"] = [{"kind": "trades", "reason": "sha256_mismatch"}]
    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: package)

    result = _same_ledger_bundle_reconciliation_view()

    assert result["status"] == "reconciliation_required"
    assert result["issues"][0]["code"] == "source_bundle_changed"
    assert result["summary"]["issue_count"] == 1
    assert result["execution_admitted"] is False


def test_same_ledger_bundle_reconciliation_blocks_unverified_persistent_manifest(monkeypatch):
    import trading.api as api_module

    package = _complete_bundle_for_ledger_join()
    package.pop("source_manifest_status")
    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: package)

    result = _same_ledger_bundle_reconciliation_view()

    assert result["status"] == "reconciliation_required"
    assert result["issues"][0]["code"] == "source_manifest_unverified"
    assert result["summary"]["issue_count"] == 1
    assert result["execution_admitted"] is False


def test_same_ledger_bundle_reconciliation_surfaces_fee_and_import_gap(monkeypatch):
    import trading.api as api_module
    from trading.ths import journal

    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: _complete_bundle_for_ledger_join())
    monkeypatch.setattr(journal, "status", lambda: {
        "plans": [{
            "id": "P1",
            "account_alias": "sim",
            "trading_date": "2026-10-12",
            "contract_id": "O1",
            "symbol": "000001.SZSE",
            "side": "buy",
            "qty": 100,
            "status": "observed_filled",
            "result": {
                "filled_qty": 100,
                "gross_executed_cents": 100000,
                "confirmed_account_reconciliation": {
                    "fees_cents": 600,
                    "cash_change_cents": -100600,
                },
                "ledger_import": {"ledger_imported": False},
            },
        }],
    })

    result = _same_ledger_bundle_reconciliation_view()
    codes = {item["code"] for item in result["issues"]}

    assert result["status"] == "reconciliation_required"
    assert {"fees_cents_mismatch", "cash_change_cents_mismatch", "ledger_import_missing"} <= codes
    assert result["rows"][0]["status"] == "reconciliation_required"


def test_same_ledger_bundle_reconciliation_surfaces_broker_linkage_defect(monkeypatch):
    import trading.api as api_module
    from trading.ths import journal

    package = _complete_bundle_for_ledger_join()
    package["order_reconciliation"]["linkage"]["filled_quantity_mismatches"] = [{
        "order_id": "O1", "order_filled_qty": 100, "trade_filled_qty": 90,
    }]
    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: package)
    monkeypatch.setattr(journal, "status", lambda: {"plans": []})

    result = _same_ledger_bundle_reconciliation_view()

    assert result["status"] == "reconciliation_required"
    assert any(item["code"] == "broker_fill_quantity_mismatch" for item in result["issues"])


def test_ths_observation_health_surfaces_saved_evidence_gaps_without_io(monkeypatch):
    import trading.api as api_module

    monkeypatch.setattr(api_module, "ths_native_observation", lambda: {
        "status": "candidate_only",
        "account_summary": {
            "status": "read",
            "observed_at": "2026-10-10T00:00:00+00:00",
        },
        "structured_observation_package": {"source_scope": "native_readonly_receipt"},
        "orders_read": False,
        "trades_read": False,
    })
    monkeypatch.setattr(api_module, "_latest_structured_observation_package", lambda: {
        "status": "partial_structured_observation_package",
        "source_scope": "native_readonly_receipt",
        "ready_for_ledger_reconciliation": False,
        "orders_observed": False,
        "trades_observed": False,
        "cashflows_observed": False,
        "row_counts": {"account": 1, "orders": 0, "trades": 0, "cashflows": 0},
    })
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: {
        "status": "isolated_rehearsal_only",
        "summary": {"issue_count": 0},
        "issues": [{"order_id": "O1", "code": "persistent_export_required"}],
        "ledger_mutated": False,
    })
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "plans_visible_pending_or_unimported",
    })

    result = api_module.ths_observation_health()

    assert result["status"] == "attention_required"
    assert result["transport"]["broker_io_attempted"] is False
    assert result["transport"]["submission_enabled"] is False
    assert {
        "native_account_observation_stale",
        "structured_orders_missing",
        "structured_trades_missing",
        "structured_cashflows_missing",
        "persistent_ths_export_required",
    } <= set(result["alerts"])
    assert result["same_ledger"]["issues"][0]["code"] == "persistent_export_required"
    assert result["same_ledger"]["detail"]["status"] == "isolated_rehearsal_only"
    assert result["ledger_mutated"] is False


def test_session_reconcile_returns_same_ledger_bundle_detail(monkeypatch):
    import trading.api as api_module
    import trading.ths.ledger_import as ledger_import

    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"matched_count": 1, "issue_count": 0},
        "issues": [],
        "ledger_mutated": True,
    }
    monkeypatch.setattr(ledger_import, "reconcile_session", lambda *args, **kwargs: {
        "changed": True,
        "native_verified": False,
    })
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.reconcile_ths_simulated_ledger_session(
        api_module.ThsSimulatedLedgerReconcileInput(
            run_id="a" * 32,
            confirm_reconcile=True,
        )
    )

    assert result["paper_ledger_mutation"] is True
    assert result["same_ledger_bundle_reconciliation"] is bundle


def test_cash_movements_api_requires_confirmation_and_imports_same_ledger(monkeypatch):
    import trading.api as api_module
    import trading.ths.ledger_import as ledger_import

    pending = api_module.import_ths_simulated_ledger_movements(
        api_module.ThsSimulatedLedgerMovementsInput(run_id="a" * 32)
    )
    assert pending["status"] == "confirmation_required"
    assert pending["paper_ledger_mutation"] is False
    assert pending["execution_admitted"] is False

    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "no_complete_bundle",
        "summary": {"issue_count": 0},
        "ledger_mutated": True,
    }
    monkeypatch.setattr(ledger_import, "import_movements", lambda run_id, observed_at=None: {
        "run_id": run_id,
        "changed": True,
        "new_movements": 2,
        "cash_change_cents": 1234,
        "observed_at": observed_at or "2026-10-12T09:30:00+08:00",
        "submit_enabled": False,
    })
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.import_ths_simulated_ledger_movements(
        api_module.ThsSimulatedLedgerMovementsInput(
            run_id="a" * 32,
            observed_at="2026-10-12T09:30:00+08:00",
            confirm_import=True,
        )
    )

    assert result["status"] == "cash_statement_imported_to_paper_ledger"
    assert result["paper_ledger_mutation"] is True
    assert result["receipt"]["new_movements"] == 2
    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["client_operation"] is False
    assert result["submission_enabled"] is False


def test_recovery_review_records_manual_hold_without_retry(monkeypatch, tmp_path):
    import trading.api as api_module

    index_path = tmp_path / "paper_recovery_handoff_index.json"
    handoff_path = tmp_path / "paper_recovery_handoff_v1.json"
    handoff_path.write_text(json.dumps({
        "schema": "paper_recovery_handoff_v1",
        "strategy_id": "momentum_12_1_20",
        "rows": [{
            "scenario_id": "unknown_submission_reconcile_required",
            "lifecycle_status": "blocked",
            "recovery_action": "stop_and_reconcile_before_retry",
            "required_observation": "account_orders_trades",
        }],
    }), encoding="utf-8")
    index_path.write_text(json.dumps({
        "schema": "paper_recovery_handoff_index_v1",
        "rows": [{"strategy_id": "momentum_12_1_20", "handoff_json": str(handoff_path)}],
    }), encoding="utf-8")
    package = {"execution_day": "2026-10-12", "package_freshness": "fresh"}
    monkeypatch.setattr(api_module, "operator_package", lambda: package)
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: index_path if name == "paper_recovery_handoff_index" else None)
    monkeypatch.setattr(api_module, "_operator_package_session_root", lambda _package: tmp_path)

    pending = api_module.submit_operator_package_recovery_review(
        api_module.RecoveryReviewInput(
            strategy_id="momentum_12_1_20",
            scenario_id="unknown_submission_reconcile_required",
            decision="acknowledge_and_hold",
            reviewer="operator",
            reviewed_at="2026-10-12T09:30:00+08:00",
            notes="等待账户、委托、成交观察后再处理",
        )
    )
    assert pending["status"] == "confirmation_required"
    assert pending["retry_permitted"] is False

    result = api_module.submit_operator_package_recovery_review(
        api_module.RecoveryReviewInput(
            strategy_id="momentum_12_1_20",
            scenario_id="unknown_submission_reconcile_required",
            decision="acknowledge_and_hold",
            reviewer="operator",
            reviewed_at="2026-10-12T09:30:00+08:00",
            notes="等待账户、委托、成交观察后再处理",
            confirm_review=True,
        )
    )
    assert result["status"] == "human_recovery_review_recorded"
    assert result["retry_permitted"] is False
    assert result["automatic_recovery"] is False
    assert result["execution_admitted"] is False


def test_audit_event_consumer_keeps_paper_events_separate_from_broker_state(monkeypatch, tmp_path):
    import trading.api as api_module

    event_path = tmp_path / "paper_audit_event_log.csv"
    event_path.write_text(
        "event_id,event_sequence,event_type,state\n"
        "E1,1,score_bundle_loaded,paper_only\n"
        "E2,2,paper_release_blocked,blocked\n",
        encoding="utf-8",
    )
    jsonl_path = tmp_path / "paper_audit_event_log.jsonl"
    jsonl_path.write_text(
        '{"event_id":"E3","event_sequence":3,"event_type":"paper_hold","state":"held"}\n',
        encoding="utf-8",
    )
    package = {"artifact_pointers": {
        "paper_audit_event_log": str(event_path),
        "paper_audit_event_log_jsonl": str(jsonl_path),
    }}
    monkeypatch.setattr(api_module, "operator_package", lambda: package)
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "ledger_mutated": False,
        "status": "paper_only",
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "isolated_rehearsal_only",
        "summary": {"issue_count": 0},
        "ledger_mutated": False,
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.operator_package_audit_events()

    assert result["status"] == "paper_event_log_only"
    assert result["event_count"] == 2
    assert result["state_counts"] == {"paper_only": 1, "blocked": 1}
    assert result["event_type_counts"]["paper_release_blocked"] == 1
    assert result["broker_state_observed"] is False
    assert result["execution_admitted"] is False
    assert result["same_ledger_bundle_reconciliation"] is bundle


def test_audit_event_consumer_reads_jsonl_when_csv_is_missing(monkeypatch, tmp_path):
    import trading.api as api_module

    jsonl_path = tmp_path / "paper_audit_event_log.jsonl"
    jsonl_path.write_text(
        '{"event_id":"E1","event_sequence":1,"event_type":"paper_only","state":"held"}\n',
        encoding="utf-8",
    )
    package = {"artifact_pointers": {"paper_audit_event_log_jsonl": str(jsonl_path)}}
    monkeypatch.setattr(api_module, "operator_package", lambda: package)
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "ledger_mutated": False, "status": "paper_only",
    })
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "isolated_rehearsal_only",
        "summary": {"issue_count": 0},
        "ledger_mutated": False,
    })

    result = api_module.operator_package_audit_events()

    assert result["event_count"] == 1
    assert result["events"][0]["event_id"] == "E1"


def test_operator_package_surfaces_prior_same_ledger_import_without_client_action(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    package_root = tmp_path / "outputs" / "opening_session_runs" / "2026-10-12"
    package_root.mkdir(parents=True)
    package = {
        "schema": "offline_operator_package_v1",
        "execution_day": "2026-10-12",
        "strategy_candidates": [],
    }
    package_path = package_root / "offline_operator_package_v1.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    from trading.ths import journal

    monkeypatch.setattr(journal, "status", lambda: {
        "paused": True,
        "submission_enabled": False,
        "plans": [{
            "id": "plan-1",
            "account_alias": "sim",
            "trading_date": "2026-10-12",
            "status": "observed_filled",
            "result": {
                "status": "observed_filled",
                "ledger_import": {
                    "run_id": "paper-run",
                    "source_ref": "receipt-1",
                    "ledger_imported": True,
                },
            },
        }],
    })

    result = operator_package()

    assert result["same_ledger_reconciliation_status"] == "isolated_rehearsal_only"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False


def test_release_readiness_reuses_same_ledger_receipt(monkeypatch):
    import trading.api as api_module

    monkeypatch.setattr(api_module, "paper_preview_latest", lambda: {
        "execution_day": "2026-10-12",
        "release_blockers": [],
        "status": "observed",
    })
    monkeypatch.setattr(api_module, "ths_native_observation", lambda: {
        "missing_evidence": [],
        "account_summary": {"status": "read_background"},
        "full_account_observation": False,
        "orders_read": False,
        "trades_read": False,
        "client_session": {},
    })
    monkeypatch.setattr(api_module, "_latest_structured_ths_bundle", lambda: {"status": "partial"})
    monkeypatch.setattr(api_module, "_latest_structured_observation_package", lambda: {
        "status": "partial_structured_observation_package",
        "account_summary_observed": True,
        "account_structured_observed": False,
        "account_summary": {"observed_at": "2026-10-12T09:30:00+08:00"},
        "holdings_candidate_observed": True,
        "holdings_candidate_semantic_verified": False,
        "orders_observed": False,
        "trades_observed": False,
        "cashflows_observed": False,
        "structured_receipts": {},
        "empty_activity_exports": {},
        "ready_for_ledger_reconciliation": False,
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        "order_reconciliation": {"status": "blocked"},
    })
    monkeypatch.setattr(api_module, "_latest_structured_quote", lambda: {
        "status": "missing",
        "price_count": 0,
        "coverage_status": "unknown",
        "missing_symbols": [],
    })
    monkeypatch.setattr(api_module, "_quote_observation_preview", lambda: {"status": "missing"})
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })

    result = paper_preview_readiness()

    assert result["same_ledger_reconciliation_status"] == "isolated_rehearsal_only"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_lifecycle_view_reuses_same_ledger_receipt(monkeypatch):
    import trading.api as api_module

    monkeypatch.setattr(api_module, "operator_package", lambda: {
        "execution_day": "2026-10-12",
        "strategy_id": "momentum",
    })
    monkeypatch.setattr(api_module, "_research_evidence_consumer_summary", lambda _: {
        "status": "research_only",
    })
    monkeypatch.setattr(api_module, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: {})
    monkeypatch.setattr(api_module, "paper_preview_readiness", lambda: {
        "status": "blocked",
        "release_ready": False,
    })
    monkeypatch.setattr(api_module, "_package_artifact", lambda *_: None)
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })

    result = operator_package_lifecycle()

    assert result["same_ledger_reconciliation_status"] == "structured_package_not_ready"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_decision_queue_reuses_same_ledger_receipt(tmp_path, monkeypatch):
    import trading.api as api_module

    queue_path = tmp_path / "decision_queue.json"
    queue_path.write_text(json.dumps({
        "schema": "offline_operator_decision_queue_v1",
        "rows": [],
    }), encoding="utf-8")
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: queue_path if name == "offline_operator_decision_queue_json" else None)
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "reconciliation_required",
        "summary": {"issue_count": 1},
        "issues": [{"order_id": "O1", "code": "fees_cents_mismatch"}],
        "ledger_mutated": False,
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = operator_package_decision_queue()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_bundle_reconciliation"]["summary"]["issue_count"] == 1
    assert result["same_ledger_reconciliation_status"] == "reconciliation_required"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_execution_evidence_reuses_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module

    paths = {}
    for name in ("paper_execution_cost_stress", "tca_pretrade", "risk_checks", "child_order_schedule"):
        path = tmp_path / f"{name}.csv"
        path.write_text("column\n", encoding="utf-8")
        paths[name] = path
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "source_scope": "isolated_rehearsal",
        "summary": {"issue_count": 0},
        "rows": [{
            "broker": {
                "order_qty": 100,
                "trade_filled_qty": 100,
                "gross_cents": 100000,
                "fees_cents": 500,
            },
            "tca": {
                "adverse_slippage_bps": None,
            },
        }],
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.operator_package_execution_evidence()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False
    assert result["observed_fill_tca"]["status"] == "isolated_rehearsal_only"
    assert result["observed_fill_tca"]["effective_fill_price_rmb"] == "10.0000"
    assert result["observed_fill_tca"]["fees_cents"] == 500
    assert result["child_order_reconciliation"]["status"] == "not_available"
    assert result["child_order_reconciliation"]["execution_admitted"] is False


def test_execution_evidence_exposes_paper_child_reconciliation(tmp_path, monkeypatch):
    import trading.api as api_module

    paths = {}
    for name in ("paper_execution_cost_stress", "tca_pretrade", "risk_checks", "child_order_schedule"):
        path = tmp_path / f"{name}.csv"
        path.write_text("column\n", encoding="utf-8")
        paths[name] = path
    reconciliation_csv = tmp_path / "paper_child_order_reconciliation.csv"
    reconciliation_csv.write_text(
        "parent_sequence,strategy_id,parent_execution_status,reconciliation_status,residual_qty\n"
        "P1,s1,synthetic_full_fill_reference_price,pass_synthetic_complete,0\n",
        encoding="utf-8",
    )
    reconciliation_json = tmp_path / "paper_child_order_reconciliation.json"
    reconciliation_json.write_text(json.dumps({
        "schema": "paper_child_order_reconciliation_v1",
        "status": "paper_only",
        "parent_count": 1,
        "scheduled_parent_count": 1,
        "complete_synthetic_parent_count": 1,
        "rejected_parent_count": 0,
        "mismatch_count": 0,
        "real_broker_state": False,
        "execution_admitted": False,
    }), encoding="utf-8")
    paths["paper_child_order_reconciliation"] = reconciliation_csv
    paths["paper_child_order_reconciliation_summary"] = reconciliation_json
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "paper_only",
        "ledger_mutated": False,
    })
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "no_complete_bundle",
        "ledger_mutated": False,
    })

    result = api_module.operator_package_execution_evidence()

    reconciliation = result["child_order_reconciliation"]
    assert reconciliation["status"] == "paper_only"
    assert reconciliation["parent_count"] == 1
    assert reconciliation["mismatch_count"] == 0
    assert reconciliation["rows"] == [{
        "parent_sequence": "P1",
        "strategy_id": "s1",
        "parent_execution_status": "synthetic_full_fill_reference_price",
        "reconciliation_status": "pass_synthetic_complete",
        "residual_qty": "0",
    }]
    assert reconciliation["by_strategy"]["s1"]["complete_synthetic_parent_count"] == 1
    assert reconciliation["real_broker_state"] is False
    assert reconciliation["execution_admitted"] is False


def test_portfolio_controls_reuses_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module

    paths = {}
    for name in ("scenario_analysis", "allocation_settlement_projection"):
        path = tmp_path / f"{name}.csv"
        path.write_text("row_type\n", encoding="utf-8")
        paths[name] = path
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"issue_count": 0},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.operator_package_portfolio_controls()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_observation_journal_status_reuses_same_ledger_receipt(monkeypatch):
    import trading.api as api_module
    from trading.ths import journal

    monkeypatch.setattr(journal, "initialize", lambda: None)
    monkeypatch.setattr(journal, "status", lambda: {
        "paused": True,
        "plans": [],
    })
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"issue_count": 0},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.ths_observation_bundle_journal_status()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_release_controls_reuses_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module

    paths = {}
    for name in ("exception_alerts", "execution_controls"):
        path = tmp_path / f"{name}.csv"
        path.write_text("column\n", encoding="utf-8")
        paths[name] = path
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"issue_count": 0},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.operator_package_release_controls()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
    assert result["ledger_mutated"] is True
    assert result["execution_admitted"] is False


def test_order_desk_consumers_reuse_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module

    schedule = tmp_path / "execution_schedule.csv"
    schedule.write_text(
        "phase,side,submission_mode,live_submission\n"
        "sell,sell,manual_ths_prefill_only,disabled\n",
        encoding="utf-8",
    )
    blotter = tmp_path / "blotter.csv"
    blotter.write_text(
        "preflight_status,route_status,lifecycle,side\n"
        "ready,route_available,planned,sell\n",
        encoding="utf-8",
    )
    attribution = tmp_path / "paper_execution_attribution.json"
    attribution.write_text(json.dumps({"status": "paper_only", "parent_count": 1}), encoding="utf-8")
    paths = {
        "execution_schedule": schedule,
        "blotter": blotter,
        "paper_execution_attribution": attribution,
    }
    monkeypatch.setattr(api_module, "operator_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"issue_count": 0},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    results = (
        api_module.operator_package_basket_schedule(),
        api_module.operator_package_blotter(),
        api_module.operator_package_execution_attribution(),
    )

    for result in results:
        assert result["same_ledger_bundle_reconciliation"] is bundle
        assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
        assert result["ledger_mutated"] is True
        assert result["execution_admitted"] is False


def test_observation_bundle_readiness_reuses_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module
    import trading.ths.observation_preview as observation_preview

    monkeypatch.setattr(api_module.settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(observation_preview, "preview_bundle", lambda *args, **kwargs: {
        "status": "templates_blank",
    })
    monkeypatch.setattr(api_module, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api_module, "_latest_complete_structured_observation_package", lambda: {})
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "ledger_reconciled",
        "summary": {"issue_count": 0},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.ths_observation_bundle_readiness()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "ledger_reconciled"
    assert result["same_ledger_imported"] is False
    assert result["source_manifest_status"] == "not_ready"
    assert result["source_manifest"] is None
    assert result["execution_admitted"] is False


def test_session_handoff_exposes_detailed_same_ledger_bundle(tmp_path, monkeypatch):
    import trading.api as api_module

    recovery = tmp_path / "recovery.csv"
    recovery.write_text("row\n", encoding="utf-8")
    carry = tmp_path / "carry.json"
    carry.write_text(json.dumps({"status": "paper_cross_session_state_only"}), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.csv"
    reconciliation.write_text("status\n", encoding="utf-8")
    target = tmp_path / "target.csv"
    target.write_text("status\n", encoding="utf-8")
    paths = {
        "recovery_plan": recovery,
        "paper_next_session_carry": carry,
        "paper_reconciliation": reconciliation,
        "target_order_reconciliation": target,
    }
    monkeypatch.setattr(api_module, "operator_package", lambda: {"strategy_id": "momentum"})
    monkeypatch.setattr(api_module, "_research_evidence_consumer_summary", lambda _: {"status": "research_only"})
    monkeypatch.setattr(api_module, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api_module, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })
    monkeypatch.setattr(api_module, "paper_preview_readiness", lambda: {
        "status": "blocked",
        "release_ready": False,
    })
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "reconciliation_required",
        "ledger_mutated": False,
        "summary": {"issue_count": 1},
    }
    monkeypatch.setattr(api_module, "_same_ledger_bundle_reconciliation_view", lambda: bundle)

    result = api_module.operator_package_session_handoff()

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_reconciliation_status"] == "reconciliation_required"
    assert result["same_ledger_imported"] is False
    assert result["execution_admitted"] is False


def test_promotion_review_persists_explicit_paper_only_human_receipt(tmp_path, monkeypatch):
    import trading.api as api_module

    template = tmp_path / "strategy_promotion_review_v1.json"
    template.write_text(json.dumps({
        "schema": "strategy_promotion_review_record_v1",
        "status": "manual_promotion_review_template",
        "candidate_count": 1,
        "eligible_candidate_count": 0,
        "required_evidence": ["fresh_holdout_required"],
        "rows": [{"strategy_id": "candidate_one", "promotion_status": "blocked_pending_explicit_review"}],
    }), encoding="utf-8")
    package = {"package_freshness": "current_or_future_execution_day", "execution_day": "2026-10-13"}
    session_root = tmp_path / "session"
    monkeypatch.setattr(api_module, "operator_package", lambda: package)
    monkeypatch.setattr(
        api_module, "_package_artifact",
        lambda _package, name: template if name == "strategy_promotion_review" else None,
    )
    monkeypatch.setattr(api_module, "_operator_package_session_root", lambda _package: session_root)

    body = api_module.PromotionReviewInput(
        strategy_id="candidate_one",
        decision="accept_for_paper",
        reviewer="operator",
        reviewed_at="2026-10-12T09:00:00+08:00",
        notes="保留纸面观察，不进入提交",
    )
    pending = api_module.submit_operator_package_promotion_review(body)
    assert pending["status"] == "confirmation_required"
    assert not (session_root / "human_promotion_review_receipt_v1.json").exists()

    confirmed = api_module.submit_operator_package_promotion_review(
        api_module.PromotionReviewInput(
            strategy_id=body.strategy_id,
            decision=body.decision,
            reviewer=body.reviewer,
            reviewed_at=body.reviewed_at,
            notes=body.notes,
            confirm_review=True,
        )
    )
    assert confirmed["status"] == "human_review_recorded"
    assert confirmed["automatic_promotion"] is False
    assert confirmed["execution_admitted"] is False
    assert confirmed["submission_enabled"] is False

    view = api_module.operator_package_promotion_review()
    assert view["status"] == "human_review_recorded"
    assert view["receipt"]["decision"] == "accept_for_paper"


def test_promotion_review_rejects_unknown_strategy(tmp_path, monkeypatch):
    import trading.api as api_module

    template = tmp_path / "strategy_promotion_review_v1.json"
    template.write_text(json.dumps({
        "schema": "strategy_promotion_review_record_v1",
        "rows": [{"strategy_id": "known_candidate"}],
    }), encoding="utf-8")
    monkeypatch.setattr(api_module, "operator_package", lambda: {
        "package_freshness": "current_or_future_execution_day",
        "execution_day": "2026-10-13",
    })
    monkeypatch.setattr(api_module, "_package_artifact", lambda _package, name: template)

    body = api_module.PromotionReviewInput(
        strategy_id="unknown_candidate",
        decision="defer",
        reviewer="operator",
        reviewed_at="2026-10-12T09:00:00+08:00",
        notes="unknown",
        confirm_review=True,
    )
    try:
        api_module.submit_operator_package_promotion_review(body)
    except Exception as error:
        assert getattr(error, "status_code", None) == 400
    else:
        raise AssertionError("unknown strategy should not receive a review receipt")


def test_latest_research_exception_view_reads_saved_register_without_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "research_decision_matrices" / "current" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "research_decision_matrix_v1",
        "id": "matrix-1",
        "research_data_exceptions": {
            "schema": "research_data_exception_register_v1",
            "status": "no_open_blocker",
            "completed_session_frontier": 424,
            "open_items": [],
            "resume_allowed": False,
            "resume_policy": "resume_after_reviewed_source_evidence_only",
            "operator_next_action": "retain latest projection",
        },
    }), encoding="utf-8")

    result = _latest_research_exception_view()

    assert result["schema"] == "research_data_exception_view_v1"
    assert result["status"] == "no_open_blocker"
    assert result["matrix_run_id"] == "matrix-1"
    assert result["completed_session_frontier"] == 424
    assert result["resume_allowed"] is False
    assert result["execution_admitted"] is False


def test_latest_research_exception_view_is_safe_when_matrix_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)

    result = _latest_research_exception_view()

    assert result["status"] == "missing"
    assert result["research_only"] is True
    assert result["execution_admitted"] is False
    assert result["resume_allowed"] is False
