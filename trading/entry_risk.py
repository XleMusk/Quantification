"""Opt-in, versioned raw-quote entry restriction for ordinary SH/SZ stocks."""
from decimal import Decimal, InvalidOperation
from .notice_risk import NOTICE_BLOCK_REASON, NoticeRiskView


RAW_PRICE_FLOOR1 = "ordinary_sh_sz_raw_price_floor1_v1"
RAW_FLOOR1_AND_NOTICES = "ordinary_sh_sz_raw_floor1_and_severe_notices_v1"
BLOCK_REASON = "ordinary_stock_raw_price_below_entry_floor"


def entry_risk_contract(policy, notice_view=None):
    if policy is None:
        if notice_view is not None:
            raise ValueError("Notice view requires its explicit entry-risk policy")
        return None
    if policy not in {RAW_PRICE_FLOOR1, RAW_FLOOR1_AND_NOTICES}:
        raise ValueError("Unknown fixed entry-risk policy")
    if ((policy == RAW_FLOOR1_AND_NOTICES and not isinstance(notice_view, NoticeRiskView))
            or (policy == RAW_PRICE_FLOOR1 and notice_view is not None)):
        raise ValueError("Notice entry-risk policy requires exactly its pinned primary view")
    contract = {"id": policy, "minimum_raw_rmb": "1.00",
            "scope": "Supported RMB ordinary SH/SZ stocks only; no ETF/B-share/CDR/BSE inference",
            "rule": "Block buys/increases when observable raw open OR preclose reference is below1CNY; exactly1 allowed. No same-session close or future outDate input",
            "allocation": "Unchanged ranks/targets; blocked buys leave cash, no replacement or weight redistribution",
            "exits": "Sells unchanged. No forced sale, settlement or removal of existing holdings",
            "limits": "Conservative price-only entry restriction, not a legal delisting classifier or protection against later price falls/other delisting causes"}
    if notice_view is not None:
        contract.update(notice_view=notice_view.binding, mode="historical_replay_only",
            notice_rule="Also block buys/adds for explicit active severe-risk notices; no same-day publication use, automatic clearance or ranking replacement",
            limits="Partial post-incident primary-notice coverage; absent events mean unknown. Price/notice restrictions cannot guarantee exits or prevent other delisting causes")
    return contract


def validate_entry_risk_binding(state, contract):
    saved = state.get("entry_risk_policy")
    if saved != contract and (state.get("day") is not None or saved is not None
                              or state.get("orders") or state.get("fills") or state.get("positions")):
        raise ValueError("Entry-risk policy changed on an existing account; fresh replay required")


def raw_quote_entry_block(policy, side, quote, rule, notice_view=None):
    contract = entry_risk_contract(policy, notice_view)
    if contract is None or side != "buy" or rule.is_fund:
        return None
    if rule.price_unit != "cent" or rule.board not in {"main", "chinext", "star"}:
        raise ValueError("Entry-risk policy requires a supported ordinary stock rule")
    if str(quote.get("adjustflag")) != "3":
        raise ValueError("Entry-risk policy needs explicit raw quote semantics")
    values = {}
    for field in ("open", "preclose"):
        try:
            value = Decimal(str(quote[field]))
        except (KeyError, InvalidOperation, ValueError) as error:
            raise ValueError("Entry-risk policy needs finite positive raw quotes") from error
        if not value.is_finite() or value <= 0:
            raise ValueError("Entry-risk policy needs finite positive raw quotes")
        values[field] = value
    if min(values.values()) < Decimal(contract["minimum_raw_rmb"]):
        return {"reason": BLOCK_REASON, "entry_risk_policy": contract["id"],
                "minimum_raw_rmb": contract["minimum_raw_rmb"],
                "raw_open_rmb": str(values["open"]), "raw_preclose_rmb": str(values["preclose"])}
    return None


def notice_entry_block(policy, notice_view, symbol, day, side, rule):
    contract = entry_risk_contract(policy, notice_view)
    if contract is None or notice_view is None or side != "buy" or rule.is_fund:
        return None
    event = notice_view.active_event(symbol, day)
    if event is None:
        return None
    return {"reason": NOTICE_BLOCK_REASON, "entry_risk_policy": policy,
            "risk_event_id": event["id"], "publication_on": event["publication_on"],
            "known_at_proxy": event["known_at_proxy"], "risk_source_url": event["source_url"],
            "risk_coverage": "partial_positive_only_others_unknown"}
