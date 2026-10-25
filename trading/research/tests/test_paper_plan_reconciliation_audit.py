from trading.research.paper_plan_reconciliation_audit import BLOCKED, VERIFIED, audit_payloads


def _payloads():
    symbols = {"000001.SZSE": .4, "600000.SSE": .4}
    plan = {
        "execution_admitted": False, "prefill_allowed": False,
        "broker_connected": False, "ths_client_interacted": False,
        "seed_last_completed_day": "2026-10-23",
        "historical_target": {"weights": symbols},
    }
    recon = {
        "status": "PAPER_CLOSE_REFERENCE_READY", "execution_admitted": False,
        "orders_created": False, "ledger_mutated": False, "day": "2026-10-23",
        "target_count": 2,
        "rows": [{"symbol": symbol, "target_weight": weight,
                  "ths_prefill_status": "reference_only_not_prefillable"}
                 for symbol, weight in symbols.items()],
    }
    continuity = {"status": "VERIFIED_READONLY_CURRENT_CUTOFF",
                  "execution_admitted": False, "pending_targets": False,
                  "client_interacted": False, "last_completed_day": "2026-10-23"}
    ths = {"status": "READONLY_REFERENCE_REQUEST_CREATED", "execution_admitted": False,
           "client_interacted": False, "prefill_allowed": False, "submitted": False,
           "cancelled": False, "readback_observed": False}
    csv_rows = [{"prefill_status": "reference_only_not_prefillable",
                 "same_session_price_rmb": "", "estimated_target_qty": "", "delta_qty": ""}
                for _ in symbols]
    return plan, recon, continuity, ths, csv_rows


def test_audit_binds_four_local_artifacts_and_keeps_live_channel_closed():
    result = audit_payloads(*_payloads())
    assert result["status"] == VERIFIED
    assert result["issues"] == []
    assert result["execution_admitted"] is False
    assert result["orders_created"] is False
    assert result["readback_observed"] is False


def test_audit_blocks_populated_ths_template_or_weight_drift():
    plan, recon, continuity, ths, rows = _payloads()
    rows[0]["estimated_target_qty"] = "100"
    recon["rows"][0]["target_weight"] = .3
    result = audit_payloads(plan, recon, continuity, ths, rows)
    assert result["status"] == BLOCKED
    assert "target.weight.000001.SZSE" in result["issues"]
    assert "ths_csv.1.estimated_target_qty_populated" in result["issues"]

