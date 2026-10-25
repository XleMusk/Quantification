import json

from trading.research.candidate_shadow_paper import build_shadow_package


def test_shadow_package_routes_terminal_targets_without_external_execution(tmp_path):
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "synthetic_candidate",
        "end_date": "2026-10-13",
        "execution_admitted": False,
        "strategy_validated": False,
        "selected_events": [
            {"date": "2026-10-13", "symbol": "000001.SZSE", "score": 0.9},
            {"date": "2026-10-13", "symbol": "600000.SSE", "score": 0.8},
        ],
        "state": {
            "day": "2026-10-13",
            "targets": {"000001.SZSE": 0.2, "600000.SSE": 0.2},
            "prices": {"000001.SZSE": 1000, "600000.SSE": 2000},
        },
    }), encoding="utf-8")
    receipt = build_shadow_package(
        replay, tmp_path / "out", initial_cash=10000,
        execution_day="2026-10-14", settlement_day="2026-10-15",
    )
    result = receipt["package_result"]
    assert receipt["status"] == "COMPLETED_RESEARCH_ONLY_SHADOW_PAPER_PACKAGE"
    assert result["target_count"] == 2
    assert result["paper_order_count"] == 2
    assert result["execution_admitted"] is False
    assert result["account_observed"] is False
    assert result["client_interacted"] is False
    assert result["submitted"] is False
    assert result["cancelled"] is False
    assert result["ledger_persisted"] is False
    assert receipt["validation"]["account_observed"] is False
    assert receipt["validation"]["client_interacted"] is False
    assert receipt["validation"]["submitted"] is False
    assert all(receipt["boundary_assertions"].values())


def test_shadow_package_rejects_admitted_replay(tmp_path):
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "strategy_id": "unsafe",
        "execution_admitted": True,
        "strategy_validated": False,
        "state": {"targets": {"000001.SZSE": 0.4}, "prices": {"000001.SZSE": 1000}},
    }), encoding="utf-8")
    try:
        build_shadow_package(
            replay, tmp_path / "out", initial_cash=10000,
            execution_day="2026-10-14", settlement_day="2026-10-15",
        )
    except ValueError as error:
        assert "unadmitted" in str(error)
    else:
        raise AssertionError("admitted replay must be rejected")


def test_shadow_package_accepts_explicit_legacy_terminal_account(tmp_path):
    account = tmp_path / "account.json"
    account.write_text(json.dumps({
        "day": "2026-10-13",
        "targets": {"000001.SZSE": 0.2, "600000.SSE": 0.2},
        "prices": {"000001.SZSE": 1000, "600000.SSE": 2000},
        "candidates": [
            {"symbol": "600000.SSE", "score": 0.8},
            {"symbol": "000001.SZSE", "score": 0.7},
        ],
    }), encoding="utf-8")
    receipt = build_shadow_package(
        account, tmp_path / "legacy-out", initial_cash=10000,
        execution_day="2026-10-14", settlement_day="2026-10-15",
        legacy_account=True, strategy_id="trend_low_vol_half_momentum_20",
    )
    assert receipt["source_kind"] == "legacy_terminal_account"
    assert receipt["package_result"]["strategy_id"] == "trend_low_vol_half_momentum_20"
    assert receipt["package_result"]["target_count"] == 2
    assert receipt["package_result"]["paper_order_count"] == 2
    assert receipt["execution_admitted"] is False


def test_shadow_package_requires_strategy_id_for_legacy_account(tmp_path):
    account = tmp_path / "account.json"
    account.write_text(json.dumps({
        "day": "2026-10-13", "targets": {"000001.SZSE": 0.2},
        "prices": {"000001.SZSE": 1000},
    }), encoding="utf-8")
    try:
        build_shadow_package(
            account, tmp_path / "legacy-out", initial_cash=10000,
            execution_day="2026-10-14", settlement_day="2026-10-15",
            legacy_account=True,
        )
    except ValueError as error:
        assert "strategy_id" in str(error)
    else:
        raise AssertionError("legacy account without strategy_id must be rejected")
