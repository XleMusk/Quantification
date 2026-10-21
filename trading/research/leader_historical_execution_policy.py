"""Opt-in whole-native/action checks for historical eight-type research only."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from numbers import Real

import numpy as np
import pandas as pd
from ..symbols import normalize_symbol
from ..contracts import TradingMode, SignalFrame, SignalUnavailable

from .action_events import reference_concordance
from .bulk_feed import AMOUNT_FIELDS
from .leader_daily_replay import CommonCohortDecisionFeed
from .shortline_decision_v2 import SIGNAL_SOURCE
from .order_capacity_replay import PriorVolumeReplayPolicy


METHOD_ID = "historical_eight_type_native_reported_action_execution_v1"


class HistoricalDecisionDayFeed(CommonCohortDecisionFeed):
    """Same saved candidates and selector, without E-row lookahead clearance."""
    def __init__(self, base, batches, state, execution):
        super().__init__(base, batches, state, market_defense=False)
        self.execution = execution

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Historical eight-type decisions are replay-only")
        batch = self.batches.get(day)
        if batch is None or batch.get("status") != "complete":
            return SignalUnavailable("shortline_v2_decision_batch_missing" if batch is None
                else "shortline_v2_decision_batch_incomplete")
        held = {symbol for symbol, lots in self.state["positions"].items() if sum(lot["qty"] for lot in lots)}
        required = held | set(self.state.get("pending_targets") or {}) | {row["symbol"] for row in batch["candidates"]}
        if day not in self.calendar or self.execution.raw_issues(day, required):
            return SignalUnavailable("historical_eight_type_decision_day_raw_unavailable")
        # Exact E data is admitted only on E, by the existing before-mutation
        # quote/IPO/action guard. Missing E must preserve this recorded intent.
        return SignalFrame.from_legacy(batch["candidates"], day, SIGNAL_SOURCE, context)


class HistoricalNativeRiskValidation:
    """Keep all exit decisions; use the declared whole-native mark guard.

    The old raw predicate additionally needs a positive open and nonblank
    volume.  Those are not valuation requirements on a known suspension.
    This adapter is opt-in and does not change expiry or structural decisions,
    including their conservative deferral when exit quotes are unavailable.
    """
    def __init__(self, delegate, execution):
        self.delegate, self.execution = delegate, execution

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def __call__(self, *args, **kwargs):
        return self.delegate(*args, **kwargs)

    def validate_session(self, state, feed, day, context):
        if context.mode != TradingMode.REPLAY or day not in feed.calendar:
            raise ValueError("Historical native risk valuation requires a declared replay session")
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        issues = self.execution.raw_issues(day, held)
        if issues:
            raise ValueError(f"Unpriceable historical native risk-exit holdings: {issues}")


class HistoricalExecutionPolicy:
    def __init__(self, base, accounting, source_days, *, prior_volume_policy=None):
        if prior_volume_policy is not None and not isinstance(prior_volume_policy, PriorVolumeReplayPolicy):
            raise ValueError("Historical per-order capacity requires its explicit typed REPLAY policy")
        self.base, self.accounting = base, deepcopy(accounting)
        self.prior_volume_policy = prior_volume_policy
        self.checks = []
        self.provenance = {"method_id": METHOD_ID, "accounting": deepcopy(accounting.provenance),
            "old_one_percent_reference_proxy_used": False,
            "risk_mark_validation": "explicit_native_status0_blank_quantity_preserved",
            "candidate_clearance": "exact_D_only_E_guard_before_E_account_mutation",
            "each_case_independent_action_checks": True, "returns_reportable": False,
            "pit_verified": False, "execution_admitted": False}
        base.actions = self.accounting.actions
        base.manifest = {**base.manifest, "start": source_days[0], "end": source_days[-1],
            "historical_price_adjustments": self.accounting.special_references,
            "historical_execution_policy": METHOD_ID}
        base.validate_session = self.validate_session
        if prior_volume_policy is not None:
            base.prior_volume_policy = prior_volume_policy
            self.provenance["prior_volume_execution_policy"] = prior_volume_policy.method_id

    def wrap_risk_policy(self, policy):
        return HistoricalNativeRiskValidation(policy, self)

    def decision_feed(self, batches, state):
        return HistoricalDecisionDayFeed(self.base, batches, state, self)

    def raw_issues(self, day, symbols):
        issues = []
        for symbol in sorted(symbols):
            row = self.base.rows.get(day, {}).get(symbol)
            finite = lambda value: isinstance(value, Real) and not isinstance(value, bool) and np.isfinite(value)
            reason = None
            if row is None or row.get("tradestatus") not in (0, 1) or row.get("isST") not in (0, 1):
                reason = "native_row_or_status_unknown"
            elif any(not finite(row.get(field)) or row[field] <= 0 for field in ("close", "preclose")):
                reason = "native_valuation_or_reference_unknown"
            elif not finite(row.get("open")) or row["open"] < 0 or (row["open"] == 0 and row["tradestatus"] != 0):
                reason = "native_open_unknown"
            elif row["tradestatus"] == 1:
                if (any(not finite(row.get(field)) for field in ("high", "low", "volume", "amount"))
                        or row["high"] < max(row["open"], row["close"], row["low"])
                        or row["low"] > min(row["open"], row["close"]) or row["low"] <= 0
                        or row["volume"] < 0 or row["amount"] < 0):
                    reason = "active_native_shape_or_quantity_unknown"
            if reason:
                issues.append({"day": day, "symbol": symbol, "reason": reason})
        return issues

    def references(self, day, symbols):
        results = []
        previous = self.accounting.previous.get(day)
        for symbol in sorted(symbols):
            row = self.base.rows.get(day, {}).get(symbol)
            before = self.base.rows.get(previous, {}).get(symbol)
            evidence = {"symbol": symbol, "day": day, "previous_day_required": previous,
                "reference_method": METHOD_ID, "absence_of_corporate_actions_proven": False}
            try:
                prior = Decimal(str(before["close"]))
                actual = Decimal(str(row["preclose"]))
                if not prior.is_finite() or not actual.is_finite() or min(prior, actual) <= 0:
                    raise InvalidOperation
            except (KeyError, TypeError, ValueError, InvalidOperation):
                evidence["status"] = "exact_native_reference_unknown"
                results.append(evidence)
                continue
            action = next((item for item in self.accounting.actions_by_ex.get(day, []) if item["symbol"] == symbol), None)
            if action is not None or symbol in self.accounting.special_by_ex.get(day, {}):
                gap = {"symbol": symbol, "code": normalize_symbol(symbol).baostock_code,
                    "date": day, "previous_day": previous,
                    "previous_observed_close": str(prior), "preclose": str(actual),
                    "has_reported_distribution": action is not None,
                    **{field: action.get(field, "") if action else "" for field in AMOUNT_FIELDS}}
                try:
                    checked = reference_concordance(pd.DataFrame([gap]),
                        self.accounting.special_references, self.accounting.evidence).iloc[0].to_dict()
                    evidence.update(status=checked["reference_formula_status"], reference_evidence=checked)
                except (ValueError, InvalidOperation) as error:
                    evidence.update(status="invalid_reference_terms", error=str(error))
            else:
                evidence["status"] = ("no_reported_event_reference_continuous"
                    if abs(actual - prior) <= Decimal(".021") else "missing_source_event")
            results.append(evidence)
        self.checks.extend(deepcopy(results))
        return results

    @staticmethod
    def reference_ok(row):
        return row["status"] in {"reference_match", "no_reported_event_reference_continuous"}

    def validate_session(self, state, day):
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        pending = set(state.get("pending_targets") or {})
        required = held | pending
        issues = self.raw_issues(day, required)
        if issues:
            raise ValueError(f"Unknown required native execution quotes: {issues}")
        self.accounting.validate(state, day, self.base.rows)
        residual = (state.get("full_exit_lifecycle") or {}).get("remaining_full_exits")
        if self.prior_volume_policy is None and (state.get("pending_targets") is not None or residual or state.get("pending_risk_exits") is not None
                or any(self.base.rows[day][symbol]["isST"] == 1 for symbol in held)):
            previous = self.accounting.previous.get(day)
            for symbol in required:
                if self.base.rows[day][symbol]["tradestatus"] == 0:
                    continue  # Suspension blanks do not become known zero turnover.
                try:
                    volume = self.base.volumes.at[previous, symbol]
                except KeyError:
                    volume = np.nan
                if not isinstance(volume, Real) or not np.isfinite(volume) or volume < 0:
                    raise ValueError(f"Unknown exact prior native execution capacity: {symbol} {day} previous={previous}")


class HistoricalExecutionFactory:
    def __init__(self, accounting, *, prior_volume_policy=None):
        self.accounting = accounting
        if prior_volume_policy is not None and not isinstance(prior_volume_policy, PriorVolumeReplayPolicy):
            raise ValueError("Historical per-order capacity requires its explicit typed REPLAY policy")
        self.prior_volume_policy = prior_volume_policy
        self.provenance = {"method_id": METHOD_ID, "accounting": deepcopy(accounting.provenance),
            "returns_reportable": False, "execution_admitted": False}
        if prior_volume_policy is not None:
            self.provenance["prior_volume_execution_policy"] = prior_volume_policy.method_id
            self.provenance["prior_volume_context"] = prior_volume_policy.context.to_dict()

    def __call__(self, base, source_days):
        return HistoricalExecutionPolicy(base, self.accounting, source_days,
            prior_volume_policy=self.prior_volume_policy)
