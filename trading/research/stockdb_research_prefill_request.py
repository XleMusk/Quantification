"""Prepare a quote/account request and blank THS reference template."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT


def _json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def run(target_run: Path, *, output: Path | None = None) -> tuple[Path, dict]:
    target = _json(Path(target_run).resolve())
    if target.get("status") != "completed_stockdb_research_target_preview" or target.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted research target preview required")
    target_path = Path(target["target_path"])
    frame = pd.read_csv(target_path, dtype={"symbol": str})
    required = {"rank", "symbol", "score", "target_weight"}
    if not required <= set(frame.columns) or frame.symbol.duplicated().any():
        raise ValueError("Research target weight identity changed")
    quote = pd.DataFrame({
        "symbol": frame.symbol,
        "requested_target_weight": frame.target_weight,
        "price_rmb": "",
        "qty": "",
        "status": "awaiting_same_session_raw_rmb_quote_and_account_refresh",
        "execution_admitted": "false",
    })
    prefill = pd.DataFrame({
        "sequence": range(1, len(frame) + 1),
        "symbol": frame.symbol,
        "side": "buy",
        "symbol_field": "",
        "price_field": "",
        "qty_field": "",
        "prefill_status": "not_prefillable_without_quote_and_account",
        "execution_admitted": "false",
    })
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_research_prefill_requests" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_research_prefill_requests" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    quote_path, prefill_path = artifacts / "quote_request.csv", artifacts / "ths_prefill_reference_template.csv"
    quote.to_csv(quote_path, index=False)
    prefill.to_csv(prefill_path, index=False)
    result = {
        "status": "completed_stockdb_research_prefill_request",
        "target_run": str(Path(target_run).resolve()),
        "date": target.get("date"),
        "request_count": len(frame),
        "quote_request": str(quote_path),
        "ths_prefill_reference_template": str(prefill_path),
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "submitted": False,
        "execution_admitted": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "artifacts": str(artifacts),
        "limitations": [
            "This is a blank reference template; no current RMB quote or account state was supplied.",
            "No symbol/price/quantity fields are populated for client prefill.",
            "No GUI interaction, readback, submission, cancellation, Ledger or broker state was attempted.",
        ],
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# Research target quote and THS reference request", "",
        f"Date: `{result['date']}`; request rows: `{len(frame)}`.",
        f"Quote request: `{quote_path}`.",
        f"Blank THS reference template: `{prefill_path}`.",
        "No account/quote observation or client interaction occurred.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-run", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.target_run, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
