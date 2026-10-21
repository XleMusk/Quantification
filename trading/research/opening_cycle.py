"""Build one paper-only next-session preview and opening pack.

This is a small consumer of an existing fixed Engine/Ledger replay.  It keeps
the source cutoff and intended execution day explicit, then passes the saved
paper preview to the existing opening-pack builder.  It never reads the
broker client directly, creates persistent orders, or operates THS.  An
optional account alias reads only the latest normalized journal observation;
alternatively, a caller-supplied account JSON and quote JSON/CSV are checked
for the same freshness and raw-RMB rules as the existing THS consumer.  The
result also includes a paper-only trader-desk snapshot.
"""

import argparse
import json
from pathlib import Path

from . import next_session_preview, opening_pack
from ..ths.handoff import prepare_opening_pack
from ..ths import journal


def build(source_run, signal, cutoff, execution_day, *, comparison=None,
          rank_override=None, account_observation=None, quotes=None,
          quotes_csv=None, quote_source=None, quote_observed_at=None,
          quote_trading_date=None, account=None, save_next=False,
          sizing_mode="replay_shares", desk_out=None,
          execution_scope="strict"):
    """Return the preview and opening-pack directories for one fixed cycle."""
    preview_dir, preview = next_session_preview.build(
        source_run,
        signal,
        cutoff,
        execution_day,
        rank_override=rank_override,
        execution_scope=execution_scope,
    )
    preview_result = Path(preview_dir) / "result.json"
    pack_dir, pack = opening_pack.build(
        [preview_result],
        execution_day=execution_day,
        comparison=comparison,
        strategy_id=signal,
    )
    hydration = None
    if account is not None and account_observation is not None:
        raise ValueError("account and account_observation are mutually exclusive")
    if quotes is not None and quotes_csv is not None:
        raise ValueError("quotes and quotes_csv are mutually exclusive")
    if quotes_csv is not None:
        if not all((quote_source, quote_observed_at, quote_trading_date)):
            raise ValueError("quote CSV requires source, observed_at and trading_date")
        from ..ths.quotes import build_quote_observation
        quote_payload = build_quote_observation(
            quotes_csv,
            source=quote_source,
            observed_at=quote_observed_at,
            trading_date=quote_trading_date,
        )
    elif quotes is not None:
        quote_payload = json.loads(Path(quotes).read_text(encoding="utf-8-sig"))
    else:
        quote_payload = None
    if save_next and ((account is None and account_observation is None) or quote_payload is None):
        raise ValueError("--save-next requires an account source and same-session quotes")
    if account is not None or account_observation is not None or quote_payload is not None:
        if (account is None and account_observation is None) or quote_payload is None:
            raise ValueError("an account source and quotes must be supplied together")
        pack_json = json.loads((Path(pack_dir) / "opening_pack.json").read_text(encoding="utf-8-sig"))
        if account is not None:
            journal.initialize()
            snapshot = journal.latest(account, "account")
        else:
            snapshot = json.loads(Path(account_observation).read_text(encoding="utf-8-sig"))
        hydration = prepare_opening_pack(
            pack_json,
            snapshot,
            quote_payload,
            strategy_id=signal,
            sizing_mode=sizing_mode,
            execution_scope=execution_scope,
        )
        if save_next:
            ready = [candidate for candidate in hydration["candidates"]
                     if candidate.get("status") == "current_preview_only"]
            if len(ready) != 1 or not ready[0]["plan"].get("next_plan"):
                raise ValueError("Selected strategy has no saveable next manual-review plan")
            journal.initialize()
            saved_plan = journal.save_plan(ready[0]["plan"]["next_plan"])
            ready[0]["plan"]["next_plan"] = saved_plan
            hydration["saved"] = True
            hydration["saved_strategy_id"] = ready[0]["strategy_id"]
            hydration["saved_plan_id"] = saved_plan["id"]
            hydration["not_attempted"] = [
                "Only one next manual-review plan was saved in the existing THS journal.",
                "No THS client operation, prefill, submission, cancellation, or transfer was attempted.",
            ]
        elif hydration is not None:
            hydration["saved"] = False
    from .trader_desk_snapshot import build as build_trader_desk_snapshot
    desk_dir, desk_snapshot = build_trader_desk_snapshot(
        Path(pack_dir) / "opening_pack.json",
        desk_out,
        account_observation=account_observation,
        account=account,
        quotes=quotes,
        quotes_csv=quotes_csv,
        quote_source=quote_source,
        quote_observed_at=quote_observed_at,
        quote_trading_date=quote_trading_date,
        strategy_id=signal,
        sizing=sizing_mode,
        execution_scope=execution_scope,
    )
    return preview_dir, pack_dir, {
        "preview": preview,
        "opening_pack": pack,
        "hydration": hydration,
        "trader_desk_snapshot": desk_snapshot,
        "trader_desk_output": str(desk_dir),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True,
                        help="Completed fixed replay run.json")
    parser.add_argument("--signal", required=True,
                        help="One frozen signal from the source run")
    parser.add_argument("--cutoff", required=True,
                        help="Pinned source-data cutoff, YYYY-MM-DD")
    parser.add_argument("--execution-day", required=True,
                        help="Intended paper execution day, YYYY-MM-DD")
    parser.add_argument("--comparison",
                        help="Optional same-window comparison.json")
    parser.add_argument("--rank-override",
                        help="Optional current-score rankings directory")
    account_source = parser.add_mutually_exclusive_group()
    account_source.add_argument("--account",
                                help="Optional account alias with a latest normalized journal observation")
    account_source.add_argument("--account-observation",
                                help="Optional same-session normalized account JSON")
    quote_source = parser.add_mutually_exclusive_group()
    quote_source.add_argument("--quotes",
                              help="Optional same-session raw-RMB quote observation JSON")
    quote_source.add_argument("--quotes-csv",
                              help="Optional CSV with symbol, market and price_rmb")
    parser.add_argument("--quote-source",
                        help="Quote source label required with --quotes-csv")
    parser.add_argument("--quote-observed-at",
                        help="Timezone-qualified observation time required with --quotes-csv")
    parser.add_argument("--quote-trading-date",
                        help="Trading date required with --quotes-csv")
    parser.add_argument("--save-next", action="store_true",
                        help="Save the hydrated selected plan to the existing manual-review journal")
    parser.add_argument("--desk-out",
                        help="Optional output directory for the paper trader-desk snapshot")
    parser.add_argument("--sizing", choices=("replay_shares", "observed_account"),
                        default="replay_shares",
                        help="Use frozen replay shares or size from the current account")
    parser.add_argument("--execution-scope", choices=("strict", "mainboard_only"),
                        default="strict",
                        help="Keep the frozen universe or explicitly restrict account sizing to mainboard")
    args = parser.parse_args(argv)
    preview_dir, pack_dir, result = build(
        args.source_run,
        args.signal,
        args.cutoff,
        args.execution_day,
        comparison=args.comparison,
        rank_override=args.rank_override,
        account=args.account,
        account_observation=args.account_observation,
        quotes=args.quotes,
        quotes_csv=args.quotes_csv,
        quote_source=args.quote_source,
        quote_observed_at=args.quote_observed_at,
        quote_trading_date=args.quote_trading_date,
        save_next=args.save_next,
        desk_out=args.desk_out,
        sizing_mode=args.sizing,
        execution_scope=args.execution_scope,
    )
    print(json.dumps({
        "preview_output": str(preview_dir),
        "opening_pack_output": str(pack_dir),
        **result,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
