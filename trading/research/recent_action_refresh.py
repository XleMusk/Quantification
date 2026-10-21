"""Explicit scoped action preparation, collection and review-aware compilation.

No account, Engine or issuer-notice request is started by this CLI. Collection
uses only the bounded current-year source path; missing reviews stay pending.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..market_dataset import write_json

FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "engine_advanced": False,
    "ledger_mutated": False, "official_notices_fetched": False}


def run(day, package, calendar_file, out, *, publications=(), account_heads=(), reuse_roots=(),
        base_event_view, base_reference_view, prior_bundle=None, reviews=(), wheel=None):
    from .recent_action_sources import prepare, collect_prepared
    from .recent_action_bundle import build
    directory = Path(out).resolve()
    if directory.exists():
        raise FileExistsError("Use a new action-refresh directory; prior attempts are retained")
    inputs = [Path(p).resolve() for p in (package, base_event_view, base_reference_view, *reuse_roots)]
    inputs += [Path(p).resolve().parent for p in (calendar_file, *publications, *account_heads, *reviews)]
    if prior_bundle is not None:
        inputs.append(Path(prior_bundle).resolve().parent)
        prior = json.loads(Path(prior_bundle).read_text(encoding="utf-8-sig"))
        if prior.get("official_event_supplements") or "official_event_supplement_mode" in prior:
            from .recent_action_extension import extension_readiness
            # Reject changed registered official evidence before any source
            # preparation/collection.  The compiler retains these exact refs.
            inspected = extension_readiness(prior_bundle, base_event_view, base_reference_view,
                calendar_file, through=prior["window_end"])
            inputs.extend(map(Path, inspected["input_directories"]))
    if any(directory == p or directory in p.parents or p in directory.parents for p in inputs):
        raise ValueError("Action-refresh output must remain outside all existing inputs")
    directory.mkdir(parents=True, exist_ok=False)
    request = {"schema": "scoped_recent_action_refresh_request_v1", "day": day,
        "started_at": datetime.now(MARKET_TZ).isoformat(),
        "package": str(Path(package).resolve()), "calendar": str(Path(calendar_file).resolve()),
        "publications": [str(Path(p).resolve()) for p in publications],
        "account_heads": [str(Path(p).resolve()) for p in account_heads],
        "reuse_roots": [str(Path(p).resolve()) for p in reuse_roots],
        "base_event_view": str(Path(base_event_view).resolve()),
        "base_reference_view": str(Path(base_reference_view).resolve()),
        "prior_bundle": str(Path(prior_bundle).resolve()) if prior_bundle is not None else None,
        "reviews": [str(Path(p).resolve()) for p in reviews], "automatic_retry": False, **FLAGS}
    write_json(directory / "request.json", request)
    try:
        prepared = prepare(day, package, calendar_file, directory / "source",
            publications=publications, account_heads=account_heads, reuse_roots=reuse_roots)
        source = collect_prepared(directory / "source", wheel)
        if source.get("source_complete") is not True:
            value = {"status": "pending_source", "bundle": None, **FLAGS}
        else:
            value = build(prepared["provider_root"], base_event_view, base_reference_view, calendar_file,
                directory / "compiled", day=day, prior_bundle=prior_bundle, reviews=reviews)
        value = {**value, "preparation": prepared, "source": source, **FLAGS}
    except Exception as error:
        write_json(directory / "result.json", {"status": "failed", "bundle": None,
            "error": f"{type(error).__name__}: {error}", "automatic_retry": False,
            "completed_at": datetime.now(MARKET_TZ).isoformat(), **FLAGS})
        raise
    value.update(schema="scoped_recent_action_refresh_result_v1", day=day,
        completed_at=datetime.now(MARKET_TZ).isoformat(), request_file=str(directory / "request.json"))
    write_json(directory / "result.json", value)
    return value


def _source_args(parser):
    parser.add_argument("--day", required=True)
    for name in ("package", "calendar", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("publication", "account-head", "reuse-root"):
        parser.add_argument("--" + name, type=Path, action="append", default=[])


def _build_args(parser, common=True):
    for name in ("action-event-view", "action-reference-view"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--prior-bundle", type=Path)
    parser.add_argument("--review", type=Path, action="append", default=[])
    if common:
        parser.add_argument("--day", required=True)
        for name in ("provider-root", "calendar", "out"):
            parser.add_argument("--" + name, type=Path, required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    _source_args(commands.add_parser("prepare"))
    collect = commands.add_parser("collect")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--wheel", type=Path)
    _build_args(commands.add_parser("build"))
    execute = commands.add_parser("run")
    _source_args(execute)
    _build_args(execute, common=False)
    execute.add_argument("--wheel", type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        from .recent_action_sources import prepare
        value = prepare(args.day, args.package, args.calendar, args.out,
            publications=args.publication, account_heads=args.account_head, reuse_roots=args.reuse_root)
    elif args.action == "collect":
        from .recent_action_sources import collect_prepared
        value = collect_prepared(args.run_dir, args.wheel)
    elif args.action == "build":
        from .recent_action_bundle import build
        value = build(args.provider_root, args.action_event_view, args.action_reference_view,
            args.calendar, args.out, day=args.day, prior_bundle=args.prior_bundle, reviews=args.review)
    else:
        value = run(args.day, args.package, args.calendar, args.out, publications=args.publication,
            account_heads=args.account_head, reuse_roots=args.reuse_root,
            base_event_view=args.action_event_view, base_reference_view=args.action_reference_view,
            prior_bundle=args.prior_bundle, reviews=args.review, wheel=args.wheel)
    source = value.get("source", value)
    compact = {key: value.get(key) for key in ("schema", "status", "bundle", "request_file")}
    compact["source"] = {key: source.get(key) for key in ("status", "provider_root", "source_complete",
        "scope_count", "reused_query_count", "pending_query_count", "new_query_count")}
    compact.update(pending_review_count=len(value.get("pending_review") or []), **FLAGS)
    print(json.dumps(compact, ensure_ascii=False))
    if args.action == "prepare" or (args.action == "collect" and value.get("source_complete") is True):
        return 0
    return 0 if value.get("status") == "ready" else 1 if value.get("status") == "failed" else 2
