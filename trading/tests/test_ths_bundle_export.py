import json
from pathlib import Path

import pytest

from trading.ths.bundle_export import attach_cashflow_export, export_observations
from trading.ths.observation_preview import build_statement_observations, preview_bundle
from trading.ths.structured_observation import (
    build_structured_observation_package,
    build_structured_observation_package_from_bundle,
    build_structured_observation_package_from_native_index,
    build_source_manifest,
    verify_source_manifest,
)
from trading.ths.readonly_evidence import build_readonly_evidence_index


def _account():
    return {
        "kind": "account", "source": "ths_user_export", "account_alias": "sim",
        "observed_at": "2026-10-10T10:00:00+08:00", "trading_date": "2026-10-10",
        "cash_basis": "cash_balance_includes_frozen", "cash_total": 100000,
        "cash_frozen": 1000, "cash_available": 99000,
        "rows": [{"symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price_cents": 1234}],
    }


def test_export_materializes_structured_views_without_inventing_cashflows(tmp_path):
    receipt = export_observations([_account()], tmp_path)
    assert receipt["kinds"] == ["account"]
    assert receipt["status"] == "materialized_partial"
    assert receipt["missing_kinds"] == ["orders", "trades", "cashflows"]
    payload = json.loads((tmp_path / "account_observation.json").read_text())
    assert payload["positions"][0]["symbol"] == "600000.SSE"
    assert payload["cash_available"] == 99000
    assert not (tmp_path / "cashflows.csv").exists()


def test_export_refuses_to_overwrite_non_template_files(tmp_path):
    (tmp_path / "account_observation.json").write_text(json.dumps({"cash_available": 1}))
    try:
        export_observations([_account()], tmp_path)
    except ValueError as error:
        assert "Refusing to overwrite" in str(error)
    else:
        raise AssertionError("non-template export was overwritten")


def test_attach_cashflow_export_is_safe_and_preserves_explicit_input(tmp_path):
    source = tmp_path / "user_cashflows.csv"
    source.write_text("券商导出内容\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    target = attach_cashflow_export(source, bundle)
    assert target.read_text(encoding="utf-8") == "券商导出内容\n"

    target.write_text("symbol,market,side,order_id,gross_amount,fees,cash_change\nrow\n", encoding="utf-8")
    try:
        attach_cashflow_export(source, bundle)
    except ValueError as error:
        assert "Refusing to overwrite" in str(error)
    else:
        raise AssertionError("non-template cashflow export was overwritten")


def test_export_preflights_all_destinations_before_writing(tmp_path):
    (tmp_path / "orders.csv").write_text("symbol,market,side,order_id,qty,price,filled_qty,status\nlocked\n")
    account_path = tmp_path / "account_observation.json"
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10", "rows": []}]
    try:
        export_observations(values, tmp_path)
    except ValueError as error:
        assert "Refusing to overwrite" in str(error)
    else:
        raise AssertionError("locked member was overwritten")
    assert not account_path.exists()


def test_exported_four_views_are_ready_for_reconciliation(tmp_path):
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "qty": 100,
                   "price_cents": 1234, "filled_qty": 100, "broker_status": "FILLED"}]},
        {"kind": "trades", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:02+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "execution_id": "E1",
                   "qty": 100, "price_cents": 1234}]},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:03+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "gross_cents": 123400,
                   "fees_cents": 5, "cash_change_cents": -123405}]},
    ]
    export_observations(values, tmp_path)
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    preview = preview_bundle(tmp_path / "account_observation.json", tmp_path / "orders.csv",
                             tmp_path / "trades.csv", tmp_path / "cashflows.csv", profile=profile)
    assert preview["status"] == "bundle_ready_for_reconciliation"
    assert preview["linkage"]["filled_quantity_mismatches"] == []


