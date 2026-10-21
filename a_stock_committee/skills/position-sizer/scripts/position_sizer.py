from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from a_stock_committee.position_sizing import (  # noqa: E402
    calculate_a_share_position,
    result_to_markdown,
    save_position_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股仓位计算")
    parser.add_argument("--account-equity", type=float, required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, default=None)
    parser.add_argument("--stop-pct", type=float, default=None)
    parser.add_argument("--risk-pct", type=float, default=0.5)
    parser.add_argument("--max-position-pct", type=float, default=5)
    parser.add_argument("--output-dir", default="committee_reports")
    args = parser.parse_args()

    result = calculate_a_share_position(
        account_equity=args.account_equity,
        entry_price=args.entry,
        stop_price=args.stop,
        stop_pct=args.stop_pct,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
    )
    json_path, md_path = save_position_report(result, args.output_dir)
    print(result_to_markdown(result))
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

