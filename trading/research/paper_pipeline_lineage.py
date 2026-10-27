"""Rebuild a research-only Data-to-Cash/Position lineage from saved receipts."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "quantification_paper_pipeline_lineage_v1"
VERIFIED = "VERIFIED_LOCAL_RESEARCH_ONLY_PAPER_LINEAGE"
DEFAULT_POST_CLOSE = Path("outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v2/next_close/contract_receipt.json")
DEFAULT_FIXED = Path("outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v2/next_close/fixed/result.json")
DEFAULT_JOINT = Path("outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v2/next_close/joint_paper/joint_paper_receipt.json")
DEFAULT_THS = Path("outputs/pipeline_receipts/20261027_ths_target_readback_current_synthetic_v1.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v1.json")

def _read(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path}")
    return value

def _ref(path: str | Path) -> dict[str, str]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"lineage source is missing: {path}")
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}

def _closed(name: str, value: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if value.get(key) is not False:
            raise ValueError(f"{name}.{key} must be false")

def build_lineage(post_close=DEFAULT_POST_CLOSE, fixed=DEFAULT_FIXED, joint=DEFAULT_JOINT,
                  ths_readback=DEFAULT_THS, output=DEFAULT_OUTPUT) -> dict[str, Any]:
    paths = [Path(p).expanduser().resolve() for p in (post_close, fixed, joint, ths_readback)]
    post, fixed_payload, joint, ths = [_read(p) for p in paths]
    _closed("post_close", post, ("execution_admitted", "broker_interacted", "ths_interacted", "persistent_ledger_mutated"))
    _closed("fixed", fixed_payload, ("execution_admitted", "client_interacted", "submitted", "ledger_persisted"))
    _closed("joint", joint, ("execution_admitted", "broker_interacted", "ths_interacted", "persistent_ledger_mutated"))
    _closed("ths_readback", ths, ("execution_admitted", "client_interacted", "submitted", "ledger_mutated"))
    if post.get("status") != "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1":
        raise ValueError("post-close receipt is not a completed research-only D0/D1 task")
    if fixed_payload.get("schema") != "stockdb_paper_target_order_package_v1":
        raise ValueError("fixed package schema is unsupported")
    paper_package = (joint.get("paper_package") or {}).get("result")
    if not isinstance(paper_package, Mapping) or paper_package.get("schema") != "stockdb_paper_target_order_package_v1":
        raise ValueError("joint paper package is missing")
    projected_path = paper_package.get("artifacts", {}).get("projected_account")
    if not isinstance(projected_path, str):
        raise ValueError("joint projected account is missing")
    projected = _read(projected_path)
    snapshot_path = post.get("snapshot", {}).get("path")
    candidate_path = post.get("candidate_source_manifest", {}).get("path")
    if not isinstance(snapshot_path, str) or not isinstance(candidate_path, str):
        raise ValueError("post-close source references are missing")
    snapshot = Path(snapshot_path)
    if snapshot.is_dir():
        snapshot /= "snapshot.json"
    fa, ja = fixed_payload.get("artifacts", {}), paper_package.get("artifacts", {})
    result = {"schema": SCHEMA, "status": VERIFIED, "close_date": post.get("close_date"),
        "execution_day": post.get("execution_day") or post.get("fingerprint_inputs", {}).get("execution_day"),
        "settlement_day": post.get("settlement_day") or post.get("fingerprint_inputs", {}).get("settlement_day"),
        "input_fingerprint": post.get("input_fingerprint"), "execution_admitted": False,
        "broker_interacted": False, "ths_interacted": False, "persistent_ledger_mutated": False,
        "stages": {
          "data": {"status": "STOCKDB_SNAPSHOT_BOUND", "coverage": post.get("snapshot_coverage"), "source": _ref(snapshot)},
          "signal": {"status": "STRATEGY_SIGNAL_INPUT_BOUND", "strategies": ["low_volatility_252_20", "stockdb_low_vol_half_momentum_20"], "source": _ref(candidate_path)},
          "target_portfolio": {"status": "TARGET_PREVIEW_BOUND", "fixed_target_count": fixed_payload.get("target_count"), "joint_target_count": paper_package.get("target_count"), "sources": [_ref(fa["target_holdings"]), _ref(ja["target_holdings"])]},
          "orders": {"status": "PAPER_ORDER_PLAN_BOUND", "fixed_order_count": fixed_payload.get("paper_order_count"), "joint_order_count": paper_package.get("paper_order_count"), "sources": [_ref(fa["order_plan"]), _ref(ja["order_plan"])]},
          "broker_adapter": {"status": "REFERENCE_PAYLOAD_ONLY", "mode": "offline_ths_reference_payload", "client_interacted": False, "submitted": False, "source": _ref(paths[3])},
          "fill": {"status": "MODELED_PAPER_FILL_PROJECTION", "count": None, "basis": "shared local Ledger projection; no broker fill observed", "sources": [_ref(fa["projected_account"]), _ref(ja["projected_account"])]},
          "cash_position": {"status": "PROJECTED_CASH_POSITION_BOUND", "initial_cash_rmb": projected.get("initial_cash_rmb"), "cash_after_modeled_fills_rmb": projected.get("cash_after_modeled_fills_rmb"), "equity_at_observed_marks_rmb": projected.get("equity_at_observed_marks_rmb"), "position_count": len(projected.get("positions", {})), "source": _ref(projected_path)},
        },
        "sources": {"post_close": _ref(paths[0]), "fixed": _ref(paths[1]), "joint": _ref(paths[2]), "ths_readback": _ref(paths[3])},
        "limitations": ["This lineage joins saved research-only artifacts and does not create a persistent account.", "BrokerAdapter is an offline reference payload, not a THS client observation or submission.", "Fill count is intentionally not inferred from order count; projected cash and positions are modeled outputs."]}
    output = Path(output).expanduser().resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output)
    return result

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-close", type=Path, default=DEFAULT_POST_CLOSE)
    parser.add_argument("--fixed", type=Path, default=DEFAULT_FIXED)
    parser.add_argument("--joint", type=Path, default=DEFAULT_JOINT)
    parser.add_argument("--ths-readback", type=Path, default=DEFAULT_THS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build_lineage(args.post_close, args.fixed, args.joint, args.ths_readback, args.output)
    print(json.dumps({"status": result["status"], "execution_admitted": False, "output": result["output"]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