def test_preview_blocks_incomplete_explicit_profile_before_parsing(tmp_path):
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10", "rows": []},
        {"kind": "trades", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:02+08:00", "trading_date": "2026-10-10", "rows": []},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:03+08:00", "trading_date": "2026-10-10", "rows": []}]
    export_observations(values, tmp_path)
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    del profile["tables"]["cashflows"]

    preview = preview_bundle(
        tmp_path / "account_observation.json", tmp_path / "orders.csv",
        tmp_path / "trades.csv", tmp_path / "cashflows.csv", profile=profile,
    )

    assert preview["status"] == "validation_error"
    assert preview["profile_validation"]["status"] == "invalid"
    assert preview["profile_validation"]["missing_kinds"] == ["cashflows"]
    assert preview["ledger_mutated"] is False


def test_statement_import_requires_declared_semantics(tmp_path):
    from trading.ths.observation_preview import build_statement_observations

    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    profile["tables"]["movements"]["basis"] = "cumulative_order_cashflow"
    path = tmp_path / "movements.csv"
    path.write_text("movement_id,kind,cash_change\nM1,interest,1.00\n", encoding="utf-8")
    account = {"account_alias": "sim", "observed_at": "2026-10-10T10:00:00+08:00", "trading_date": "2026-10-10"}

    with pytest.raises(ValueError, match="Invalid statement export profile"):
        build_statement_observations(account, {"movements": path}, profile=profile)


def test_explicit_statement_exports_join_same_account_observation(tmp_path):
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    account = {
        "kind": "account", "source": "ths_user_export", "account_alias": "sim",
        "observed_at": "2026-10-10T10:00:00+08:00", "trading_date": "2026-10-10",
    }
    statement_rows = {
        "movements": [
            {"movement_id": "M1", "kind": "cash_dividend", "cash_change": "12.34"},
        ],
        "restriction_releases": [
            {"release_id": "R1", "symbol": "600000", "market": "SSE",
             "released_qty": "100", "release_day": "2026-10-10",
             "reason": "opening_unknown_restriction_release"},
        ],
        "share_credits": [
            {"credit_id": "C1", "symbol": "600000", "market": "SSE",
             "credited_qty": "20", "credit_day": "2026-10-10",
             "available_day": "2026-10-13", "kind": "bonus_shares"},
        ],
    }
    for kind, rows in statement_rows.items():
        path = tmp_path / f"{kind}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = __import__("csv").DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    values = build_statement_observations(
        account,
        {kind: tmp_path / f"{kind}.csv" for kind in statement_rows},
        profile=profile,
    )

    assert [value["kind"] for value in values] == [
        "movements", "restriction_releases", "share_credits"
    ]
    assert values[0]["rows"][0]["cash_change_cents"] == 1234
    assert values[1]["rows"][0]["released_qty"] == 100
    assert values[2]["rows"][0]["available_day"] == "2026-10-13"
    assert all(value["account_alias"] == "sim" for value in values)


def test_structured_source_manifest_detects_post_capture_export_change(tmp_path):
    paths = {}
    for kind in ("account", "orders", "trades", "cashflows"):
        path = tmp_path / f"{kind}.export"
        path.write_text(f"{kind}-snapshot-v1\n", encoding="utf-8")
        paths[kind] = str(path)

    manifest = build_source_manifest(paths)
    assert manifest["schema"] == "ths_observation_source_manifest_v1"
    assert manifest["status"] == "complete"
    assert verify_source_manifest(manifest)["status"] == "verified"

    (tmp_path / "trades.export").write_text("trades-snapshot-v2\n", encoding="utf-8")
    checked = verify_source_manifest(manifest)
    assert checked["status"] == "source_changed"
    assert checked["issues"] == [
        {
            "kind": "trades",
            "code": "source_file_changed",
            "expected_sha256": manifest["files"]["trades"]["sha256"],
            "actual_sha256": checked["issues"][0]["actual_sha256"],
            "expected_size_bytes": manifest["files"]["trades"]["size_bytes"],
            "actual_size_bytes": checked["issues"][0]["actual_size_bytes"],
        }
    ]


