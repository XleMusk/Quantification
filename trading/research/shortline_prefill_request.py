"""Create a blank quote/account request from a shortline research target preview."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT


def _json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def run(target_preview: Path, *, output: Path | None = None) -> tuple[Path, dict]:
    target = _json(Path(target_preview).resolve())
    if target.get("status") != "completed_research_target_weight_preview" or target.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted shortline target preview required")
    weights = target.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("Shortline target preview needs a non-empty weights object")
    symbols = sorted(str(symbol) for symbol in weights)
    if len(symbols) != len(set(symbols)) or any(not symbol for symbol in symbols):
        raise ValueError("Shortline target symbols must be unique and non-empty")
    quote = pd.DataFrame({
        "symbol": symbols,
        "requested_target_weight": [weights[symbol] for symbol in symbols],
        "price_rmb": "",
        "qty": "",
        "status": "awaiting_same_session_raw_rmb_quote_and_account_refresh",
        "execution_admitted": "false",
    })
    prefill = pd.DataFrame({
        "sequence": range(1, len(symbols) + 1),
        "symbol": symbols,
        "side": "buy",
        "symbol_field": "",
        "price_field": "",
        "qty_field": "",
        "prefill_status": "not_prefillable_without_quote_and_account",
        "execution_admitted": "false",
    })
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "shortline_prefill_requests" / identifier
    artifacts = DATA_ROOT / "research" / "shortline_prefill_requests" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    quote_path = artifacts / "quote_request.csv"
    prefill_path = artifacts / "ths_prefill_reference_template.csv"
    quote.to_csv(quote_path, index=False)
    prefill.to_csv(prefill_path, index=False)
    result = {
        "status": "completed_shortline_prefill_request",
        "target_preview": str(Path(target_preview).resolve()),
        "date": target.get("decision_day"),
        "request_count": len(symbols),
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
        "# Shortline research target quote and THS reference request", "",
        f"Date: `{result['date']}`; request rows: `{len(symbols)}`.",
        f"Quote request: `{quote_path}`.",
        f"Blank THS reference template: `{prefill_path}`.",
        "No account/quote observation or client interaction occurred.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-preview", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.target_preview, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
