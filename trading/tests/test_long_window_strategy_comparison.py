from trading.research import long_window_strategy_comparison as module


def test_saved_long_window_comparison_is_complete_and_research_only():
    receipt = module.build_receipt()
    assert receipt["schema"] == module.SCHEMA
    assert receipt["execution_admitted"] is False
    assert receipt["ledger_mutated"] is False
    assert receipt["comparison_contract"]["shared_stockdb_snapshot"] is True
    assert [row["id"] for row in receipt["strategies"]] == [
        "fixed_low_volatility_252_20",
        "low_vol_half_momentum_20",
        "momentum_12_1_20",
        "mature_factor_balanced_ridge",
        "alpha158_lgbm_cap500",
    ]
    fields = {"sessions", "orders", "fills", "net_return", "max_drawdown", "sharpe", "fees", "blocked"}
    for row in receipt["strategies"]:
        assert fields <= set(row["metrics"])
        assert row["engine_ledger_replay_completed"] is True
        assert row["execution_admitted"] is False
        assert row["source_paths"]["daily"].endswith("daily.csv")
        assert row["metrics"]["sessions"] > 0


def test_markdown_contains_all_standardized_metric_columns():
    markdown = module.render_markdown(module.build_receipt())
    for column in ("Sessions", "Orders", "Fills", "Net return", "Max drawdown", "Sharpe", "Fees", "Blocked"):
        assert column in markdown


def test_h60_variant_adds_saved_ridge_row_without_changing_baseline_contract():
    baseline = module.build_receipt()
    variant = module.build_receipt(include_h60=True)
    assert len(baseline["strategies"]) == 5
    assert [row["id"] for row in variant["strategies"]][-1] == "mature_factor_balanced_ridge_h60"
    row = variant["strategies"][-1]
    assert row["candidate_spec_id"] == "factor_candidate_family_balanced_ridge"
    assert row["metrics"]["sessions"] == 648
    assert row["execution_admitted"] is False
    assert variant["comparison_contract"]["include_h60_variant"] is True