def test_preview_rejects_order_status_filled_quantity_mismatch(tmp_path):
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "qty": 100,
                   "price_cents": 1234, "filled_qty": 50, "broker_status": "FILLED"}]},
        {"kind": "trades", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:02+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "execution_id": "E1",
                   "qty": 50, "price_cents": 1234}]},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:03+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1",
                   "gross_cents": 123400, "fees_cents": 5, "cash_change_cents": -123405}]},
    ]
    export_observations(values, tmp_path)
    preview = preview_bundle(
        tmp_path / "account_observation.json", tmp_path / "orders.csv",
        tmp_path / "trades.csv", tmp_path / "cashflows.csv",
    )
    assert preview["status"] == "validation_error", preview
    assert preview["linkage"]["status_fill_mismatches"] == [
        {"order_id": "O1", "status": "filled", "order_qty": 100, "filled_qty": 50}
    ]
    assert "order statuses do not match filled quantities" in preview["errors"]
    assert preview["execution_admitted"] is False
    assert preview["ledger_mutated"] is False


def test_preview_accepts_explicit_gb18030_ths_csv_profile(tmp_path):
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "qty": 100,
                   "price_cents": 1234, "filled_qty": 100, "broker_status": "FILLED"}]},
        {"kind": "trades", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:02+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "execution_id": "E1",
                   "qty": 100, "price_cents": 1234}]},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:03+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "gross_cents": 123400,
                   "fees_cents": 5, "cash_change_cents": -123405}]},
    ]
    export_observations(values, tmp_path)
    for name in ("orders.csv", "trades.csv", "cashflows.csv"):
        path = tmp_path / name
        path.write_bytes(path.read_text(encoding="utf-8-sig").encode("gb18030"))

    preview = preview_bundle(tmp_path / "account_observation.json", tmp_path / "orders.csv",
                             tmp_path / "trades.csv", tmp_path / "cashflows.csv",
                             profile={"encoding": "gb18030"})
    assert preview["status"] == "bundle_ready_for_reconciliation", preview
    assert preview["linkage"]["filled_quantity_mismatches"] == []


def test_preview_accepts_explicit_tab_delimited_ths_profile(tmp_path):
    values = [_account(),
        {"kind": "orders", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:01+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "qty": 100,
                   "price_cents": 1234, "filled_qty": 100, "broker_status": "FILLED"}]},
        {"kind": "trades", "source": "ths_controls", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:02+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "execution_id": "E1",
                   "qty": 100, "price_cents": 1234}]},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": "2026-10-10T10:00:03+08:00", "trading_date": "2026-10-10",
         "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1", "gross_cents": 123400,
                   "fees_cents": 5, "cash_change_cents": -123405}]},
    ]
    export_observations(values, tmp_path)
    for name in ("orders.csv", "trades.csv", "cashflows.csv"):
        path = tmp_path / name
        path.write_text(path.read_text(encoding="utf-8-sig").replace(",", "\t"), encoding="utf-8-sig")

    preview = preview_bundle(
        tmp_path / "account_observation.json", tmp_path / "orders.csv",
        tmp_path / "trades.csv", tmp_path / "cashflows.csv",
        profile={"delimiter": "tab"},
    )
    assert preview["status"] == "bundle_ready_for_reconciliation", preview
    assert preview["linkage"]["filled_quantity_mismatches"] == []


def test_preview_returns_structured_error_for_invalid_account_json(tmp_path):
    (tmp_path / "account_observation.json").write_text("not-json", encoding="utf-8")
    for name in ("orders.csv", "trades.csv", "cashflows.csv"):
        (tmp_path / name).write_text("header\n", encoding="utf-8")

    preview = preview_bundle(tmp_path / "account_observation.json", tmp_path / "orders.csv",
                             tmp_path / "trades.csv", tmp_path / "cashflows.csv")
    assert preview["status"] == "validation_error"
    assert preview["ledger_mutated"] is False
    assert "Account JSON cannot be read" in preview["errors"][0]


