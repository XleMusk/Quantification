"""Current EOD scores to the existing paused simulated broker's next plan."""
from copy import deepcopy
import csv
from datetime import date, datetime
import json
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, PortfolioIntent, RunContext, SignalFrame, SignalKind, TradingMode, decision_time
from ..engine import choose_targets
from ..portfolio_allocation import OBSERVED_LOT_ALLOCATION
from ..store import connection
from . import journal
from .adapter import fresh, now, validate_snapshot
from .ledger_import import pinned_calendar, positions_of, run_book, sessions_of
from .weights import from_weights


CURRENT_SCORE_BUNDLE_SCHEMA = "ths_current_eod_score_bundle_v1"


def validate_current_score_bundle(bundle, *, current=None):
    """Validate provenance for a score batch before it enters the paper preview."""
    if not isinstance(bundle, dict) or bundle.get("schema") != CURRENT_SCORE_BUNDLE_SCHEMA:
        raise ValueError("Current score bundle schema is missing or unsupported")
    source = bundle.get("source")
    if not isinstance(source, dict):
        raise ValueError("Current score bundle needs a source receipt")
    if (not isinstance(source.get("source_id"), str) or not source["source_id"].strip()
            or source.get("kind") != "current_eod"
            or source.get("status") != "validated_current_eod"
            or source.get("execution_admitted") is not False):
        raise ValueError("Current score source must be an explicitly validated, unadmitted EOD receipt")
    if source.get("decision_day") != bundle.get("day"):
        raise ValueError("Current score source decision day does not match the score batch")
    if not isinstance(source.get("observed_at"), str):
        raise ValueError("Current score source needs an observed_at timestamp")
    observed = datetime.fromisoformat(source["observed_at"])
    current = current or now()
    if observed.tzinfo is None or observed > current:
        raise ValueError("Current score source observation is missing timezone or is in the future")
    try:
        decision_day = date.fromisoformat(bundle["day"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Current score bundle needs an ISO decision day")
    if observed.astimezone(MARKET_TZ).date() < decision_day:
        raise ValueError("Current score source observation predates its decision day")
    if any(token in source["source_id"].lower() for token in ("research:", "historical:")):
        raise ValueError("Historical research source cannot become a current score bundle")
    return deepcopy(source)


def build_current_request(frame, prices, account_alias, *, source_receipt=None, generated_at=None, current=None):
    """Serialize a validated current EOD score batch for the manual plan consumer.

    This is deliberately account-free: the bound-run consumer still performs the
    pinned-calendar, account-observation, holdings, quote and ledger checks.
    """
    current = current or now()
    if not isinstance(frame, SignalFrame):
        raise TypeError("Current signal input must be a SignalFrame")
    if not isinstance(account_alias, str) or not account_alias.strip():
        raise ValueError("A nonempty simulated account alias is required")
    if frame.context.mode is not TradingMode.PAPER:
        raise ValueError("Current signals must use an end-of-day paper context")
    if frame.source.startswith(("research:", "historical:")):
        raise ValueError("Historical research sources cannot become current broker signals")
    if any(signal.kind is not SignalKind.SCORE for signal in frame.signals):
        raise ValueError("Current signal requests require ranking scores")
    if any(signal.time.availability_evidence != "provided" for signal in frame.signals):
        raise ValueError("Historical EOD availability assumptions cannot become current broker signals")
    today = current.astimezone(MARKET_TZ).date().isoformat()
    if frame.day >= today:
        raise ValueError("Current signal batches must belong to a prior session")
    if not isinstance(prices, list) or any(not isinstance(item, dict) for item in prices):
        raise ValueError("Current signal requests require a list of explicit raw-price inputs")
    for item in prices:
        if item.get("price_basis") != "raw_rmb" or not item.get("symbol"):
            raise ValueError("Each current signal price must declare raw_rmb and a symbol")
        if "limit_price_rmb" not in item:
            raise ValueError("Each current signal price needs an explicit limit_price_rmb")
    generated_at = generated_at or current.isoformat()
    fresh(generated_at, current)
    if source_receipt is not None:
        try:
            source_observed = datetime.fromisoformat(source_receipt["observed_at"])
            generated = datetime.fromisoformat(generated_at)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Current score source needs an ISO observed_at timestamp")
        if source_observed.tzinfo is None or generated.tzinfo is None or source_observed > generated:
            raise ValueError("Current score batch was generated before its source was observed")
    return {
        "mode": "current_preview",
        "account_alias": account_alias,
        "strategy_id": frame.source,
        "generated_at": generated_at,
        "signal_frame": frame.to_dict(),
        "prices": deepcopy(prices),
        "producer": "ths_current_signal_builder_v1",
        **({"source_receipt": deepcopy(source_receipt)} if source_receipt is not None else {}),
    }


def build_current_request_from_scores(rows, day, strategy_id, run_id, prices, account_alias,
                                      *, source_receipt=None, generated_at=None, current=None):
    """Build the same request from a strategy's explicit score rows.

    Every row must carry its own ``available_at`` timestamp. Omitting it would
    silently turn a historical end-of-day assumption into a current signal.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("Current score input must be a nonempty list")
    candidates, available_at = [], []
    for original in rows:
        if not isinstance(original, dict):
            raise ValueError("Current score rows must be objects")
        row = deepcopy(original)
        stamp = row.pop("available_at", None)
        if not stamp:
            raise ValueError("Each current score row needs an explicit available_at timestamp")
        available_at.append(stamp)
        candidates.append(row)
    if any(left.get("score") < right.get("score") for left, right in zip(candidates, candidates[1:])):
        raise ValueError("Current score rows must be ranked in descending order")
    frame = SignalFrame.from_legacy(candidates, day, strategy_id,
                                    RunContext(run_id, TradingMode.PAPER), available_at=available_at)
    if source_receipt is not None:
        source_receipt = validate_current_score_bundle(
            {"schema": CURRENT_SCORE_BUNDLE_SCHEMA, "source": source_receipt, "day": day}, current=current)
    return build_current_request(frame, prices, account_alias, source_receipt=source_receipt,
                                 generated_at=generated_at, current=current)


def build_current_request_from_bundle(bundle, *, current=None):
    """Consume a versioned current-source handoff, never a research export."""
    source = validate_current_score_bundle(bundle, current=current)
    for key in ("day", "strategy", "run_id", "scores", "prices", "account"):
        if key not in bundle:
            raise ValueError(f"Current score bundle needs {key}")
    return build_current_request_from_scores(
        bundle["scores"], bundle["day"], bundle["strategy"], bundle["run_id"],
        bundle["prices"], bundle["account"], source_receipt=source,
        generated_at=bundle.get("generated_at"), current=current)


def require_matching_account(book, snapshot, day):
    held = {s for s in book.state["positions"] if book.quantity(s)}
    if (snapshot.get("cash_total") != book.state["cash"]
            or snapshot.get("cash_frozen") != book.state.get("broker_cash_frozen", 0)
            or positions_of(snapshot) != {s: {"quantity": book.quantity(s), "sellable": book.available(s, day)} for s in held}):
        raise ValueError("Latest broker cash/positions differ from the sole ledger; reconcile/import first")
    return held


def check_signal_plan(db, plan):
    """Recheck the reviewed selector and observation inside the plan-write transaction."""
    metadata = plan["strategy_signal"]
    row, book = run_book(db, metadata["run_id"])
    frame = SignalFrame.from_dict(metadata["signal_frame"])
    if "source_receipt" in metadata:
        validate_current_score_bundle({"schema": CURRENT_SCORE_BUNDLE_SCHEMA,
                                       "source": metadata["source_receipt"], "day": frame.day})
    binding = book.state.get("broker_binding", {})
    policy = binding.get("lot_allocation")
    if (frame.context != book.context or book.config.to_dict() != metadata["selector_config"]
            or book.config.to_dict() != plan.get("execution_config", StrategyConfig().to_dict())
            or binding.get("account_alias") != plan["account_alias"]
            or binding.get("channel") != "ths" or binding.get("broker_account_kind") != "simulated"):
        raise ValueError("Bound signal account/selector changed since preview; regenerate the plan")
    if (policy not in (None, OBSERVED_LOT_ALLOCATION)
            or any(value.get("lot_allocation") != policy for value in (
                metadata, plan.get("weight_portfolio", {}), plan.get("weight_sizing", {})))):
        raise ValueError("Bound signal lot allocation changed since preview; regenerate the plan")
    day = metadata["execution_day"]
    pinned_calendar(row, binding, day)
    if journal._latest(db, plan["account_alias"], "account") != plan["snapshot"]:
        raise ValueError("Broker observation changed since signal preview; regenerate the plan")
    require_matching_account(book, plan["snapshot"], day)


def from_signal(run_id, request, *, current=None):
    current = current or now()
    if request.get("mode") != "current_preview":
        raise ValueError("Only reviewed current-preview signal requests are accepted")
    fresh(request["generated_at"], current)
    frame = SignalFrame.from_dict(request["signal_frame"])
    if "source_receipt" in request:
        validate_current_score_bundle({"schema": CURRENT_SCORE_BUNDLE_SCHEMA,
                                       "source": request["source_receipt"], "day": frame.day}, current=current)
    if request.get("strategy_id") != frame.source or frame.source.startswith(("research:", "historical:")):
        raise ValueError("Signal strategy/source mismatch or historical research source")
    if any(s.time.availability_evidence != "provided" for s in frame.signals):
        raise ValueError("Historical EOD availability assumptions cannot become current broker signals")

    with connection() as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            raise PermissionError("Signal plans require an existing paused bound paper run")
        row, book = run_book(db, run_id)
        binding = book.state.get("broker_binding", {})
        if (binding.get("channel") != "ths" or binding.get("broker_account_kind") != "simulated"
                or binding.get("account_alias") != request.get("account_alias")
                or frame.context != book.context):
            raise PermissionError("Signal context must match this bound simulated account/run")
        alias = binding["account_alias"]
        policy = binding.get("lot_allocation")
        if (policy not in (None, OBSERVED_LOT_ALLOCATION)
                or ("lot_allocation" in request and request["lot_allocation"] != policy)):
            raise ValueError("Signal lot allocation must match the immutable broker binding")
        snapshot = journal._latest(db, alias, "account")
        validate_snapshot(snapshot, current)
        day = current.astimezone(MARKET_TZ).date().isoformat()
        calendar = pinned_calendar(row, binding, day)
        source_calendar = pinned_calendar(row, binding, frame.day)
        session = sessions_of(book.state).get(day)
        if (book.state["day"] != day or not session or session["calendar"] != calendar
                or (row["error"] or "").startswith("broker_session:")
                or datetime.fromisoformat(snapshot["observed_at"]) < datetime.fromisoformat(session["observed_at"])):
            raise ValueError("Reconcile the bound account's current session before generating a signal plan")
        if (source_calendar["available_day"] != day
                or not row["start"] <= frame.day < day <= row["end"]):
            raise ValueError("EOD scores must belong to the immediately preceding pinned exchange session")
        held = require_matching_account(book, snapshot, day)
        config = book.config.validate()
        manifest = json.loads((Path(row["dataset"]) / "manifest.json").read_text(encoding="utf-8"))
        with (Path(manifest.get("folder", row["dataset"])) / "calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
            days = [item["date"] for item in csv.DictReader(stream)]
        phase = days.index(frame.day) - days.index(row["start"])

    metadata = {"run_id": run_id, "signal_frame": frame.to_dict(), "selector_config": config.to_dict(),
                "signal_session_number": phase, "execution_day": day}
    if "source_receipt" in request:
        metadata["source_receipt"] = deepcopy(request["source_receipt"])
    if policy is not None:
        metadata["lot_allocation"] = policy
    if phase % config.rebalance_sessions:
        intent = PortfolioIntent.no_change("not_rebalance_session", context=frame.context,
                                           decided_at=decision_time(frame.day), source=frame.source)
        return {"account_alias": alias, "strategy_id": frame.source, "status": "no_change",
                "reason": intent.reason, "portfolio_intent": intent.to_dict(), "strategy_signal": metadata,
                "next_plan": None, "saved": False, "client_interacted": False,
                "execution_admitted": False, "submission": "manual_only"}

    candidates = frame.ranked_candidates()
    if any(left["score"] < right["score"] for left, right in zip(candidates, candidates[1:])):
        raise ValueError("Signal scores must be ranked in descending order, as in the shared engine")
    targets = choose_targets(candidates, sorted(held), config)
    intent = PortfolioIntent.full_target(targets, context=frame.context, decided_at=decision_time(frame.day),
                                         execution_not_before=day, source=frame.source)
    prices = request.get("prices")
    if not isinstance(prices, list) or any(not isinstance(item, dict) for item in prices):
        raise ValueError("Signal plans require explicit raw-price inputs for the full portfolio")
    price_map = {item["symbol"]: item for item in prices}
    if len(price_map) != len(prices) or set(price_map) != held | set(targets):
        raise ValueError("Price inputs must exactly cover selected and held securities without duplicates")
    ordered_symbols = list(targets) + sorted(held - set(targets)) if policy is not None else sorted(price_map)
    weights = {"mode": request["mode"], "strategy_id": frame.source, "account_alias": alias,
               "generated_at": request["generated_at"], "weight_unit": "equity_fraction",
               "targets": [{**price_map[s], "symbol": s, "target_weight": targets.get(s, 0)}
                           for s in ordered_symbols]}
    if policy is not None:
        weights["lot_allocation"] = policy
    result = from_weights(snapshot, weights, current=current, config=config)
    result.update(portfolio_intent=intent.to_dict(), strategy_signal=metadata)
    if result["next_plan"] is not None:
        result["next_plan"]["strategy_signal"] = deepcopy(metadata)
        result["next_plan"]["portfolio_intent"] = intent.to_dict()
    return result
