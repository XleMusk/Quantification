"""Publish unchanged saved monthly low-volatility decisions at an actual clock.

This module reads rank caches and one strict close. It does not scan historical
price chunks, create an account, advance an Engine, or claim broker readiness.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, PortfolioIntent, PreopenPortfolioIntent, RunContext
from ..engine import choose_targets
from ..rules import stock_board
from .leader_daily_signals import _calendar_view
from .monthly_timing import month_ends
from .shortline_native_preopen_forward import _source
from .stockdb_rolling_low_volatility import portfolio


METHOD_ID = "native_lowvol_saved_monthly_preopen_forward_v1"
SCHEMA = "native_lowvol_preopen_publication_v1"
VARIANT = "low_volatility_252_20"
INCEPTIONS = ("fresh_cash_wait_next_month_end", "bootstrap_latest_saved_monthly_rank")
FLAGS = {"candidate_panel_only": True, "research_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "returns_reportable": False,
    "pit_verified": False, "corporate_actions_verified": False, "model_refit": False,
    "signal_features_recomputed": False, "ranks_recomputed": False,
    "historical_signal_publication_verified": False, "broker_interacted": False,
    "old_account_migrated": False, "account_created": False, "engine_advanced": False}


def _now(now=None):
    value = datetime.now(MARKET_TZ) if now is None else now
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Actual publication clock must be timezone aware")
    return value.astimezone(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path, role=None):
    path = Path(path).resolve()
    data = path.read_bytes()
    result = {"file": str(path), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}
    if role is not None:
        result["role"] = role
    return result


def _checked(ref):
    actual = _ref(ref["file"])
    if any(actual[key] != ref.get(key) for key in ("file", "sha256", "byte_count")):
        raise ValueError("Saved publication source hash or identity changed")
    return Path(actual["file"])


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _calendar(path):
    ref = _ref(path)
    payload = _read(path)
    year = payload["year"]
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("An explicit fixed exchange calendar is required")
    return ref, sessions


def _cache(saved_run, day, sessions):
    path = Path(saved_run).resolve()
    path = path / "run.json" if path.is_dir() else path
    run = _read(path)
    folder = Path(run["artifacts"]).resolve()
    protocol = _read(folder / "protocol.json")
    if (run.get("schema") != "stockdb_rolling_low_volatility_ledger_v1"
            or run.get("status") != "completed_research_only" or run.get("research_only") is not True
            or run.get("execution_admitted") is not False or run.get("variants") != [VARIANT]
            or day < run["replay_start"]
            or protocol.get("schema") != run["schema"] or protocol.get("variants") != [VARIANT]
            or any(protocol.get(k) != run.get(k) for k in ("config", "replay_start", "replay_end", "sessions", "symbols", "supplemental_raw_view"))
            or protocol.get("lookback") != 252 or protocol.get("skip") != 21
            or protocol.get("rank_cadence") != "monthly_completed_session_end"):
        raise ValueError("A completed unchanged single-variant monthly rank cache is required")
    config = StrategyConfig(**run["config"]).validate()
    if config != portfolio(config.initial_cash):
        raise ValueError("Original fixed low-volatility config changed")
    snapshot = Path(protocol["snapshot"]).resolve() / "manifest.json"
    if _read(snapshot) != protocol["snapshot_manifest"]:
        raise ValueError("Original snapshot manifest changed")
    rank_file, decision_file = folder / f"{VARIANT}_ranks.csv", folder / f"{VARIANT}_decisions.csv"
    ranks = pd.read_csv(rank_file, dtype={"date": str, "symbol": str, "variant": str})
    decisions = pd.read_csv(decision_file, dtype={"date": str, "decision": str, "variant": str})
    if (not {"date", "variant", "rank", "symbol", "volatility", "momentum", "median_amount"} <= set(ranks)
            or not {"date", "variant", "decision", "eligible_count", "candidate_count"} <= set(decisions)
            or not ranks.variant.eq(VARIANT).all() or not decisions.variant.eq(VARIANT).all()
            or decisions.date.duplicated().any() or ranks.duplicated(["date", "symbol"]).any()):
        raise ValueError("Saved monthly ranks or exact decision coverage is incomplete")
    monthly = set(month_ends(sessions, day))
    cache_through = run["replay_end"]
    if day > cache_through:
        if day in monthly:
            raise ValueError("A new month-end requires its own saved completed monthly ranking")
        if (cache_through not in sessions or cache_through not in set(decisions.date)
                or decisions.date.max() != cache_through or ranks.date.gt(cache_through).any()):
            raise ValueError("The saved rank-cache cutoff is incomplete or inconsistent")
        reason = "not_month_end"
        decision_basis = "fixed_calendar_non_month_end_after_saved_rank_cache"
    else:
        if day not in set(decisions.date):
            raise ValueError("Saved monthly ranks or exact decision coverage is incomplete")
        reason = decisions.loc[decisions.date.eq(day), "decision"].item()
        decision_basis = "saved_exact_day_decision"
    if (reason not in {"monthly_ranking", "not_month_end", "252_session_warmup"}
            or (reason != "not_month_end") != (day in monthly)
            or reason != "monthly_ranking" and ranks.date.eq(day).any()
            or _now(run["completed_at"]) < _now(run["replay_end"] + "T15:00:00+08:00")):
        raise ValueError("Saved decision disagrees with the fixed monthly calendar")
    refs = [_ref(file, role) for file, role in ((path, "saved_run"), (folder / "protocol.json", "saved_protocol"),
        (rank_file, "saved_ranks"), (decision_file, "saved_decisions"), (snapshot, "snapshot_manifest"))]
    return {"config": config, "ranks": ranks, "decisions": decisions, "reason": reason,
        "decision_source_basis": decision_basis, "decision_cache_through": cache_through,
        "available_at": _now(run["completed_at"]).isoformat(), "refs": refs,
        "identity": {"variant": VARIANT, "snapshot_manifest": refs[-1], "lookback": 252, "skip": 21,
                     "rank_cadence": "monthly_completed_session_end"}}


def _rank(cache, day):
    config = cache["config"]
    observations = cache["decisions"].loc[cache["decisions"].date.eq(day)]
    if len(observations) != 1 or observations.decision.item() != "monthly_ranking":
        raise ValueError("One complete original monthly ranking is required")
    counts = pd.to_numeric(observations.iloc[0][["eligible_count", "candidate_count"]], errors="raise")
    if (not np.isfinite(counts).all() or counts.lt(0).any() or not counts.eq(counts.astype(int)).all()
            or counts.eligible_count != counts.candidate_count):
        raise ValueError("Original monthly candidate counts changed")
    rows = cache["ranks"].loc[cache["ranks"].date.eq(day)].copy()
    rows["rank"] = pd.to_numeric(rows["rank"], errors="raise")
    rows = rows.sort_values("rank", kind="stable")
    expected = min(config.retain_rank, int(counts.eligible_count))
    if len(rows) != expected or rows["rank"].tolist() != list(range(1, expected + 1)):
        raise ValueError("Complete original rank30 prefix required")
    for field in ("volatility", "momentum", "median_amount"):
        rows[field] = pd.to_numeric(rows[field], errors="raise")
        if not np.isfinite(rows[field]).all():
            raise ValueError("Saved rank contains unknown numeric fields")
    if rows.volatility.le(0).any() or rows.median_amount.lt(config.min_amount).any():
        raise ValueError("Saved rank violates original eligibility")
    for symbol in rows.symbol:
        stock_board(symbol)
    return [{"symbol": row.symbol, "score": -float(row.volatility)} for row in rows.itertuples()]


def _decision(cache, day, sessions, inception, account, first):
    rank_day, bootstrap = None, False
    if cache["reason"] == "monthly_ranking":
        rank_day = day
    elif first and inception == INCEPTIONS[1]:
        earlier = cache["decisions"].loc[cache["decisions"].date.le(day) & cache["decisions"].decision.eq("monthly_ranking"), "date"]
        if earlier.empty:
            raise ValueError("Bootstrap requires an actual saved completed monthly rank")
        rank_day, bootstrap = earlier.max(), True
        if rank_day not in set(month_ends(sessions, day)):
            raise ValueError("Bootstrap ranking date is not a completed fixed-calendar month end")
    if rank_day is None:
        return "NO_CHANGE", None, None, "saved_" + cache["reason"], False
    ranked = _rank(cache, rank_day)
    if not ranked:
        return "NO_CHANGE", None, rank_day, "saved_known_empty_eligible_pool", bootstrap
    held = [symbol for symbol, lots in (account or {}).get("state", {}).get("positions", {}).items()
            if sum(lot["qty"] for lot in lots) > 0]
    return "FULL_TARGET", choose_targets(ranked, held, cache["config"]), rank_day, (
        "new_cash_inception_using_latest_saved_monthly_rank_not_a_new_daily_ranking" if bootstrap else
        "saved_original_monthly_ranking"), bootstrap


def _account(snapshot, day, config):
    if (snapshot.get("method_id") != METHOD_ID or snapshot.get("config") != config.to_dict()
            or snapshot.get("state", {}).get("day") != day or snapshot["state"].get("broker_binding")):
        raise ValueError("Continuation requires this method's exact source-day simulated account")
    context = RunContext.from_dict(snapshot["context"])
    return {"method_id": METHOD_ID, "config": config.to_dict(), "context": context.to_dict(),
            "state": deepcopy(snapshot["state"])}


def publish(saved_run, calendar_path, close_folder, out, *, inception=INCEPTIONS[0],
            account_path=None, previous_publication=None, now=None):
    actual, out = _now(now), Path(out).resolve()
    if inception not in INCEPTIONS or (account_path is None) != (previous_publication is None):
        raise ValueError("Explicit inception and paired continuation account/previous publication required")
    if out.exists():
        raise FileExistsError("Use a new immutable native low-volatility publication directory")
    calendar_ref, sessions = _calendar(calendar_path)
    source = _source(close_folder, calendar_ref, sessions, actual)
    proof, day = source["proof"], source["proof"]["day"]
    if day not in sessions or sessions.index(day) + 1 >= len(sessions):
        raise ValueError("An exact source day and successor session are required")
    execution = sessions[sessions.index(day) + 1]
    if actual >= _now(execution + "T09:30:00+08:00"):
        raise ValueError("Publication must actually precede its exact execution opening")
    cache = _cache(saved_run, day, sessions)
    if _now(cache["available_at"]) > actual or _now(proof["source_available_at"]) > actual:
        raise ValueError("Saved source or rank cache is not yet actually available")
    previous, account, original_head = None, None, None
    if previous_publication is not None:
        previous = load_publication(previous_publication, now=actual)
        account = _account(_read(account_path), day, cache["config"])
        original_head = _ref(account_path)
        if (previous["execution_day"] != day or previous["inception"] != inception
                or previous["calendar_ref"] != calendar_ref or previous["config"] != cache["config"].to_dict()
                or previous["signal_source_identity"] != cache["identity"]):
            raise ValueError("Continuation publication prefix or original signal identity changed")
    kind, weights, rank_day, reason, bootstrap = _decision(cache, day, sessions, inception, account, previous is None)
    refs = list(cache["refs"])
    close_folder = Path(close_folder).resolve()
    refs += [_ref(close_folder / name, "source_" + name) for name in ("raw_daily.csv", "plan.json", "receipt.json")]
    plan = _read(close_folder / "plan.json")
    if plan.get("roster_folder"):
        roster = Path(plan["roster_folder"])
        roster = roster if roster.is_absolute() else close_folder / roster
        refs.append(_ref(roster / "receipt.json", "dated_roster_receipt"))
        refs.append(_ref(roster / f"query_all_stock_{day}.csv", "dated_roster_raw"))
    inputs = [Path(ref["file"]).parent for ref in refs] + [Path(calendar_ref["file"]).parent]
    if account_path:
        inputs.append(Path(account_path).resolve().parent)
    if any(out == parent or out in parent.parents or parent in out.parents for parent in inputs):
        raise ValueError("Publication output must remain separate from source/account directories")
    out.mkdir(parents=True)
    account_ref = None
    if account is not None:
        _write(out / "account_snapshot.json", account)
        account_ref = _ref(out / "account_snapshot.json")
    payload = {"schema": SCHEMA, "method_id": METHOD_ID, "source_day": day, "execution_day": execution,
        "config": cache["config"].to_dict(), "inception": inception, "kind": kind, "weights": weights,
        "rank_day": rank_day, "reason": reason, "bootstrap_applied": bootstrap,
        "decision_source_basis": cache["decision_source_basis"],
        "decision_cache_through": cache["decision_cache_through"],
        "source_proof": proof, "source_refs": refs, "calendar_ref": calendar_ref,
        "signal_source_identity": cache["identity"], "rank_cache_actual_available_at": cache["available_at"],
        "account_ref": account_ref, "source_head_ref": original_head,
        "previous_publication_ref": previous["publication_ref"] if previous else None, **FLAGS}
    _write(out / "payload.json", payload)
    published = _now(now)
    if not _now(day + "T15:00:00+08:00") < published < _now(execution + "T09:30:00+08:00"):
        raise ValueError("Actual final publication clock is outside the source/next-open interval")
    record = {**payload, "published_at": published.isoformat(), "payload_ref": _ref(out / "payload.json")}
    _write(out / "publication.json", record)
    return {**record, "publication_ref": _ref(out / "publication.json"), "account_snapshot": account}


def load_publication(path, now=None):
    actual = _now(now)
    path = Path(path).resolve()
    path = path / "publication.json" if path.is_dir() else path
    record = _read(path)
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("inception") not in INCEPTIONS or any(record.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Saved native low-volatility method or research boundary changed")
    payload_path = _checked(record["payload_ref"])
    if payload_path != path.parent / "payload.json":
        raise ValueError("Publication payload must remain in its own immutable directory")
    payload = _read(payload_path)
    if {k: v for k, v in record.items() if k not in {"published_at", "payload_ref"}} != payload:
        raise ValueError("Final publication changed its already saved payload")
    for ref in record["source_refs"]:
        _checked(ref)
    calendar_ref, sessions = _calendar(_checked(record["calendar_ref"]))
    day, execution = record["source_day"], record["execution_day"]
    if calendar_ref != record["calendar_ref"] or day not in sessions or sessions[sessions.index(day) + 1] != execution:
        raise ValueError("Publication source/execution calendar identity changed")
    published = _now(record["published_at"])
    if (not _now(day + "T15:00:00+08:00") < published < _now(execution + "T09:30:00+08:00")
            or published > actual or _now(record["source_proof"]["source_available_at"]) > published
            or _now(record["rank_cache_actual_available_at"]) > published):
        raise ValueError("Publication has a future, backdated or post-opening clock")
    refs = {ref["role"]: ref for ref in record["source_refs"]}
    proof = record["source_proof"]
    if (proof.get("day") != day or proof.get("calendar_sha256") != calendar_ref["sha256"]
            or proof.get("raw_sha256") != refs["source_raw_daily.csv"]["sha256"]
            or proof.get("plan_sha256") != refs["source_plan.json"]["sha256"]
            or proof.get("receipt_sha256") != refs["source_receipt.json"]["sha256"]
            or Path(proof["source_folder"]).resolve() != Path(refs["source_raw_daily.csv"]["file"]).parent
            or _now(proof["source_available_at"]) < _now(day + "T15:00:00+08:00")):
        raise ValueError("Publication strict own-date source proof binding changed")
    cache = _cache(refs["saved_run"]["file"], day, sessions)
    basis_fields = ("decision_source_basis", "decision_cache_through")
    if not any(key in record for key in basis_fields):
        # Already published v1 files predate these explicit provenance fields.
        # Their original exact-day contract cannot acquire calendar extrapolation.
        if cache["decision_source_basis"] != "saved_exact_day_decision":
            raise ValueError("Legacy publications require their original exact-day saved decision")
    elif any(record.get(key) != cache[key] for key in basis_fields):
        raise ValueError("Publication decision basis or saved cache cutoff changed")
    if (cache["config"].to_dict() != record["config"] or cache["identity"] != record["signal_source_identity"]
            or cache["available_at"] != record["rank_cache_actual_available_at"]
            or any(refs[ref["role"]] != ref for ref in cache["refs"])):
        raise ValueError("Publication config or original signal-source binding changed")
    account = None
    if record["account_ref"] is not None:
        file = _checked(record["account_ref"])
        if file != path.parent / "account_snapshot.json":
            raise ValueError("Account snapshot must remain inside this publication")
        account = _account(_read(file), day, cache["config"])
    previous_ref = record["previous_publication_ref"]
    if (account is None) != (previous_ref is None):
        raise ValueError("Continuation account snapshot and previous publication must be paired")
    if previous_ref is not None:
        previous = load_publication(_checked(previous_ref), now=actual)
        if (previous["execution_day"] != day or previous["calendar_ref"] != calendar_ref
                or previous["inception"] != record["inception"] or previous["config"] != record["config"]
                or previous["signal_source_identity"] != record["signal_source_identity"]
                or _now(previous["published_at"]) >= published):
            raise ValueError("Publication predecessor identity, date or actual clock changed")
    expected = _decision(cache, day, sessions, record["inception"], account, previous_ref is None)
    if expected != (record["kind"], record["weights"], record["rank_day"], record["reason"], record["bootstrap_applied"]):
        raise ValueError("Published target differs from the original monthly cache/account policy")
    return {**record, "publication_ref": _ref(path), "account_snapshot": account}


def bind_preopen(publication, context):
    if not isinstance(context, RunContext):
        raise ValueError("An explicit simulated account context is required")
    loaded = load_publication(publication["publication_ref"]["file"] if isinstance(publication, dict) else publication)
    if loaded["account_snapshot"] is not None and loaded["account_snapshot"]["context"] != context.to_dict():
        raise ValueError("Publication account snapshot differs from this consumer context")
    metadata = {"context": context, "reason": loaded["reason"],
        "source": f"research:{METHOD_ID}:{loaded['publication_ref']['sha256']}"}
    intent = (PortfolioIntent.no_change(**metadata) if loaded["kind"] == "NO_CHANGE" else
              PortfolioIntent.full_target(loaded["weights"], **metadata))
    return PreopenPortfolioIntent(loaded["source_day"], loaded["execution_day"], loaded["published_at"], intent,
                                  explicit_exit_all=loaded["kind"] == "FULL_TARGET" and not loaded["weights"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("saved-run", "calendar", "close", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--inception", choices=INCEPTIONS, default=INCEPTIONS[0])
    parser.add_argument("--account", type=Path)
    parser.add_argument("--previous-publication", type=Path)
    args = parser.parse_args(argv)
    result = publish(args.saved_run, args.calendar, args.close, args.out,
        inception=args.inception, account_path=args.account,
        previous_publication=args.previous_publication)
    print(json.dumps({key: result[key] for key in ("method_id", "source_day", "execution_day",
        "published_at", "inception", "kind", "weights", "rank_day", "reason", "publication_ref")},
        ensure_ascii=False))
    return 0