def test_preview_accepts_observed_cash_only_account_with_header_only_activity_exports(tmp_path):
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    account = {
        "schema": "ths_account_observation_v1",
        "source": "ths_user_export",
        "account_alias": "simulated-account",
        "observed_at": "2026-10-09T15:05:00+08:00",
        "trading_date": "2026-10-09",
        "cash_available": 10000000,
        "cash_total": 10000000,
        "cash_frozen": 0,
        "positions": [],
    }
    (tmp_path / "account_observation.json").write_text(json.dumps(account), encoding="utf-8")
    (tmp_path / "orders.csv").write_text(
        "symbol,market,side,order_id,qty,price,filled_qty,status\n", encoding="utf-8"
    )
    (tmp_path / "trades.csv").write_text(
        "symbol,market,side,order_id,execution_id,qty,price\n", encoding="utf-8"
    )
    (tmp_path / "cashflows.csv").write_text(
        "symbol,market,side,order_id,gross_amount,fees,cash_change\n", encoding="utf-8"
    )

    preview = preview_bundle(
        tmp_path / "account_observation.json",
        tmp_path / "orders.csv",
        tmp_path / "trades.csv",
        tmp_path / "cashflows.csv",
        profile=profile,
    )

    assert preview["status"] == "bundle_ready_for_reconciliation", preview
    assert preview["observations"]["orders"]["row_count"] == 0
    assert preview["observations"]["trades"]["row_count"] == 0
    assert preview["observations"]["cashflows"]["row_count"] == 0
    assert preview["linkage"]["order_count"] == 0
    assert preview["execution_admitted"] is False
    assert preview["ledger_mutated"] is False


def test_structured_package_marks_header_only_activity_as_explicit_empty(tmp_path):
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    account = {
        "schema": "ths_account_observation_v1",
        "source": "ths_user_export",
        "account_alias": "simulated-account",
        "observed_at": "2026-10-09T15:05:00+08:00",
        "trading_date": "2026-10-09",
        "cash_available": 99000,
        "cash_total": 100000,
        "cash_frozen": 1000,
        "positions": [{"symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price": "12.34"}],
    }
    (tmp_path / "account_observation.json").write_text(json.dumps(account), encoding="utf-8")
    (tmp_path / "orders.csv").write_text(
        "symbol,market,side,order_id,qty,price,filled_qty,status\n", encoding="utf-8"
    )
    (tmp_path / "trades.csv").write_text(
        "symbol,market,side,order_id,execution_id,qty,price\n", encoding="utf-8"
    )
    (tmp_path / "cashflows.csv").write_text(
        "symbol,market,side,order_id,gross_amount,fees,cash_change\n", encoding="utf-8"
    )
    preview = preview_bundle(
        tmp_path / "account_observation.json",
        tmp_path / "orders.csv",
        tmp_path / "trades.csv",
        tmp_path / "cashflows.csv",
        profile=profile,
    )
    assert preview["status"] == "bundle_ready_for_reconciliation", preview
    observations = []
    from trading.ths.observation_preview import build_observation_bundle
    _, observations = build_observation_bundle(
        tmp_path / "account_observation.json",
        tmp_path / "orders.csv",
        tmp_path / "trades.csv",
        tmp_path / "cashflows.csv",
        profile=profile,
    )
    result = build_structured_observation_package_from_bundle(
        preview, observations, tmp_path / "structured",
        source_paths={kind: str(tmp_path / name) for kind, name in {
            "account": "account_observation.json",
            "orders": "orders.csv",
            "trades": "trades.csv",
            "cashflows": "cashflows.csv",
        }.items()},
    )
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert result["ready_for_ledger_reconciliation"] is True
    assert package["empty_activity_exports"] == {"orders": True, "trades": True, "cashflows": True}
    assert package["holdings_candidate_observed"] is True
    assert package["account_summary"]["positions_read"] is True
    assert package["tables"]["orders"]["empty_export_explicit"] is True
    assert package["order_reconciliation"]["zero_orders_inferred"] is False


