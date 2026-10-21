"""Saved-account exposure/fee attribution, without running a strategy or Ledger.

The cash blend is an after-the-fact mathematical diagnostic.  Its fraction is
not available at historical decision time and is not a lot-sized portfolio.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from .turnover_attribution import daily_exposure


REQUEST_SCHEMA = "leader_environment_attribution_request_v1"
PAIR_SCHEMA = "leader_environment_attribution_pair_v1"
RESULT_SCHEMA = "leader_environment_attribution_saved_results_v1"
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "returns_reportable": False,
    "pit_verified": False, "market_prediction_validated": False,
    "engine_advanced": False, "ledger_mutated": False, "account_created": False,
    "orders_generated": False, "provider_contacted": False, "model_refit": False,
    "original_strategy_metrics_replaced": False, "executable_strategy_claimed": False}
LIMITATIONS = [
    "Closing stock value is saved equity minus cash and unpaid receivables; this is not intraday exposure.",
    "The blend fraction uses the whole saved window's exposure and is only available afterward; it is not a historical trading signal.",
    "The cash blend scales an existing net-equity path mathematically; it does not resize whole-share lots or simulate alternative orders, fills or costs.",
    "Fee add-back removes only explicit saved fees; slippage, fills, stock values and timing remain unchanged.",
    "Resolved blocked/rejected orders remain in source evidence. Eventual liquidation does not prove a timely exit, and those records are not independent market samples.",
    "Observed historical pairs, posthoc listing/identity evidence and source limitations remain; this is neither holdout validation nor proof of market prediction.",
]


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Explicit finite numeric value required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Explicit finite numeric value required") from error
    if not result.is_finite():
        raise ValueError("Explicit finite numeric value required")
    return result


def _cents(value):
    result = _decimal(value) * 100
    if result != result.to_integral_value():
        raise ValueError("Saved daily money must have exact cent precision")
    return int(result)


def _state(case):
    daily = case.get("daily") or []
    blocked = [event for row in daily for event in row.get("events", []) if event.get("kind") == "order_blocked"]
    rejected = [order for order in (case.get("orders") or []) if order.get("status") == "rejected"]
    return {"case_id": case.get("case_id"), "type": case.get("type"),
        "guard": case.get("guard"), "capacity": case.get("capacity"), "status": case.get("status"),
        "complete_PnL_available": case.get("complete_PnL_available"),
        "engine_sessions_committed": case.get("engine_sessions_committed"),
        "blocking_evidence": deepcopy(case.get("blocking_evidence")),
        "residual_positions": deepcopy(case.get("residual_positions")),
        "order_count": len(case.get("orders") or []), "fill_count": len(case.get("fills") or []),
        "saved_metrics": deepcopy(case.get("metrics")),
        "historical_order_blocked_count": len(blocked),
        "historical_order_blocked_reasons": dict(Counter(str(item.get("reason") or "unspecified") for item in blocked)),
        "historical_rejected_order_count": len(rejected),
        "historical_rejected_order_reasons": dict(Counter(str(item.get("reason") or "unspecified") for item in rejected)),
        "stale_valuation_days": sum(bool(row.get("stale_symbols")) for row in daily)}


def _fees(case, days):
    """Saved fills contain fee deltas; saved orders contain cumulative fees."""
    daily = {row["date"]: _cents(row["fees"]) for row in case["daily"]}
    if any(value < 0 for value in daily.values()):
        raise ValueError("Negative saved daily fees")
    orders, fill_by_order, fill_by_day = {}, defaultdict(int), defaultdict(int)
    for row in case["orders"]:
        if (not isinstance(row.get("id"), str) or row["id"] in orders
                or row.get("day") not in days or type(row.get("fees")) is not int or row["fees"] < 0):
            raise ValueError("Unique saved orders with known nonnegative cent fees required")
        orders[row["id"]] = row["fees"]
    for row in case["fills"]:
        if (row.get("order_id") not in orders or row.get("day") not in days
                or type(row.get("fees")) is not int or row["fees"] < 0):
            raise ValueError("Saved fill fee deltas and original order linkage required")
        fill_by_order[row["order_id"]] += row["fees"]
        fill_by_day[row["day"]] += row["fees"]
    if (any(fill_by_order[key] != value for key, value in orders.items())
            or any(fill_by_day[day] != daily[day] for day in days)
            or sum(orders.values()) != sum(daily.values())):
        raise ValueError("Daily, cumulative-order and incremental-fill fees do not agree exactly")
    total = sum(daily.values())
    saved = _decimal(case["metrics"]["fees"])
    if abs(saved - Decimal(total) / 100) > Decimal("0.00000001"):
        raise ValueError("Saved metrics fees disagree with exact daily/order/fill cents")
    return daily, total


def _drawdown(values, initial):
    peak, worst = initial, Decimal(0)
    for value in values:
        if value <= 0:
            raise ValueError("Nonpositive mathematical blend equity")
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return float(worst)


def _execution_source(case):
    """Describe the saved execution contract, including its own source hashes.

    Source-origin labels say whether a case was reused; they do not establish
    equal inputs. Compare the actual saved policy/accounting instead, without
    reopening price data or running an account.
    """
    policy = deepcopy(case.get("historical_execution_policy"))
    if (not isinstance(policy, dict) or not isinstance(policy.get("method_id"), str)
            or not policy["method_id"]):
        return None
    accounting = policy.get("accounting")
    if not isinstance(accounting, dict):
        return None
    for field in ("event_view", "reference_view"):
        if not isinstance(accounting.get(field), str) or not accounting[field]:
            return None
        accounting[field] = str(Path(accounting[field]).resolve())
    refs = accounting.get("refs")
    if not isinstance(refs, list) or not refs:
        return None
    normalized = []
    for ref in refs:
        if (not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not ref["path"]
                or not isinstance(ref.get("sha256"), str) or len(ref["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in ref["sha256"])):
            return None
        normalized.append({**ref, "path": str(Path(ref["path"]).resolve())})
    if len({ref["path"] for ref in normalized}) != len(normalized):
        return None
    required = {str((Path(accounting[field]) / "result.json").resolve())
                for field in ("event_view", "reference_view")}
    if not required <= {ref["path"] for ref in normalized}:
        return None
    accounting["refs"] = sorted(normalized, key=lambda ref: ref["path"])
    # This optional policy changes execution even if its action pair is equal.
    account = case.get("account") or {}
    if not isinstance(account, dict):
        return None
    identity = {"policy": policy,
                "prior_volume_execution_policy": account.get("prior_volume_execution_policy"),
                "full_exit_lifecycle": account.get("full_exit_lifecycle")}
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return {"identity_sha256": sha256(encoded).hexdigest(), "method_id": policy["method_id"],
            "event_view": accounting["event_view"], "reference_view": accounting["reference_view"],
            "saved_reference_count": len(normalized), "saved_source_hashes_compared": True,
            "source_files_reopened": False}


def diagnose_pair(unguarded, guarded, *, require_same_execution_source=False):
    """Attribute one exact saved U/G pair; never change the original metrics."""
    result = {"schema": PAIR_SCHEMA, "status": "not_comparable", "reason_codes": [], "reasons": [],
        "type": unguarded.get("type"), "capacity": unguarded.get("capacity"),
        "source_states": {"unguarded": _state(unguarded), "guarded": _state(guarded)},
        "lambda": None, "comparison": None, "daily": [], "limitations": list(LIMITATIONS), **FLAGS}

    def reason(code, side=None, detail=None):
        result["reasons"].append({"code": code, "side": side, "detail": detail})
        if code not in result["reason_codes"]:
            result["reason_codes"].append(code)

    sources = {"unguarded": _execution_source(unguarded), "guarded": _execution_source(guarded)}
    verified = all(sources.values())
    result["execution_source_comparison"] = {"required": require_same_execution_source,
        "sources": sources, "verified_equal_saved_contract":
            verified and sources["unguarded"]["identity_sha256"] == sources["guarded"]["identity_sha256"]}
    if verified and not result["execution_source_comparison"]["verified_equal_saved_contract"]:
        reason("execution_source_mismatch")
    elif require_same_execution_source and not verified:
        reason("execution_source_unknown")

    if unguarded.get("guard") != "unguarded" or guarded.get("guard") != "two_close_envguard":
        reason("guard_identity_mismatch")
    if (not isinstance(unguarded.get("type"), str) or not unguarded["type"]
            or not isinstance(unguarded.get("capacity"), str) or not unguarded["capacity"]):
        reason("type_or_capacity_identity_missing")
    if any(unguarded.get(key) != guarded.get(key) for key in ("config", "type", "capacity")):
        reason("configuration_type_or_capacity_mismatch")
    by_side = {"unguarded": unguarded, "guarded": guarded}
    day_paths = {}
    for side, case in by_side.items():
        if case.get("complete_PnL_available") is not True or not isinstance(case.get("metrics"), dict):
            reason("incomplete_or_null_saved_metrics", side)
        if not isinstance(case.get("fills"), list) or not case["fills"]:
            reason("no_actual_saved_fills", side)
        if case.get("blocking_evidence") is not None:
            reason("uncommitted_blocking_evidence", side)
        if not isinstance(case.get("residual_positions"), list) or case["residual_positions"]:
            reason("residual_or_unknown_positions", side)
        days = [row.get("date") for row in case.get("daily", [])]
        day_paths[side] = days
        if (not days or any(not isinstance(day, str) for day in days)
                or days != sorted(set(days)) or case.get("engine_sessions_committed") != len(days)):
            reason("unknown_or_nonexact_daily_sessions", side)
        if result["source_states"][side]["stale_valuation_days"]:
            reason("stale_valuation_present", side)
    if day_paths["unguarded"] != day_paths["guarded"]:
        reason("daily_session_paths_mismatch")
    if result["reasons"]:
        return result
    days = day_paths["unguarded"]
    exposures, fee_paths, fee_totals, initial = {}, {}, {}, None
    for side, case in by_side.items():
        try:
            opening = _decimal(case["config"]["initial_cash"])
            if opening <= 0:
                raise ValueError("Positive original initial cash required")
            initial = opening if initial is None else initial
            if opening != initial:
                raise ValueError("Different original cash inception")
            frame, summary = daily_exposure(case["daily"])
            exposures[side] = (frame, summary)
        except (KeyError, TypeError, ValueError) as error:
            reason("exposure_or_initial_cash_unknown", side, str(error))
            continue
        try:
            fee_paths[side], fee_totals[side] = _fees(case, days)
        except (KeyError, TypeError, ValueError) as error:
            reason("explicit_fee_accounting_unknown_or_conflicting", side, str(error))
        try:
            saved = case["metrics"]
            terminal = _cents(case["daily"][-1]["equity"])
            if (terminal != _cents(saved["final_equity"])
                    or abs(_decimal(saved["net_return"]) - (Decimal(terminal) / 100 / opening - 1)) > Decimal("0.000000000001")
                    or not -1 <= _decimal(saved["max_drawdown"]) <= 0):
                raise ValueError("Saved terminal metrics disagree with the same saved equity path")
        except (KeyError, TypeError, ValueError) as error:
            reason("saved_terminal_metrics_unknown_or_conflicting", side, str(error))
    if result["reasons"]:
        return result
    sums = {side: sum(_cents(value) for value in exposures[side][0].invested_value_cny) for side in by_side}
    if sums["unguarded"] <= 0:
        reason("unguarded_positive_closing_stock_exposure_missing")
        return result
    fraction = Decimal(sums["guarded"]) / Decimal(sums["unguarded"])
    if not 0 <= fraction <= 1:
        reason("lambda_outside_cash_blend_range", detail={"numerator": sums["guarded"], "denominator": sums["unguarded"]})
        return result
    cumulative = {side: 0 for side in by_side}
    curves = {key: [] for key in ("guarded_net", "matched_net", "guarded_fee_added_back", "matched_fee_added_back")}
    for index, day in enumerate(days):
        for side in by_side:
            cumulative[side] += fee_paths[side][day]
        eu = _decimal(unguarded["daily"][index]["equity"])
        eg = _decimal(guarded["daily"][index]["equity"])
        matched = initial + fraction * (eu - initial)
        gross_g = eg + Decimal(cumulative["guarded"]) / 100
        gross_u = initial + fraction * (eu + Decimal(cumulative["unguarded"]) / 100 - initial)
        for key, value in (("guarded_net", eg), ("matched_net", matched),
                ("guarded_fee_added_back", gross_g), ("matched_fee_added_back", gross_u)):
            curves[key].append(value)
        result["daily"].append({"day": day, "unguarded_equity_cny": float(eu), "guarded_equity_cny": float(eg),
            "unguarded_closing_stock_value_cny": float(exposures["unguarded"][0].iloc[index].invested_value_cny),
            "guarded_closing_stock_value_cny": float(exposures["guarded"][0].iloc[index].invested_value_cny),
            "matched_unguarded_equity_cny": float(matched),
            "guarded_fee_added_back_equity_cny": float(gross_g), "matched_fee_added_back_equity_cny": float(gross_u),
            "unguarded_cumulative_fees_cny": cumulative["unguarded"] / 100,
            "guarded_cumulative_fees_cny": cumulative["guarded"] / 100})
    u_return, g_return = (_decimal(case["metrics"]["net_return"]) for case in (unguarded, guarded))
    fee_saving = (fraction * Decimal(fee_totals["unguarded"]) - Decimal(fee_totals["guarded"])) / 100
    net_advantage = g_return - fraction * u_return
    gross_advantage = net_advantage - fee_saving / initial
    result.update(status="comparable_posthoc_math_diagnostic", **{"lambda": float(fraction)},
        comparison={"initial_cash_cny": float(initial), "sessions": len(days),
            "unguarded_saved_net_return": float(u_return), "guarded_saved_net_return": float(g_return),
            "matched_unguarded_net_return": float(fraction * u_return),
            "net_advantage_return": float(net_advantage), "gross_advantage_return": float(gross_advantage),
            "net_advantage_cny": float(net_advantage * initial), "gross_advantage_cny": float(gross_advantage * initial),
            "fee_saving_cny": float(fee_saving), "fee_add_back_semantics": "explicit_fees_only_slippage_and_saved_market_value_paths_unchanged",
            "fees": {"unguarded_cents": fee_totals["unguarded"], "guarded_cents": fee_totals["guarded"],
                "matched_unguarded_cny": float(fraction * Decimal(fee_totals["unguarded"]) / 100),
                "three_saved_accounting_totals_match_exactly": True},
            "exposure": {"unguarded": exposures["unguarded"][1], "guarded": exposures["guarded"][1],
                "unguarded_stock_value_cny_sessions": sums["unguarded"] / 100,
                "guarded_stock_value_cny_sessions": sums["guarded"] / 100,
                "matched_unguarded_stock_value_cny_sessions": float(fraction * sums["unguarded"] / 100)},
            "dd": {"unguarded_saved_max_drawdown": unguarded["metrics"]["max_drawdown"],
                "guarded_saved_max_drawdown": guarded["metrics"]["max_drawdown"],
                "matched_unguarded_max_drawdown": _drawdown(curves["matched_net"], initial),
                "guarded_fee_added_back_max_drawdown": _drawdown(curves["guarded_fee_added_back"], initial),
                "matched_fee_added_back_max_drawdown": _drawdown(curves["matched_fee_added_back"], initial)}})
    return result


def run(request, out):
    """Read each declared saved result once and retain every requested pair."""
    request, out = Path(request).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Environment attribution requires a new output directory")
    request_bytes = request.read_bytes()
    value = json.loads(request_bytes.decode("utf-8-sig"))
    require_same_source = value.get("require_same_execution_source", False)
    if type(require_same_source) is not bool:
        raise ValueError("Explicit boolean execution-source comparison required")
    pairs = value.get("pairs", [])
    if (value.get("schema") != REQUEST_SCHEMA or not pairs
            or len({pair.get("pair_id") for pair in pairs}) != len(pairs)
            or any(not isinstance(pair.get("pair_id"), str) or not pair["pair_id"] for pair in pairs)):
        raise ValueError("Explicit unique saved-result pairs required")
    paths = {Path(pair[side]["result"]).resolve() for pair in pairs for side in ("unguarded", "guarded")}
    if out.is_relative_to(request.parent) or any(out.is_relative_to(path.parent) for path in paths):
        raise ValueError("New attribution output must be outside its protected input directories")
    declared = value.get("source_refs")
    expected = None
    if declared is not None:
        expected = {Path(ref["path"]).resolve(): ref["sha256"] for ref in declared}
        if len(expected) != len(declared) or set(expected) != paths:
            raise ValueError("Declared source SHA refs must cover every pair result path exactly once")
    saved, refs = {}, []
    for path in sorted(paths, key=str):
        payload = path.read_bytes()
        actual = sha256(payload).hexdigest()
        if expected is not None and actual != expected[path]:
            raise ValueError("A declared saved-result source changed since the request")
        record = json.loads(payload.decode("utf-8-sig"))
        cases = record.get("cases", [])
        if len({case["case_id"] for case in cases}) != len(cases):
            raise ValueError("Duplicate case identity in a saved result")
        saved[path] = {case["case_id"]: case for case in cases}
        refs.append({"path": str(path), "sha256": actual})
    diagnostics = []
    for pair in pairs:
        cases = [saved[Path(pair[side]["result"]).resolve()][pair[side]["case_id"]]
            for side in ("unguarded", "guarded")]
        item = diagnose_pair(*cases, require_same_execution_source=require_same_source)
        item.update(pair_id=pair["pair_id"], source_pair=deepcopy(pair))
        diagnostics.append(item)
    result = {"schema": RESULT_SCHEMA, "built_at": datetime.now(MARKET_TZ).isoformat(),
        "request": {"path": str(request), "sha256": sha256(request_bytes).hexdigest(), "record": value},
        "source_refs": refs, "source_refs_verified_once": True, "pair_count": len(diagnostics),
        "comparable_count": sum(item["comparison"] is not None for item in diagnostics),
        "not_comparable_count": sum(item["comparison"] is None for item in diagnostics),
        "reason_counts": dict(Counter(code for item in diagnostics for code in item["reason_codes"])),
        "pairs": diagnostics, "limitations": list(LIMITATIONS), **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = ["pair_id", "type", "capacity", "status", "reason_codes", "lambda",
        "unguarded_saved_net_return", "guarded_saved_net_return", "matched_unguarded_net_return",
        "net_advantage_return", "gross_advantage_return", "fee_saving_cny",
        "unguarded_order_blocked_count", "guarded_order_blocked_count"]
    with (out / "pairs.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for item in diagnostics:
            comparison = item["comparison"] or {}
            writer.writerow({**{key: item[key] for key in ("pair_id", "type", "capacity", "status", "lambda")},
                "reason_codes": "|".join(item["reason_codes"]),
                **{key: comparison.get(key) for key in fields[6:12]},
                "unguarded_order_blocked_count": item["source_states"]["unguarded"]["historical_order_blocked_count"],
                "guarded_order_blocked_count": item["source_states"]["guarded"]["historical_order_blocked_count"]})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.request, args.out)
    print(json.dumps({"result": str(args.out.resolve() / "result.json"),
        "csv": str(args.out.resolve() / "pairs.csv"), "pair_count": result["pair_count"],
        "comparable_count": result["comparable_count"], "not_comparable_count": result["not_comparable_count"],
        **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
