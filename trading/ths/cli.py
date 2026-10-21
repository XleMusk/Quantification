"""Project-local THS discovery, observations and manual-only preview CLI."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from .. import settings
from ..contracts import MARKET_TZ
from ..portfolio_allocation import OBSERVED_LOT_ALLOCATION
from .adapter import create_plan, now
from .broker import THSAdapter
from . import journal
from .observations import csv_rows, observation
from .signals import (build_current_request, build_current_request_from_bundle,
                      build_current_request_from_scores)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _observation_bundle_paths(args):
    """Resolve one explicit bundle directory or the four legacy paths."""
    names = {
        "account_json": "account_observation.json",
        "orders_csv": "orders.csv",
        "trades_csv": "trades.csv",
        "cashflows_csv": "cashflows.csv",
    }
    bundle_dir = Path(args.bundle_dir).expanduser() if getattr(args, "bundle_dir", None) else None
    resolved = {}
    missing = []
    for attribute, filename in names.items():
        value = getattr(args, attribute, None)
        if value:
            resolved[attribute] = value
        elif bundle_dir:
            candidate = bundle_dir / filename
            if candidate.exists():
                resolved[attribute] = str(candidate)
            else:
                missing.append(str(candidate))
        else:
            missing.append(f"--{attribute.replace('_', '-')}")
    if missing:
        raise ValueError(
            "Observation bundle needs all four inputs; missing: " + ", ".join(missing)
        )
    return tuple(resolved[name] for name in names)


def _observation_bundle_profile(args):
    """Load an explicit profile, or the profile stored beside a bundle.

    A copied profile is part of the standard observation directory contract;
    discovering it here keeps preview/import/reconciliation reproducible while
    preserving explicit ``--profile`` precedence.
    """
    explicit = getattr(args, "profile", None)
    if explicit:
        return load(explicit)
    bundle_dir = Path(args.bundle_dir).expanduser() if getattr(args, "bundle_dir", None) else None
    if bundle_dir:
        for filename in ("profile.json", "observation_bundle_profile.json"):
            candidate = bundle_dir / filename
            if candidate.is_file():
                return load(candidate)
    return None


def output(value, path=None):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="Only enumerate client process/window metadata")
    probe.add_argument("--out", default=str(settings.DATA_ROOT / "ths" / "probe.json"))
    refresh_profile = sub.add_parser(
        "refresh-profile",
        help="Refresh volatile THS HWND/PID values after a strict read-only summary verification",
    )
    refresh_profile.add_argument("--profile", required=True)
    refresh_profile.add_argument("--out", required=True)
    inspect = sub.add_parser("inspect", help="Read control metadata without edit-field values")
    inspect.add_argument("--hwnd", type=int, required=True)
    inspect.add_argument("--backend", choices=["win32", "uia"], default="win32")
    inspect.add_argument("--out", required=True)
    summary = sub.add_parser(
        "capture-summary",
        help="Read inspected static THS account summary fields; never reads positions or submits",
    )
    summary.add_argument("--profile", required=True)
    summary.add_argument("--wait-seconds", type=int, choices=range(0, 31), default=5)
    summary.add_argument("--out")
    background_summary = sub.add_parser(
        "capture-summary-background",
        help="Read inspected static THS account summary fields without focusing; never writes or submits",
    )
    background_summary.add_argument("--profile", required=True)
    background_summary.add_argument("--out")
    grid_capture = sub.add_parser(
        "capture-grid-image",
        help="Capture an inspected virtual grid as read-only visual evidence; never edits or submits",
    )
    grid_capture.add_argument("--profile", required=True)
    grid_capture.add_argument("--kind", required=True)
    grid_capture.add_argument("--image-out", required=True)
    grid_capture.add_argument("--out")
    grid_ocr = sub.add_parser(
        "ocr-grid-image",
        help="OCR a captured grid into a candidate-only receipt; never imports or submits",
    )
    grid_ocr.add_argument("--image", required=True)
    grid_ocr.add_argument("--profile", required=True)
    grid_ocr.add_argument("--kind", required=True)
    grid_ocr.add_argument("--summary", help="Optional native summary receipt for aggregate cross-checks")
    grid_ocr.add_argument("--out", required=True)
    readonly_index = sub.add_parser(
        "readonly-evidence-index",
        help="Index static THS read-only receipts without importing or mutating the Ledger",
    )
    readonly_index.add_argument("--summary", required=True)
    readonly_index.add_argument("--holdings-candidate", required=True)
    readonly_index.add_argument("--holdings-receipt", required=True)
    readonly_index.add_argument("--orders-receipt", required=True)
    readonly_index.add_argument("--trades-receipt", required=True)
    readonly_index.add_argument(
        "--cashflows-receipt",
        help="Optional structured cashflow receipt; without it the package stays partial",
    )
    readonly_index.add_argument("--out", required=True)
    structured_package = sub.add_parser(
        "build-structured-observation-package",
        help="Materialize a read-only THS evidence package without importing or mutating the Ledger",
    )
    structured_package.add_argument("--index", required=True)
    structured_package.add_argument("--out-dir", required=True)
    structured_package.add_argument("--out")
    native_preview = sub.add_parser(
        "preview-native-observation-bundle",
        help="Validate native structured THS receipts and build a Ledger-ready package without saving observations",
    )
    native_preview.add_argument("--index", required=True,
                                help="ths_readonly_evidence_index_v1 with explicit native structured receipts")
    native_preview.add_argument("--out-dir", required=True)
    native_preview.add_argument("--out")
    native_import = sub.add_parser(
        "import-native-observation-bundle",
        help="Save validated native THS receipts to the journal; optional refresh/import uses the same paper Ledger",
    )
    native_import.add_argument("--index", required=True)
    native_import.add_argument("--structured-out-dir", required=True)
    native_import.add_argument("--confirm-import", action="store_true", required=True,
                               help="Explicitly authorize saving the validated native receipts")
    native_import.add_argument("--plan-id",
                               help="Optional saved THS plan to refresh after journal import")
    native_import.add_argument("--run-id",
                               help="Optional paused simulated run to receive the reconciled receipt")
    native_import.add_argument("--confirm-ledger-import", action="store_true",
                               help="Explicitly authorize importing the reconciled receipt into --run-id")
    native_import.add_argument("--out", required=True)
    capabilities = sub.add_parser("capabilities", help="Implementation flags only; no client interaction")
    capabilities.add_argument("--profile", help="Optional profile; declaration is not live verification")
    health = sub.add_parser(
        "observation-health",
        help="Read saved THS observation freshness and reconciliation gaps without broker or client I/O",
    )
    health.add_argument("--out", help="Optional JSON output path")
    for command in ("capture", "import-export"):
        item = sub.add_parser(command)
        item.add_argument("--profile", required=True)
        kinds = ["account", "orders", "trades"] + (["cashflows", "movements", "restriction_releases", "share_credits"] if command == "import-export" else [])
        item.add_argument("--kind", choices=kinds, required=True)
        item.add_argument("--trading-date", required=True)
        if command == "capture":
            item.add_argument("--wait-seconds", type=int, choices=range(0, 31), default=5)
        else:
            item.add_argument("--csv", required=True)
            item.add_argument("--observed-at", required=True, help="Actual export/read time, not import time")
            item.add_argument("--cash-rmb", help="Available cash observed with the holdings export")
            item.add_argument("--cash-total-rmb", help="Actual cash balance INCLUDING frozen cash, not total assets or withdrawable cash")
            item.add_argument("--cash-frozen-rmb", help="Actual frozen cash from the same account read; requires --cash-total-rmb")
            item.add_argument("--encoding", default="utf-8-sig")
            item.add_argument("--delimiter", choices=["comma", "tab"], default="comma")
    capture_session = sub.add_parser(
        "capture-session",
        help="Read account, orders and trades in sequence and optionally refresh one plan; read-only",
    )
    capture_session.add_argument("--profile", required=True)
    capture_session.add_argument(
        "--kinds", nargs="+", choices=["account", "orders", "trades"],
        default=["account", "orders", "trades"],
        help="Query pages to read in order (default: account orders trades)",
    )
    capture_session.add_argument("--trading-date", required=True)
    capture_session.add_argument("--wait-seconds", type=int, choices=range(0, 31), default=5)
    capture_session.add_argument("--refresh-plan", help="Refresh this existing plan after observations are saved")
    capture_session.add_argument("--account-out", help="Save the observed account JSON for prepare-opening-pack")
    capture_session.add_argument("--bundle-dir", help="Materialize captured structured views into a safe export directory")
    capture_session.add_argument(
        "--cashflows-csv",
        help="Attach one explicit user-exported cashflow CSV to --bundle-dir; never reads or submits it",
    )
    bundle_export = sub.add_parser(
        "export-observation-bundle",
        help="Materialize selected saved structured observations; never reads the client or overwrites user exports",
    )
    bundle_export.add_argument("--account", required=True)
    bundle_export.add_argument("--trading-date", required=True)
    bundle_export.add_argument("--kinds", nargs="+", choices=["account", "orders", "trades", "cashflows"], required=True)
    bundle_export.add_argument("--out-dir", required=True)
    bundle_export.add_argument("--confirm-export", action="store_true", required=True,
                               help="Explicitly authorize writing selected journal views to the export directory")
    bundle_export.add_argument("--out", help="Optional JSON receipt path")
    capture_session.add_argument("--out", help="Save the session receipt JSON")
    plan = sub.add_parser("plan", help="Check one explicit plan against latest real-account observation")
    plan.add_argument("--account", required=True)
    plan.add_argument("--symbol", required=True)
    plan.add_argument("--side", choices=["buy", "sell"], required=True)
    plan.add_argument("--price-rmb", required=True)
    plan.add_argument("--qty", type=int, required=True)
    plan.add_argument("--raw-rmb-confirmed", action="store_true", required=True)
    target = sub.add_parser("plan-target", help="Convert a current strategy absolute-share target")
    target.add_argument("--account", required=True)
    target.add_argument("--target", required=True)
    portfolio = sub.add_parser("plan-portfolio", help="Preview a complete current portfolio, sells first; no client operation")
    portfolio.add_argument("--account", required=True)
    portfolio.add_argument("--portfolio", required=True)
    portfolio.add_argument("--save-next", action="store_true", help="Save only the next plan in the existing journal; not prefill/submission")
    portfolio.add_argument("--out", help="Optional JSON preview/receipt path")
    weights = sub.add_parser("plan-weights", help="Size current equity weights into the existing share-plan preview; no client operation")
    weights.add_argument("--account", required=True)
    weights.add_argument("--weights", required=True)
    weights.add_argument("--save-next", action="store_true", help="Save only the next plan; not prefill/submission")
    weights.add_argument("--out", help="Optional JSON preview/receipt path")
    signal = sub.add_parser("plan-signal", help="Current EOD scores through the bound paper run's existing selector and next plan; no client operation")
    signal.add_argument("--run-id", required=True)
    signal.add_argument("--signal", required=True)
    signal.add_argument("--save-next", action="store_true", help="Save only the next plan; not prefill/submission")
    signal.add_argument("--out", help="Optional JSON preview/receipt path")
    build_signal = sub.add_parser("build-signal-request", help="Validate and serialize a current EOD score batch; no account/client operation")
    build_signal.add_argument("--account")
    build_signal.add_argument("--signal-frame")
    build_signal.add_argument("--scores", help="JSON list of score rows, each with explicit available_at")
    build_signal.add_argument("--score-bundle", help="Versioned current-source score handoff with provenance receipt")
    build_signal.add_argument("--day", help="Signal decision day, required with --scores")
    build_signal.add_argument("--strategy", help="Strategy source, required with --scores")
    build_signal.add_argument("--run-id", help="Bound paper run id, required with --scores")
    build_signal.add_argument("--prices")
    build_signal.add_argument("--generated-at")
    build_signal.add_argument("--out", help="Optional JSON request path")
    prefill = sub.add_parser("prefill", help="Write three inspected edit fields, read back, STOP")
    prefill.add_argument("--id", required=True)
    prefill.add_argument("--profile", required=True)
    prefill.add_argument("--confirm-plan", required=True, help="Repeat the exact reviewed plan ID")
    prefill.add_argument("--wait-seconds", type=int, choices=range(0, 31), default=5)
    prefill_payload = sub.add_parser(
        "prefill-payload",
        help="Build and optionally verify the three THS form fields without client interaction",
    )
    prefill_payload.add_argument("--plan", required=True, help="Saved reviewed plan JSON")
    prefill_payload.add_argument("--profile", required=True, help="THS field/guard mapping JSON")
    prefill_payload.add_argument("--readback-json", help="Optional captured form readback JSON")
    prefill_payload.add_argument("--out", required=True)
    batch_readback = sub.add_parser(
        "validate-prefill-readback",
        help="Validate a batch THS prefill readback template without client or Ledger operations",
    )
    batch_readback.add_argument("--template", required=True)
    batch_readback.add_argument("--out", required=True)
    handoff = sub.add_parser("prepare-handoff", help="Hydrate a paper handoff with current account and raw-RMB quotes; no client operation")
    handoff_account = handoff.add_mutually_exclusive_group(required=True)
    handoff_account.add_argument("--account")
    handoff_account.add_argument("--account-observation", help="Explicit normalized saved account JSON; no journal writes")
    handoff.add_argument("--handoff", required=True)
    handoff.add_argument("--saved-intent", help="Exact saved fixed-account research intent JSON")
    handoff_quote_source = handoff.add_mutually_exclusive_group()
    handoff_quote_source.add_argument("--quotes", help="Current raw-RMB quote observation JSON")
    handoff_quote_source.add_argument("--quotes-csv", help="Filled quote CSV with a price_rmb column")
    handoff.add_argument("--quote-source", help="Source label required with --quotes-csv")
    handoff.add_argument("--quote-observed-at", help="Timezone-qualified observation time required with --quotes-csv")
    handoff.add_argument("--quote-trading-date", help="Trading date required with --quotes-csv")
    handoff.add_argument("--sizing", choices=("replay_shares", "observed_account", "saved_intent"),
                         default="replay_shares",
                         help="Use frozen replay shares or size from the current account")
    handoff.add_argument("--execution-scope", choices=("strict", "mainboard_only"),
                         default="strict",
                         help="Keep the frozen universe or explicitly restrict account sizing to mainboard")
    handoff.add_argument("--save-next", action="store_true", help="Save only the next manual-review plan in the existing journal")
    handoff.add_argument("--out", help="Optional JSON preview/receipt path")
    opening = sub.add_parser("prepare-opening-pack", help="Hydrate every opening-pack candidate; no client operation")
    account_source = opening.add_mutually_exclusive_group(required=True)
    account_source.add_argument("--account", help="Account alias whose latest journal observation is used")
    account_source.add_argument("--account-observation", help="Direct current account observation JSON; never saved by this command")
    opening.add_argument("--pack", required=True, help="a_share_opening_pack_v1 opening_pack.json")
    opening.add_argument("--saved-intent", help="Exact saved intent for one explicitly selected strategy")
    quote_source = opening.add_mutually_exclusive_group()
    quote_source.add_argument("--quotes", help="Current raw-RMB quote observation JSON")
    quote_source.add_argument("--quotes-csv", help="Filled quote_request.csv with a price_rmb column")
    opening.add_argument("--quote-source", help="Source label required with --quotes-csv")
    opening.add_argument("--quote-observed-at", help="Timezone-qualified observation time required with --quotes-csv")
    opening.add_argument("--quote-trading-date", help="Trading date required with --quotes-csv")
    opening.add_argument("--strategy", help="Hydrate exactly this explicit strategy candidate")
    opening.add_argument("--sizing", choices=("replay_shares", "observed_account", "saved_intent"),
                         default="replay_shares",
                         help="Use frozen replay shares or size from the current account")
    opening.add_argument("--execution-scope", choices=("strict", "mainboard_only"),
                         default="strict",
                         help="Keep the frozen universe or explicitly restrict account sizing to mainboard")
    opening.add_argument("--save-next", action="store_true",
                         help="Save only the selected strategy's next manual-review plan")
    opening.add_argument("--out", help="Optional JSON preview/receipt path")
    quote = sub.add_parser("build-quote-observation", help="Normalize an explicit raw-RMB quote CSV; no client operation")
    quote.add_argument("--csv", required=True, help="CSV with symbol, market and price_rmb columns")
    quote.add_argument("--source", required=True)
    quote.add_argument("--observed-at", required=True)
    quote.add_argument("--trading-date", required=True)
    quote.add_argument("--out", required=True)
    bundle = sub.add_parser("preview-observation-bundle", help="Validate account/order/trade/cashflow exports without saving them")
    bundle.add_argument("--bundle-dir", help="Directory containing the four standard observation files")
    bundle.add_argument("--account-json")
    bundle.add_argument("--orders-csv")
    bundle.add_argument("--trades-csv")
    bundle.add_argument("--cashflows-csv")
    bundle.add_argument("--profile", help="Optional profile supplying exact export mappings")
    bundle.add_argument("--observed-at", help="Optional shared export observation timestamp; defaults to account JSON")
    bundle.add_argument("--out", required=True)
    order_risk = sub.add_parser(
        "preview-account-order-risk",
        help="Evaluate a proposed order batch against a structured account package; read-only",
    )
    order_risk.add_argument("--package", required=True, help="structured_observation_package_v1.json")
    order_risk.add_argument("--orders-json", required=True,
                            help="JSON list or object with an orders list; prices and quantities must be explicit")
    order_risk.add_argument("--quotes", help="Optional raw-RMB quote observation JSON")
    order_risk.add_argument("--out", required=True)
    bundle_import = sub.add_parser("import-observation-bundle", help="Atomically save a validated export bundle; no client operation")
    bundle_import.add_argument("--bundle-dir", help="Directory containing the four standard observation files")
    bundle_import.add_argument("--account-json")
    bundle_import.add_argument("--orders-csv")
    bundle_import.add_argument("--trades-csv")
    bundle_import.add_argument("--cashflows-csv")
    bundle_import.add_argument("--profile", help="Optional profile supplying exact export mappings")
    bundle_import.add_argument("--observed-at", help="Optional shared export observation timestamp; defaults to account JSON")
    bundle_import.add_argument("--confirm-import", action="store_true", required=True,
                               help="Explicitly authorize writing these user-export observations to the local journal")
    bundle_import.add_argument("--structured-out-dir",
                               help="Optional directory for a complete structured package after bundle validation")
    bundle_import.add_argument("--statement-kinds", nargs="+",
                               choices=("movements", "restriction_releases", "share_credits"),
                               help="Optional explicit non-trade statement CSVs in the bundle directory")
    bundle_import.add_argument("--out", required=True)
    bundle_reconcile = sub.add_parser("reconcile-observation-bundle", help="Import a validated bundle and refresh one saved plan; no client operation")
    bundle_reconcile.add_argument("--id", required=True, help="Existing saved THS plan ID")
    bundle_reconcile.add_argument("--bundle-dir", help="Directory containing the four standard observation files")
    bundle_reconcile.add_argument("--account-json")
    bundle_reconcile.add_argument("--orders-csv")
    bundle_reconcile.add_argument("--trades-csv")
    bundle_reconcile.add_argument("--cashflows-csv")
    bundle_reconcile.add_argument("--profile", help="Optional profile supplying exact export mappings")
    bundle_reconcile.add_argument("--observed-at", help="Optional shared export observation timestamp; defaults to account JSON")
    bundle_reconcile.add_argument("--confirm-import", action="store_true", required=True,
                                  help="Explicitly authorize saving these user-export observations before refresh")
    bundle_reconcile.add_argument("--run-id", help="Optional existing paused simulated run to receive the reconciled broker receipt")
    bundle_reconcile.add_argument("--confirm-ledger-import", action="store_true",
                                  help="Explicitly authorize importing the reconciled receipt into --run-id")
    bundle_reconcile.add_argument("--structured-out-dir",
                                  help="Optional directory for a complete structured package after bundle validation")
    bundle_reconcile.add_argument("--statement-kinds", nargs="+",
                                  choices=("movements", "restriction_releases", "share_credits"),
                                  help="Optional explicit non-trade statement CSVs in the bundle directory")
    bundle_reconcile.add_argument("--out", required=True)
    bind = sub.add_parser("bind", help="Record contract ID after user manually submits")
    bind.add_argument("--id", required=True)
    bind.add_argument("--contract-id", required=True)
    bind.add_argument("--trading-date", required=True)
    for command in ("reconcile", "show", "discard"):
        sub.add_parser(command).add_argument("--id", required=True)
    close = sub.add_parser("close-unsubmitted", help="Human attests form cleared and NEVER submitted; not a cancellation")
    close.add_argument("--id", required=True)
    close.add_argument("--confirm-plan", required=True)
    close.add_argument("--form-cleared-and-never-submitted", action="store_true", required=True)
    recover = sub.add_parser(
        "recover-prefill",
        help="Mark a proven terminated prefill attempt unknown; never retry or infer submission",
    )
    recover.add_argument("--id", required=True)
    recover.add_argument("--confirm-plan", required=True, help="Repeat the exact saved plan ID")
    recover.add_argument("--confirm-attempt", required=True, help="Exact result.prefill_owner.attempt_id from show")
    recover.add_argument("--out", help="Optional recovery receipt JSON")
    for command in ("pause", "resume", "status"):
        sub.add_parser(command)
    ledger_bind = sub.add_parser("bind-ledger", help="Bind an unused paused paper run to an explicitly attested simulated account")
    ledger_bind.add_argument("--run-id", required=True)
    ledger_bind.add_argument("--account", required=True)
    ledger_bind.add_argument("--confirm-simulated-account", action="store_true", required=True)
    ledger_bind.add_argument("--lot-allocation", choices=[OBSERVED_LOT_ALLOCATION],
                             help="Explicit immutable current-mark integer allocation; omitted keeps original floor sizing")
    ledger_import = sub.add_parser("import-ledger", help="Import reconciled broker exports into that same paused run; no client operations")
    ledger_import.add_argument("--run-id", required=True)
    ledger_import.add_argument("--id", required=True)
    ledger_preview = sub.add_parser(
        "preview-ledger-import",
        help="Preview exact cash/position/order/fill deltas before Ledger import; rolls back all staged evidence",
    )
    ledger_preview.add_argument("--run-id", required=True)
    ledger_preview.add_argument("--id", required=True)
    ledger_preview.add_argument("--bundle-dir", help="Optional directory containing the four observation exports to stage temporarily")
    ledger_preview.add_argument("--account-json")
    ledger_preview.add_argument("--orders-csv")
    ledger_preview.add_argument("--trades-csv")
    ledger_preview.add_argument("--cashflows-csv")
    ledger_preview.add_argument("--profile", help="Optional observation export mapping profile")
    ledger_preview.add_argument("--observed-at", help="Optional shared observation timestamp for the staged export bundle")
    ledger_preview.add_argument("--out")
    session = sub.add_parser("reconcile-session", help="Verify next exchange session's broker balances against the same ledger; no client operation")
    session.add_argument("--run-id", required=True)
    session.add_argument("--observed-at", help="Optional exact imported account timestamp for an idempotent historical receipt")
    session.add_argument("--include-restriction-releases", action="store_true",
                         help="Consume explicit opening-restriction release exports at the new session; never infer unlocks")
    session.add_argument("--include-share-credits", action="store_true",
                         help="Consume explicit account bonus/capital-transfer share receipts at the new session")
    movements = sub.add_parser("import-movements", help="Append proven same-session nontrade cash exports before the first new plan; no client operation")
    movements.add_argument("--run-id", required=True)
    movements.add_argument("--observed-at", help="Optional exact account timestamp to reread an already imported statement")
    return parser


def run(args):
    if args.command == "build-signal-request":
        supplied = [value for value in (args.signal_frame, args.scores, args.score_bundle) if value]
        if len(supplied) != 1:
            raise ValueError("Provide exactly one of --signal-frame, --scores or --score-bundle")
        if args.score_bundle:
            if args.prices or args.account:
                raise ValueError("--score-bundle carries its own prices and account; omit --prices and --account")
            value = build_current_request_from_bundle(load(args.score_bundle))
        elif not args.account or not args.prices:
            raise ValueError("--account and --prices are required with --signal-frame or --scores")
        elif args.signal_frame:
            from ..contracts import SignalFrame
            frame = SignalFrame.from_dict(load(args.signal_frame))
            value = build_current_request(frame, load(args.prices), args.account, generated_at=args.generated_at)
        else:
            if not all((args.day, args.strategy, args.run_id)):
                raise ValueError("--scores requires --day, --strategy and --run-id")
            value = build_current_request_from_scores(load(args.scores), args.day, args.strategy, args.run_id,
                                                      load(args.prices), args.account, generated_at=args.generated_at)
        output(value, args.out)
        return
    if args.command == "capabilities":
        profile = load(args.profile) if args.profile else {}
        output({**THSAdapter(profile).capabilities.to_dict(),
                "local_mapping_declared": profile.get("verified_local") is True,
                "client_verified_this_call": False})
        return
    if args.command == "observation-health":
        from ..api import ths_observation_health
        output(ths_observation_health(), args.out)
        return
    if args.command == "probe":
        from .windows import probe
        value = probe()
        value["observed_at"] = now().isoformat()
        output(value, args.out)
        return
    if args.command == "refresh-profile":
        from .profile_refresh import write_refreshed_profile
        refreshed = write_refreshed_profile(args.profile, args.out)
        output({
            "status": "verified_read_only",
            "profile": str(Path(args.out)),
            "session_refresh": refreshed.get("session_refresh"),
            "client_interacted": True,
            "submitted": False,
            "execution_admitted": False,
        })
        return
    if args.command == "prefill-payload":
        from .adapter import (build_prefill_payload, compare_prefill_readback,
                              validate_gui_prefill_profile, verify_prefill_readback)
        plan = load(args.plan)
        profile = validate_gui_prefill_profile(load(args.profile))
        fields = profile["fields"]
        expected = build_prefill_payload(plan)
        result = {
            "schema": "ths_prefill_payload_preview_v1",
            "status": "payload_ready",
            "plan_id": plan.get("id"),
            "account_alias": plan.get("account_alias"),
            "field_payload": [
                {"field": key, "selector": fields[key], "value": expected[key]}
                for key in ("symbol", "price", "qty")
            ],
            "guard_declarations": profile.get("guards") or {},
            "client_interacted": False,
            "submitted": False,
            "execution_admitted": False,
        }
        if args.readback_json:
            readback = load(args.readback_json)
            try:
                result.update(verify_prefill_readback(plan, readback))
            except ValueError as error:
                result.update(compare_prefill_readback(plan, readback))
                result["reason"] = str(error)
        output(result, args.out)
        return
    if args.command == "validate-prefill-readback":
        from ..research.offline_strategy_plan import validate_prefill_readback_template
        output(validate_prefill_readback_template(load(args.template)), args.out)
        return
    if args.command == "inspect":
        from .windows import ControlWindow, probe
        found = probe()
        matches = [w for w in found["windows"] if w["hwnd"] == args.hwnd]
        if len(matches) != 1:
            raise ValueError("Exact candidate HWND not found; run probe again")
        window = matches[0]
        process = next(p for p in found["processes"] if p["pid"] == window["pid"])
        target = {**window, "exe": process["exe"], "version": process["version"], "backend": args.backend}
        output({"target": target, "verified_local": False, "controls": ControlWindow(target).inspect()}, args.out)
        return
    if args.command == "capture-summary":
        profile = load(args.profile)
        if profile.get("summary_verified") is not True:
            raise ValueError("Summary mapping has not been explicitly verified against the current client")
        print(
            "Put the inspected THS account window in foreground. "
            "No navigation, refresh, edit or confirmation will be sent.",
            file=sys.stderr,
        )
        time.sleep(args.wait_seconds)
        from .windows import ControlWindow
        values = ControlWindow(profile["target"]).read_summary(
            profile["summary"], process=profile.get("summary_process"),
            expected_account_alias=profile.get("expected_account_alias"))
        observed_at = now().isoformat()
        result = {
            "schema": "ths_native_account_summary_v1",
            "status": "read",
            "source": "ths_controls_summary",
            "observed_at": observed_at,
            "account_alias": values["account_alias"],
            "metrics_raw": {key: values[key] for key in values if key != "account_alias"},
            "positions_read": False,
            "orders_read": False,
            "trades_read": False,
            "full_account_observation": False,
            "client_interacted": True,
            "submitted": False,
            "execution_admitted": False,
            "next_action": "Put the THS holdings/order/trade query views in foreground or export their tables; do not use this summary as an order-sizing snapshot.",
        }
        output(result, args.out)
        return
    if args.command == "capture-summary-background":
        profile = load(args.profile)
        if profile.get("summary_verified") is not True:
            raise ValueError("Summary mapping has not been explicitly verified against the current client")
        from .windows import ControlWindow
        values = ControlWindow(profile["target"]).read_summary_background(
            profile["summary"], process=profile.get("summary_process"),
            expected_account_alias=profile.get("expected_account_alias"))
        result = {
            "schema": "ths_native_account_summary_v1",
            "status": "read_background",
            "source": "ths_controls_summary_background_read_only",
            "observed_at": now().isoformat(),
            "account_alias": values["account_alias"],
            "metrics_raw": {key: values[key] for key in values if key != "account_alias"},
            "foreground_verified": False,
            "positions_read": False,
            "orders_read": False,
            "trades_read": False,
            "full_account_observation": False,
            "client_interacted": True,
            "submitted": False,
            "execution_admitted": False,
            "next_action": "Use this background summary for display only; foreground the exact page or import explicit exports before sizing or reconciliation.",
        }
        output(result, args.out)
        return
    if args.command == "capture-grid-image":
        profile = load(args.profile)
        if profile.get("summary_verified") is not True:
            raise ValueError("Grid mapping has not been explicitly verified against the current client")
        grids = profile.get("grids") or {}
        if args.kind not in grids:
            raise ValueError(f"No verified grid mapping for kind: {args.kind}")
        from .windows import ControlWindow
        value = ControlWindow(profile["target"]).capture_grid_image(
            grids[args.kind], args.image_out, process=profile.get("summary_process"))
        result = {
            "schema": "ths_native_grid_image_v1",
            "status": "captured",
            "source": "ths_virtual_grid_printwindow",
            "observed_at": now().isoformat(),
            "kind": args.kind,
            **value,
            "execution_admitted": False,
            "client_interacted": True,
            "submitted": False,
            "next_action": "Use the image only for visual review; import a matching explicit THS export before account sizing or reconciliation.",
        }
        output(result, args.out)
        return
    if args.command == "ocr-grid-image":
        profile = load(args.profile)
        ocr_profile = profile.get("ocr") or {}
        mapping = ocr_profile.get(args.kind)
        mapping_kind = args.kind
        if not mapping and args.kind in {"orders", "trades"}:
            # Some THS builds use one compact activity grid for both tabs.
            # Retain the requested semantic kind in the candidate while using
            # the verified activity column mapping.
            mapping = ocr_profile.get("activity")
            mapping_kind = "activity"
        if not mapping:
            raise ValueError(f"No verified OCR mapping for kind: {args.kind}")
        summary = load(args.summary) if args.summary else None
        from .visual import read_grid_candidate
        output(read_grid_candidate(args.image, {**mapping, "kind": mapping_kind},
                                   summary=summary, kind=args.kind), args.out)
        return
    if args.command == "readonly-evidence-index":
        from .readonly_evidence import build_readonly_evidence_index
        output(build_readonly_evidence_index(
            args.summary, args.holdings_candidate, args.holdings_receipt,
            args.orders_receipt, args.trades_receipt, args.cashflows_receipt), args.out)
        return
    if args.command == "build-structured-observation-package":
        from .structured_observation import build_structured_observation_package
        output(build_structured_observation_package(args.index, args.out_dir), args.out)
        return
    if args.command == "preview-native-observation-bundle":
        from .structured_observation import build_structured_observation_package_from_native_index
        output(build_structured_observation_package_from_native_index(
            args.index, args.out_dir, source_scope="isolated_rehearsal"), args.out)
        return
    if args.command == "import-native-observation-bundle":
        if not args.confirm_import:
            raise PermissionError("Explicit native receipt import confirmation is required")
        if args.run_id and not args.plan_id:
            raise ValueError("--run-id requires --plan-id so the same Ledger order can be reconciled")
        if args.run_id and not args.confirm_ledger_import:
            raise PermissionError("Explicit Ledger import confirmation is required with --run-id")
        from .structured_observation import (
            build_observations_from_native_index,
            build_structured_observation_package_from_native_index,
        )
        values, paths = build_observations_from_native_index(args.index)
        ledger_preview = None
        if args.run_id:
            from .ledger_import import preview_plan
            ledger_preview = preview_plan(args.run_id, args.plan_id, observations=values)
            if ledger_preview.get("status") not in {"ready_for_import", "already_imported_no_change"}:
                raise ValueError(
                    "Native receipt failed the same-Ledger preview: "
                    + str(ledger_preview.get("error") or ledger_preview.get("status"))
                )
        structured = build_structured_observation_package_from_native_index(
            args.index, args.structured_out_dir, source_scope="persistent_user_export")
        atomic_import = None
        if args.run_id:
            from .ledger_import import import_observation_bundle
            atomic_import = import_observation_bundle(
                args.run_id, args.plan_id, values, confirm=args.confirm_ledger_import)
            receipt = atomic_import["journal"]
        else:
            journal.initialize()
            receipt = journal.observe_many(values)
        result = {
            "schema": "ths_native_observation_bundle_import_v1",
            "status": "imported",
            "journal": receipt,
            "structured_package": structured,
            "source_paths": paths,
            "ledger_preview": ledger_preview,
            "execution_admitted": False,
            "client_operation": False,
            "ledger_mutated": False,
            "ledger_imported": False,
        }
        if atomic_import is not None:
            result.update({key: atomic_import[key] for key in (
                "atomic", "plan", "ledger", "ledger_imported", "ledger_mutated", "idempotent_replay",
            )})
        elif args.plan_id:
            result["plan"] = journal.refresh(args.plan_id)
        output(result, args.out)
        return
    if args.command == "preview-observation-bundle":
        from .observation_preview import preview_bundle
        profile = _observation_bundle_profile(args)
        account_json, orders_csv, trades_csv, cashflows_csv = _observation_bundle_paths(args)
        output(preview_bundle(account_json, orders_csv, trades_csv, cashflows_csv,
                              profile=profile, observed_at=args.observed_at), args.out)
        return
    if args.command == "preview-account-order-risk":
        from .account_risk import evaluate_order_risk
        package = load(args.package)
        order_payload = load(args.orders_json)
        if isinstance(order_payload, dict):
            order_payload = order_payload.get("orders")
        quote_observation = load(args.quotes) if args.quotes else None
        output(evaluate_order_risk(package, order_payload, quote_observation=quote_observation), args.out)
        return
    if args.command == "preview-ledger-import":
        from .ledger_import import preview_plan
        observations = None
        bundle_preview = None
        bundle_args = (args.bundle_dir, args.account_json, args.orders_csv,
                       args.trades_csv, args.cashflows_csv)
        if any(bundle_args):
            from .observation_preview import build_observation_bundle, preview_bundle
            profile = _observation_bundle_profile(args)
            account_json, orders_csv, trades_csv, cashflows_csv = _observation_bundle_paths(args)
            bundle_preview = preview_bundle(
                account_json, orders_csv, trades_csv, cashflows_csv,
                profile=profile, observed_at=args.observed_at,
            )
            if bundle_preview.get("status") != "bundle_ready_for_reconciliation":
                output({
                    "schema": "ths_ledger_import_preview_v1",
                    "status": "blocked_by_observation_bundle",
                    "execution_admitted": False,
                    "ledger_mutated": False,
                    "bundle_preview": bundle_preview,
                }, args.out)
                return
            _, observations = build_observation_bundle(
                account_json, orders_csv, trades_csv, cashflows_csv,
                profile=profile, observed_at=args.observed_at,
            )
        result = preview_plan(args.run_id, args.id, observations=observations)
        if bundle_preview is not None:
            result["bundle_preview"] = bundle_preview
        output(result, args.out)
        return
    if (args.command == "reconcile-observation-bundle"
            and args.run_id and not args.confirm_ledger_import):
        raise PermissionError("Explicit Ledger import confirmation is required with --run-id")
    saved_diagnostic = (args.command in {"prepare-handoff", "prepare-opening-pack"}
                        and args.sizing == "saved_intent")
    if saved_diagnostic and args.save_next:
        raise ValueError("Saved research intents cannot be saved as prefill plans")
    if saved_diagnostic and not args.account_observation:
        raise ValueError("Saved intent diagnostics require --account-observation; no journal initialization")
    if saved_diagnostic and args.quotes_csv:
        raise ValueError("Saved intent diagnostics require account-bound --quotes JSON; CSV cannot prove account identity")
    if not (saved_diagnostic and args.account_observation):
        journal.initialize()
    if args.command in {"import-observation-bundle", "reconcile-observation-bundle"}:
        if not args.confirm_import:
            raise PermissionError("Explicit bundle import confirmation is required")
        from .observation_preview import build_observation_bundle, build_statement_observations
        profile = _observation_bundle_profile(args)
        account_json, orders_csv, trades_csv, cashflows_csv = _observation_bundle_paths(args)
        preview, observations = build_observation_bundle(
            account_json, orders_csv, trades_csv, cashflows_csv,
            profile=profile, observed_at=args.observed_at)
        statement_kinds = list(getattr(args, "statement_kinds", None) or [])
        if len(statement_kinds) > 3 or len(set(statement_kinds)) != len(statement_kinds):
            raise ValueError("Statement export kinds must be unique and contain at most three views")
        if statement_kinds and getattr(args, "run_id", None):
            raise ValueError("Statement exports cannot be combined with --run-id atomic trade import")
        bundle_root = Path(args.bundle_dir).expanduser().resolve() if args.bundle_dir else Path(account_json).parent
        statement_paths = {kind: bundle_root / f"{kind}.csv" for kind in statement_kinds}
        statement_observations = build_statement_observations(
            observations[0], statement_paths, profile=profile, observed_at=args.observed_at
        ) if statement_kinds else []
        all_observations = [*observations, *statement_observations]
        if args.command == "reconcile-observation-bundle":
            plan = journal.get(args.id)
            aliases = {value.get("account_alias") for value in all_observations}
            dates = {value.get("trading_date") for value in all_observations}
            if aliases != {plan.get("account_alias")} or dates != {plan.get("trading_date")}:
                raise ValueError(
                    "Observation bundle does not match the saved plan account or trading date"
                )
            if args.run_id:
                from .ledger_import import preview_plan
                ledger_preview = preview_plan(args.run_id, args.id, observations=observations)
                if ledger_preview.get("status") not in {"ready_for_import", "already_imported_no_change"}:
                    raise ValueError(
                        "Observation bundle failed the same-Ledger preview: "
                        + str(ledger_preview.get("error") or ledger_preview.get("status"))
                    )
        from .structured_observation import build_structured_observation_package_from_bundle
        structured_out_dir = getattr(args, "structured_out_dir", None)
        if not structured_out_dir:
            # Keep every validated import consumable without a second manual
            # CLI step.  A unique directory preserves prior evidence on re-read.
            structured_out_dir = (
                settings.DATA_ROOT / "ths" / "observation_bundle" / "structured"
                / f"import_{now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
            )
        structured_root = Path(structured_out_dir).expanduser().resolve()
        rehearsal_root = (settings.DATA_ROOT / "research" / "ths_bundle_rehearsal").resolve()
        source_scope = "isolated_rehearsal" if structured_root.is_relative_to(rehearsal_root) else "persistent_user_export"
        structured = build_structured_observation_package_from_bundle(
            preview, observations, structured_out_dir,
            source_paths={"account": account_json, "orders": orders_csv,
                          "trades": trades_csv, "cashflows": cashflows_csv},
            source_scope=source_scope)
        atomic_import = None
        if args.command == "reconcile-observation-bundle" and args.run_id:
            from .ledger_import import import_observation_bundle
            atomic_import = import_observation_bundle(
                args.run_id, args.id, observations, confirm=args.confirm_ledger_import)
            receipt = atomic_import["journal"]
        else:
            receipt = journal.observe_many(all_observations)
        result = {"schema": "ths_observation_bundle_import_v1", "status": "imported",
                "execution_admitted": False, "client_operation": False, "ledger_mutated": False,
                "preview": preview, "journal": receipt,
                "idempotent_replay": bool(receipt.get("idempotent_replay")),
                "statement_observations": {
                    "kinds": statement_kinds,
                    "rows": {value["kind"]: len(value["rows"]) for value in statement_observations},
                    "source_files": {kind: str(path) for kind, path in statement_paths.items()},
                    "ledger_mutated": False,
                }}
        if args.command == "reconcile-observation-bundle" and args.run_id:
            result["ledger_preview"] = ledger_preview
        if structured:
            result["structured_package"] = structured
        if args.command == "reconcile-observation-bundle":
            result["status"] = "reconciled"
            if atomic_import is not None:
                result.update({key: atomic_import[key] for key in (
                    "atomic", "plan", "ledger", "ledger_imported", "ledger_mutated", "idempotent_replay",
                )})
            else:
                result["plan"] = journal.refresh(args.id)
                result["ledger_imported"] = False
        output(result, args.out)
        return
    if args.command == "capture-session":
        profile = load(args.profile)
        if args.trading_date != now().astimezone(MARKET_TZ).date().isoformat():
            raise ValueError("Control capture supports today's query pages only")
        if len(set(args.kinds)) != len(args.kinds):
            raise ValueError("Each query page kind may appear only once")
        if args.cashflows_csv and not args.bundle_dir:
            raise ValueError("--cashflows-csv requires --bundle-dir")
        from .windows import ControlWindow
        adapter = THSAdapter(profile, ControlWindow(profile["target"]))
        captured = []
        for kind in args.kinds:
            print(
                f"Put the mapped {kind} query page in foreground. "
                "No navigation or confirmation will be sent.",
                file=sys.stderr,
            )
            time.sleep(args.wait_seconds)
            value = adapter.read(kind, trading_date=args.trading_date)
            captured.append(value)
        bundle = None
        if args.bundle_dir:
            from .bundle_export import export_observations
            bundle = export_observations(captured, args.bundle_dir)
            if args.cashflows_csv:
                from .bundle_export import KINDS, attach_cashflow_export
                cashflow_path = attach_cashflow_export(args.cashflows_csv, args.bundle_dir)
                bundle["kinds"] = sorted(set(bundle["kinds"]) | {"cashflows"})
                bundle["missing_kinds"] = [kind for kind in KINDS if kind not in bundle["kinds"]]
                bundle["files"]["cashflows"] = str(cashflow_path)
                bundle["status"] = "materialized" if not bundle["missing_kinds"] else "materialized_partial"
                bundle["next_action"] = (
                    "review the four-file pack and validate the explicit cashflows export with the bundle profile"
                    if not bundle["missing_kinds"] else bundle["next_action"]
                )
        observation_receipt = journal.observe_many(captured)
        result = {
            "status": "observed",
            "account_alias": profile["account_alias"],
            "trading_date": args.trading_date,
            "kinds": [value["kind"] for value in captured],
            "observed_at": {value["kind"]: value["observed_at"] for value in captured},
            "submitted_by_adapter": False,
            "observations_saved": True,
            "journal": observation_receipt,
        }
        account = next((value for value in captured if value["kind"] == "account"), None)
        if args.account_out:
            if account is None:
                raise ValueError("--account-out requires account in --kinds")
            output(account, args.account_out)
            result["account_observation_path"] = str(Path(args.account_out))
        if bundle is not None:
            from .observation_preview import preview_bundle
            from .bundle_export import KINDS
            root = Path(args.bundle_dir)
            if set(KINDS).issubset(set(bundle["kinds"])):
                if args.cashflows_csv:
                    bundle["preview"] = {
                        "schema": "ths_observation_bundle_preview_v1",
                        "status": "waiting_for_profile_validation",
                        "cashflows_source": str(Path(args.cashflows_csv).expanduser()),
                        "execution_admitted": False,
                        "ledger_mutated": False,
                        "next_action": "validate the explicit cashflows export with the bundle profile before reconciliation",
                    }
                else:
                    bundle["preview"] = preview_bundle(
                        root / "account_observation.json", root / "orders.csv",
                        root / "trades.csv", root / "cashflows.csv",
                    )
            else:
                bundle["preview"] = {
                    "schema": "ths_observation_bundle_preview_v1",
                    "status": "waiting_for_missing_exports",
                    "missing_kinds": bundle.get("missing_kinds", []),
                    "execution_admitted": False,
                    "ledger_mutated": False,
                    "next_action": bundle["next_action"],
                }
            result["observation_bundle"] = bundle
        if args.refresh_plan:
            result["plan"] = journal.refresh(args.refresh_plan)
        output(result, args.out)
    elif args.command == "export-observation-bundle":
        if not args.confirm_export:
            raise PermissionError("Explicit bundle export confirmation is required")
        from .bundle_export import export_observations
        if len(set(args.kinds)) != len(args.kinds):
            raise ValueError("Each observation kind may be exported once")
        values = [journal.latest(args.account, kind, args.trading_date) for kind in args.kinds]
        receipt = export_observations(values, args.out_dir)
        root = Path(args.out_dir)
        from .observation_preview import preview_bundle
        from .bundle_export import KINDS
        if set(KINDS).issubset(set(receipt["kinds"])):
            receipt["preview"] = preview_bundle(
                root / "account_observation.json", root / "orders.csv",
                root / "trades.csv", root / "cashflows.csv",
            )
        else:
            receipt["preview"] = {
                "schema": "ths_observation_bundle_preview_v1",
                "status": "waiting_for_missing_exports",
                "missing_kinds": receipt.get("missing_kinds", []),
                "execution_admitted": False,
                "ledger_mutated": False,
                "next_action": receipt["next_action"],
            }
        output(receipt, args.out)
    elif args.command in {"capture", "import-export"}:
        profile = load(args.profile)
        mapping = profile["tables"][args.kind]
        if args.command == "capture":
            from .windows import ControlWindow
            if args.trading_date != now().astimezone(MARKET_TZ).date().isoformat():
                raise ValueError("Control capture supports today's query pages only")
            print("Put the mapped client query page in foreground. No navigation or confirmation will be sent.", file=sys.stderr)
            time.sleep(args.wait_seconds)
            value = THSAdapter(profile, ControlWindow(profile["target"])).read(args.kind, trading_date=args.trading_date)
        else:
            rows = csv_rows(args.csv, mapping["columns"], encoding=args.encoding, delimiter="," if args.delimiter == "comma" else "\t")
            value = observation(args.kind, rows, mapping, account_alias=profile["account_alias"],
                                observed_at=args.observed_at, trading_date=args.trading_date, source="ths_user_export", cash=args.cash_rmb,
                                cash_total=args.cash_total_rmb, cash_frozen=args.cash_frozen_rmb)
        output(journal.observe(value))
    elif args.command in {"plan", "plan-target"}:
        snapshot = journal.latest(args.account, "account")
        if args.command == "plan":
            plan = create_plan(snapshot, args.symbol, args.side, args.price_rmb, args.qty, price_basis="raw_rmb")
        else:
            plan = THSAdapter({"account_alias": args.account}).prepare(snapshot, load(args.target))
        output(journal.save_plan(plan))
    elif args.command in {"plan-portfolio", "plan-weights", "plan-signal"}:
        if args.command == "plan-signal":
            request = load(args.signal)
            value = THSAdapter({"account_alias": request["account_alias"]}).prepare_signal(args.run_id, request)
        else:
            snapshot = journal.latest(args.account, "account")
            broker = THSAdapter({"account_alias": args.account})
            value = (broker.prepare_portfolio(snapshot, load(args.portfolio)) if args.command == "plan-portfolio"
                     else broker.prepare_weights(snapshot, load(args.weights)))
        if args.save_next:
            if value["next_plan"] is None:
                raise ValueError(f"Portfolio has no next plan: {value.get('reason', value['status'])}")
            value["next_plan"] = journal.save_plan(value["next_plan"])
            value["saved"] = True
        output(value, args.out)
    elif args.command == "prefill":
        from .windows import ControlWindow
        if args.confirm_plan != args.id:
            raise ValueError("Explicit plan confirmation does not match")
        profile = load(args.profile)
        print("Prefill only, no submit. Put the verified order page in foreground.", file=sys.stderr)
        time.sleep(args.wait_seconds)
        output(journal.prefill(args.id, THSAdapter(profile, ControlWindow(profile["target"]))))
    elif args.command == "prepare-handoff":
        from .handoff import prepare_handoff
        if args.account_observation:
            snapshot = load(args.account_observation)
            if snapshot.get("kind") != "account":
                raise ValueError("--account-observation must be a normalized account observation")
        else:
            snapshot = journal.latest(args.account, "account")
        if args.quotes:
            quote_observation = load(args.quotes)
        elif args.quotes_csv:
            if not all((args.quote_source, args.quote_observed_at, args.quote_trading_date)):
                raise ValueError("--quotes-csv requires --quote-source, --quote-observed-at and --quote-trading-date")
            from .quotes import build_quote_observation
            quote_observation = build_quote_observation(
                args.quotes_csv,
                source=args.quote_source,
                observed_at=args.quote_observed_at,
                trading_date=args.quote_trading_date,
            )
        else:
            quote_observation = None
        value = prepare_handoff(load(args.handoff), snapshot, quote_observation,
                                sizing_mode=args.sizing, execution_scope=args.execution_scope,
                                **({"saved_intent": load(args.saved_intent)} if args.saved_intent else {}))
        if args.save_next:
            if value["next_plan"] is None:
                raise ValueError(f"Handoff has no next plan: {value.get('reason', value['status'])}")
            value["next_plan"] = journal.save_plan(value["next_plan"])
            value["saved"] = True
        output(value, args.out)
    elif args.command == "prepare-opening-pack":
        from .handoff import prepare_opening_pack
        if args.save_next and not args.strategy:
            raise ValueError("--save-next requires an explicit --strategy")
        if args.account_observation:
            snapshot = load(args.account_observation)
            if snapshot.get("kind") != "account":
                raise ValueError("--account-observation must be a normalized account observation")
        else:
            snapshot = journal.latest(args.account, "account")
        if args.quotes:
            quote_observation = load(args.quotes)
        elif args.quotes_csv:
            if not all((args.quote_source, args.quote_observed_at, args.quote_trading_date)):
                raise ValueError("--quotes-csv requires --quote-source, --quote-observed-at and --quote-trading-date")
            from .quotes import build_quote_observation
            quote_observation = build_quote_observation(
                args.quotes_csv,
                source=args.quote_source,
                observed_at=args.quote_observed_at,
                trading_date=args.quote_trading_date,
            )
        else:
            quote_observation = None
        value = prepare_opening_pack(load(args.pack), snapshot, quote_observation,
                                     strategy_id=args.strategy, sizing_mode=args.sizing,
                                     execution_scope=args.execution_scope,
                                     **({"saved_intent": load(args.saved_intent)} if args.saved_intent else {}))
        if args.save_next:
            ready = [candidate for candidate in value["candidates"]
                     if candidate.get("status") == "current_preview_only"]
            if len(ready) != 1 or not ready[0]["plan"].get("next_plan"):
                raise ValueError("Selected strategy has no saveable next plan")
            ready[0]["plan"]["next_plan"] = journal.save_plan(ready[0]["plan"]["next_plan"])
            ready[0]["plan"]["saved"] = True
            value["saved"] = True
            value["saved_strategy_id"] = ready[0]["strategy_id"]
            value["not_attempted"] = [
                "Only one next manual-review plan was saved in the existing THS journal.",
                "No THS client operation, prefill, submission, cancellation, or transfer was attempted.",
            ]
        output(value, args.out)
    elif args.command == "build-quote-observation":
        from .quotes import build_quote_observation
        output(build_quote_observation(args.csv, source=args.source, observed_at=args.observed_at,
                                       trading_date=args.trading_date), args.out)
    elif args.command == "bind":
        output(journal.bind(args.id, args.contract_id, args.trading_date))
    elif args.command == "bind-ledger":
        from .ledger_import import bind_account
        output(bind_account(args.run_id, args.account, confirm_simulated_account=args.confirm_simulated_account,
                            lot_allocation=args.lot_allocation))
    elif args.command == "import-ledger":
        from .ledger_import import import_plan
        output(import_plan(args.run_id, args.id))
    elif args.command == "reconcile-session":
        from .ledger_import import reconcile_session
        output(reconcile_session(args.run_id, observed_at=args.observed_at,
                                 restriction_releases=args.include_restriction_releases,
                                 share_credits=args.include_share_credits))
    elif args.command == "import-movements":
        from .ledger_import import import_movements
        output(import_movements(args.run_id, observed_at=args.observed_at))
    elif args.command in {"pause", "resume"}:
        output(journal.pause(args.command == "pause"))
    elif args.command == "status":
        output(journal.status())
    elif args.command == "recover-prefill":
        if args.confirm_plan != args.id:
            raise ValueError("Explicit plan acknowledgement does not match")
        output(journal.recover_prefill(args.id, confirm_attempt=args.confirm_attempt), args.out)
    elif args.command == "close-unsubmitted":
        if args.confirm_plan != args.id:
            raise ValueError("Explicit plan acknowledgement does not match")
        output(journal.close_unsubmitted(args.id))
    else:
        method = {"reconcile": journal.refresh, "show": journal.get, "discard": journal.discard}[args.command]
        output(method(args.id))


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        run(args)
    except Exception as error:
        print(f"THS stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