def test_structured_package_blocks_missing_holdings_instead_of_inferencing_cash_only(tmp_path):
    account = {
        "kind": "account", "source": "ths_user_export", "account_alias": "sim",
        "observed_at": "2026-10-09T15:05:00+08:00", "trading_date": "2026-10-09",
        "cash_available": "99000.00", "cash_total": "100000.00", "cash_frozen": "1000.00",
        # Deliberately omit rows/positions: absence is not an explicit empty holdings table.
    }
    empty_values = [
        account,
        {"kind": "orders", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": account["observed_at"], "trading_date": account["trading_date"], "rows": []},
        {"kind": "trades", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": account["observed_at"], "trading_date": account["trading_date"], "rows": []},
        {"kind": "cashflows", "source": "ths_user_export", "account_alias": "sim",
         "observed_at": account["observed_at"], "trading_date": account["trading_date"], "rows": []},
    ]
    preview = {
        "status": "bundle_ready_for_reconciliation",
        "linkage": {"order_count": 0, "trade_count": 0, "cashflow_count": 0},
    }
    result = build_structured_observation_package_from_bundle(
        preview, empty_values, tmp_path / "structured",
    )
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert result["ready_for_ledger_reconciliation"] is False
    assert package["status"] == "partial_structured_observation_package"
    assert package["holdings_candidate_semantic_verified"] is False
    assert package["holdings_candidate"]["rows"] is None
    assert package["order_reconciliation"]["status"] == "blocked_missing_structured_holdings_rows"
    assert package["current_ledger_import_candidate"] is False
    assert "structured holdings rows are absent" in package["unknowns"][0]


def test_complete_native_index_propagates_reconciliation_counts(tmp_path):
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    for kind, count in (("orders", 2), ("trades", 3), ("cashflows", 2)):
        payload = {
            "kind": kind, "schema": f"ths_{kind}_receipt_v1", "structured_rows": True,
            "read_only": True, "submitted": False, "execution_admitted": False,
            "ledger_mutated": False, "rows": [{} for _ in range(count)],
        }
        (tmp_path / f"{kind}.json").write_text(json.dumps(payload), encoding="utf-8")
    index = {
        "schema": "ths_readonly_evidence_index_v1",
        "account_alias": "sim",
        "account_observed": True,
        "orders_observed": True,
        "trades_observed": True,
        "cashflows_observed": True,
        "structured_account": True,
        "summary": {"path": str(tmp_path / "summary.json"), "observed_at": "2026-10-10T10:00:00+08:00",
                    "metrics_raw": {}, "positions_read": True, "full_account_observation": True},
        "holdings": {"candidate": {}, "native_receipt": {}},
        "orders": {"native_receipt": {"path": str(tmp_path / "orders.json"), "row_count": 2,
                                        "structured_rows": True, "read_only": True}},
        "trades": {"native_receipt": {"path": str(tmp_path / "trades.json"), "row_count": 3,
                                        "structured_rows": True, "read_only": True}},
        "cashflows": {"native_receipt": {"path": str(tmp_path / "cashflows.json"), "row_count": 2,
                                           "structured_rows": True, "read_only": True}},
    }
    index_path = tmp_path / "readonly_evidence_index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = build_structured_observation_package(index_path, tmp_path / "structured")
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert result["ready_for_ledger_reconciliation"] is True
    assert package["order_count"] == 2
    assert package["trade_count"] == 3
    assert package["cashflow_count"] == 2
    assert package["source_paths_present"] is True
    assert package["order_reconciliation"]["next_action"] == "review this package, then run Ledger preview before import"


def test_native_index_flags_cannot_claim_readiness_without_receipt_files(tmp_path):
    index = {
        "schema": "ths_readonly_evidence_index_v1",
        "account_observed": True,
        "structured_account": True,
        "orders_observed": True,
        "trades_observed": True,
        "cashflows_observed": True,
        "summary": {"path": str(tmp_path / "missing-summary.json"), "full_account_observation": True},
        "orders": {"native_receipt": {"path": str(tmp_path / "missing-orders.json"), "structured_rows": True}},
        "trades": {"native_receipt": {"path": str(tmp_path / "missing-trades.json"), "structured_rows": True}},
        "cashflows": {"native_receipt": {"path": str(tmp_path / "missing-cashflows.json"), "structured_rows": True}},
    }
    index_path = tmp_path / "readonly_evidence_index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = build_structured_observation_package(index_path, tmp_path / "structured")
    assert result["ready_for_ledger_reconciliation"] is False
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert package["status"] == "partial_structured_observation_package"


def test_readonly_index_accepts_structured_cashflow_receipt_for_readiness(tmp_path):
    summary = {
        "schema": "ths_native_account_observation_v1",
        "kind": "account",
        "status": "read",
        "account_alias": "sim",
        "observed_at": "2026-10-10T10:00:00+08:00",
        "metrics_raw": {"cash_available": "100.00"},
        "positions_read": True,
        "full_account_observation": True,
        "structured_rows": True,
        "read_only": True,
        "submitted": False,
        "execution_admitted": False,
        "ledger_mutated": False,
        "rows": [],
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    candidate = {"kind": "holdings", "rows": [], "semantic_verified": True}
    (tmp_path / "holdings.json").write_text(json.dumps(candidate), encoding="utf-8")
    for kind in ("holdings", "orders", "trades", "cashflows"):
        receipt = {
            "kind": kind,
            "schema": f"ths_{kind}_receipt_v1",
            "status": "read",
            "observed_at": "2026-10-10T10:00:01+08:00",
            "structured_rows": True,
            "read_only": True,
            "client_interacted": False,
            "submitted": False,
            "rows": [],
        }
        (tmp_path / f"{kind}.json").write_text(json.dumps(receipt), encoding="utf-8")

    index = build_readonly_evidence_index(
        tmp_path / "summary.json",
        tmp_path / "holdings.json",
        tmp_path / "holdings.json",
        tmp_path / "orders.json",
        tmp_path / "trades.json",
        tmp_path / "cashflows.json",
    )
    assert index["status"] == "complete_readonly_evidence"
    assert index["cashflows_observed"] is True
    assert index["orders_observed"] is True and index["trades_observed"] is True
    assert index["next_action"] == "Review the complete read-only receipts, then run Ledger preview before import."
    index_path = tmp_path / "readonly_evidence_index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    package_result = build_structured_observation_package(index_path, tmp_path / "structured")
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert package_result["ready_for_ledger_reconciliation"] is True
    assert package["holdings_candidate_observed"] is True
    assert package["holdings_candidate"]["row_count"] == 0

    unsafe = json.loads((tmp_path / "orders.json").read_text(encoding="utf-8"))
    unsafe["submitted"] = True
    (tmp_path / "orders.json").write_text(json.dumps(unsafe), encoding="utf-8")
    blocked = build_readonly_evidence_index(
        tmp_path / "summary.json", tmp_path / "holdings.json", tmp_path / "holdings.json",
        tmp_path / "orders.json", tmp_path / "trades.json", tmp_path / "cashflows.json",
    )
    assert blocked["status"] == "partial_readonly_evidence"
    assert blocked["orders_observed"] is False


def test_readonly_index_accepts_structured_account_summary_receipt(tmp_path):
    """The account summary schema is valid only when it carries real rows."""
    summary = {
        "schema": "ths_native_account_summary_v1", "kind": "account",
        "status": "read", "account_alias": "sim", "observed_at": "2026-10-10T10:00:00+08:00",
        "metrics_raw": {"cash_available": "100.00"}, "positions_read": True,
        "full_account_observation": True, "structured_rows": True, "read_only": True,
        "submitted": False, "execution_admitted": False, "ledger_mutated": False,
        "rows": [{"symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price_cents": 1234}],
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    holdings_path = tmp_path / "holdings.json"
    holdings_path.write_text(json.dumps({"kind": "holdings", "rows": [], "semantic_verified": True}), encoding="utf-8")
    receipts = {}
    for kind in ("holdings", "orders", "trades", "cashflows"):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps({
            "kind": kind, "schema": f"ths_{kind}_receipt_v1", "status": "read",
            "structured_rows": True, "read_only": True, "submitted": False,
            "execution_admitted": False, "ledger_mutated": False, "rows": [],
        }), encoding="utf-8")
        receipts[kind] = path

    index = build_readonly_evidence_index(
        summary_path, holdings_path, receipts["holdings"], receipts["orders"],
        receipts["trades"], receipts["cashflows"],
    )
    assert index["status"] == "complete_readonly_evidence"
    assert index["account_observed"] is True
    assert index["summary"]["row_count"] == 1


def test_native_structured_receipts_normalize_into_same_ledger_package(tmp_path):
    from datetime import datetime, timedelta, timezone

    observed_at = datetime.now(timezone.utc).isoformat()
    trading_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    account = {
        "kind": "account", "schema": "ths_native_account_observation_v1",
        "source": "ths_controls", "account_alias": "sim-native",
        "observed_at": observed_at, "trading_date": trading_date,
        "structured_rows": True, "read_only": True, "submitted": False,
        "execution_admitted": False, "ledger_mutated": False,
        "cash_available_cents": 99895, "cash_total_cents": 99895, "cash_frozen_cents": 0,
        "rows": [{"symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price_cents": 1234}],
    }
    orders = {
        "kind": "orders", "schema": "ths_orders_receipt_v1", "source": "ths_controls",
        "account_alias": "sim-native", "observed_at": observed_at, "trading_date": trading_date,
        "structured_rows": True, "read_only": True, "submitted": False,
        "execution_admitted": False, "ledger_mutated": False,
        "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1",
                   "qty": 100, "price_cents": 1234, "filled_qty": 100, "status": "filled"}],
    }
    trades = {
        "kind": "trades", "schema": "ths_trades_receipt_v1", "source": "ths_controls",
        "account_alias": "sim-native", "observed_at": observed_at, "trading_date": trading_date,
        "structured_rows": True, "read_only": True, "submitted": False,
        "execution_admitted": False, "ledger_mutated": False,
        "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1",
                   "execution_id": "E1", "qty": 100, "price_cents": 1234}],
    }
    cashflows = {
        "kind": "cashflows", "schema": "ths_cashflows_receipt_v1", "source": "ths_user_export",
        "account_alias": "sim-native", "observed_at": observed_at, "trading_date": trading_date,
        "structured_rows": True, "read_only": True, "submitted": False,
        "execution_admitted": False, "ledger_mutated": False,
        "rows": [{"symbol": "600000.SSE", "side": "buy", "order_id": "O1",
                   "gross_cents": 123400, "fees_cents": 5, "cash_change_cents": -123405}],
    }
    paths = {}
    for kind, payload in (("account", account), ("orders", orders), ("trades", trades), ("cashflows", cashflows)):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[kind] = path
    index = {
        "schema": "ths_readonly_evidence_index_v1", "account_alias": "sim-native",
        "account_observed": True, "structured_account": True,
        "summary": {"path": str(paths["account"]), "full_account_observation": True},
        "orders": {"native_receipt": {"path": str(paths["orders"])}},
        "trades": {"native_receipt": {"path": str(paths["trades"])}},
        "cashflows": {"native_receipt": {"path": str(paths["cashflows"])}},
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = build_structured_observation_package_from_native_index(
        index_path, tmp_path / "structured")
    package = json.loads((tmp_path / "structured" / "structured_observation_package_v1.json").read_text())
    assert result["ready_for_ledger_reconciliation"] is True
    assert package["source_scope"] == "isolated_rehearsal"
    assert package["order_count"] == 1
    assert package["trade_count"] == 1
    assert package["cashflow_count"] == 1
    assert package["source_manifest_status"] == "complete"
    assert package["order_reconciliation"]["linkage"]["filled_quantity_mismatches"] == []
    assert package["ledger_mutated"] is False
