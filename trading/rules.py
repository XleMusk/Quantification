from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re


def stock_board(symbol):
    if re.fullmatch(r"(?:60[0-9]{4}\.SSE|00[0-9]{4}\.SZSE)", symbol):
        return "main"
    if re.fullmatch(r"30[0-9]{4}\.SZSE", symbol):
        return "chinext"
    if re.fullmatch(r"688[0-9]{3}\.SSE", symbol):
        return "star"
    raise ValueError(f"Unsupported SH/SZ ordinary equity: {symbol}")


@dataclass(frozen=True)
class StockQuantityRule:
    minimum: int
    step: int
    maximum: int

    def rounded(self, quantity, side, held=0):
        quantity = min(max(0, int(quantity)), self.maximum)
        if side == "sell" and quantity == held:
            return quantity
        quantity = quantity // self.step * self.step
        return quantity if quantity >= self.minimum else 0

    def rejection(self, quantity, side, held=0):
        if quantity > self.maximum:
            return "quantity_above_board_order_limit"
        if self.step == 100:
            if side == "buy" and quantity % 100:
                return "buy_lot_must_be_100"
            if side == "sell" and quantity % 100 and quantity != held:
                return "odd_lot_must_be_full_exit"
        elif quantity < self.minimum and not (side == "sell" and quantity == held):
            return "buy_minimum_200" if side == "buy" else "sell_minimum_200_or_full_exit"
        return None


_QUANTITY_RULES = {"main": StockQuantityRule(100, 100, 1_000_000),
                   "chinext": StockQuantityRule(100, 100, 300_000),
                   "star": StockQuantityRule(200, 1, 100_000)}


def stock_quantity_rule(symbol):
    return _QUANTITY_RULES[stock_board(symbol)]


ETF_PROFILE = "sse_510300_secondary_T1_2024_20260930"
EQUITY_ETF_PROFILES = {code + ".SSE": f"sse_{code}_secondary_T1_2024_2025"
                       for code in ("510050", "510500", "510880", "512100", "588000")}
EQUITY_ETF_PROFILES["510300.SSE"] = ETF_PROFILE
T0_ETF_PROFILES = {code + ".SSE": f"sse_{code}_secondary_T0_2024_2025"
                   for code in ("511010", "518880")}
FUND_PROFILES = {**EQUITY_ETF_PROFILES, **T0_ETF_PROFILES}
FUND_H1_2026_PROFILES = {
    symbol: (ETF_PROFILE if symbol == "510300.SSE" else
             f"sse_{symbol[:6]}_secondary_{'T0' if symbol in T0_ETF_PROFILES else 'T1'}_2024_20260630")
    for symbol in FUND_PROFILES}
FUND_Q3_2026_PROFILES = {
    symbol: f"sse_{symbol[:6]}_secondary_{'T0' if symbol in T0_ETF_PROFILES else 'T1'}_2024_20260930_two_rule_eras"
    for symbol in FUND_PROFILES}
_FUND_PROFILE_ENDS = {
    **{(symbol, profile): "2026-09-30" if symbol == "510300.SSE" else "2025-12-31"
       for symbol, profile in FUND_PROFILES.items()},
    **{(symbol, profile): "2026-06-30" for symbol, profile in FUND_H1_2026_PROFILES.items()
       if symbol != "510300.SSE"},
    **{(symbol, profile): "2026-09-30" for symbol, profile in FUND_Q3_2026_PROFILES.items()}}


@dataclass(frozen=True)
class ExecutionRule:
    price_unit: str
    quantity: StockQuantityRule
    board: str | None
    fund_type: str | None = None
    fund_limit: Decimal = Decimal("0.10")
    same_day_resale: bool = False

    @property
    def equity_etf(self):
        return self.fund_type == "equity_etf"

    @property
    def is_fund(self):
        return self.fund_type is not None

    def limits(self, preclose, day, is_st=False):
        if not self.is_fund:
            return price_limits(preclose, day, is_st, self.board)
        reference = quote_value(preclose, "mill")
        return (max(1, min(reference - 1, quote_value(Decimal(str(preclose)) * (1 - self.fund_limit), "mill"))),
                max(reference + 1, quote_value(Decimal(str(preclose)) * (1 + self.fund_limit), "mill")))


def execution_rule(symbol, day, profiles=None):
    profile = (profiles or {}).get(symbol)
    if profile is not None:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("Instrument profile needs a canonical session date")
        if not isinstance(profile, str):
            raise ValueError("Unverified instrument profile or date outside reviewed rule span")
        last = _FUND_PROFILE_ENDS.get((symbol, profile))
        if last is None or not "2024-01-01" <= day <= last:
            raise ValueError("Unverified instrument profile or date outside reviewed rule span")
        if symbol in T0_ETF_PROFILES:
            return ExecutionRule("mill", _QUANTITY_RULES["main"], None,
                                 "bond_etf" if symbol == "511010.SSE" else "gold_etf",
                                 same_day_resale=True)
        # STAR equity ETF keeps 100-unit orders, not the STAR stock's 200-share rule.
        return ExecutionRule("mill", _QUANTITY_RULES["main"], None, "equity_etf",
                             Decimal("0.20") if symbol == "588000.SSE" else Decimal("0.10"))
    return ExecutionRule("cent", stock_quantity_rule(symbol), stock_board(symbol))


def listing_limit_reason(ipo_date, day, calendar):
    if not ipo_date:
        return "missing_listing_date"
    listed, session = date.fromisoformat(ipo_date), date.fromisoformat(day)
    if session < listed:
        return "not_yet_listed"
    if ipo_date >= calendar[0]:
        if bisect_right(calendar, day) - bisect_left(calendar, ipo_date) <= 5:
            return "unsupported_first_five_listing_sessions"
    # A truncated calendar cannot establish the first five sessions of a recent IPO.
    elif (session - listed).days < 365 and bisect_right(calendar, day) <= 5:
        return "listing_calendar_incomplete"
    return None


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quote_value(value, unit="cent"):
    if unit == "cent":
        return cents(value)
    if unit != "mill":
        raise ValueError("Unknown CNY quote unit")
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def notional_cents(price, quantity, unit="cent"):
    if unit == "cent":
        return price * quantity
    if unit != "mill":
        raise ValueError("Unknown CNY quote unit")
    return int((Decimal(price * quantity) / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def restructuring_ex_price(preclose, item):
    """Reference price only: no cash or shares distributed to ordinary holders."""
    shares = [Decimal(item[k]) for k in ("shares_before", "debt_shares", "investor_shares", "holder_shares")]
    amounts = [Decimal(item[k]) for k in ("debt_cash_equivalent", "investor_cash", "cash_dividend")]
    donation = Decimal(item.get("investor_donation_cash", "") or "0")
    compensation = Decimal(item.get("investor_compensation_cash", "") or "0")
    if (not all(v.is_finite() and v >= 0 for v in shares + amounts)
            or not all(v.is_finite() and v >= 0 for v in (donation, compensation))
            or any(v != int(v) for v in shares) or shares[0] <= 0
            or shares[1] + shares[2] <= 0 or shares[3] != 0 or amounts[2] != 0
            or amounts[0] + amounts[1] + donation + compensation <= 0):
        raise ValueError("Only no-holder-entitlement restructuring ex-prices are supported")
    base_text = item.get("reference_share_base", "")
    basis = item.get("reference_share_basis", "")
    base = Decimal(base_text) if base_text else shares[0]
    if (bool(base_text) != bool(basis) or (basis and basis != "unrestricted_float")
            or not base.is_finite() or not 0 < base <= shares[0] or base != int(base)):
        raise ValueError("Invalid documented restructuring reference share base")
    price = Decimal(str(preclose))
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid restructuring prior close")
    added_value = amounts[0] + amounts[1] + donation + compensation
    threshold_text = item.get("reference_threshold_price", "")
    threshold = Decimal(threshold_text) if threshold_text else added_value / (shares[1] + shares[2])
    if not threshold.is_finite() or threshold < 0:
        raise ValueError("Invalid documented restructuring reference threshold")
    # Some issuers use original unrestricted float and an explicitly rounded branch threshold.
    return ((price * base + added_value) / (base + shares[1] + shares[2])
            if price > threshold else price)


def holder_restructuring_ex_price(preclose, item):
    """Issuer's adjusted reference, separate from account cash and allocation."""
    names = ("shares_before", "debt_shares", "investor_shares", "holder_shares", "surrender_shares")
    shares = {key: Decimal(str(item[key])) for key in names}
    names = ("debt_cash_equivalent", "investor_cash", "guarantee_shares", "guarantee_price",
             "guarantee_debt", "guarantee_provision", "trust_asset_value", "additional_guarantee_cash")
    amounts = {key: Decimal(str(item[key])) for key in names}
    occupation = Decimal(str(item.get("funds_occupation_impairment_adjustment", "") or "0"))
    if (not all(v.is_finite() and v >= 0 for v in [*shares.values(), *amounts.values(), occupation])
            or any(v != int(v) for v in shares.values()) or shares["shares_before"] <= 0
            or shares["holder_shares"] <= 0 or amounts["guarantee_shares"] != int(amounts["guarantee_shares"])
            or amounts["guarantee_provision"] > amounts["guarantee_debt"]
            or Decimal(str(item["cash_dividend"])) != 0):
        raise ValueError("Invalid holder-restructuring formula inputs")
    added_shares = sum(shares.values()) - shares["shares_before"]
    added_value = (amounts["debt_cash_equivalent"] + amounts["investor_cash"]
                   + amounts["guarantee_shares"] * amounts["guarantee_price"]
                   - (amounts["guarantee_debt"] - amounts["guarantee_provision"])
                   + amounts["trust_asset_value"] + amounts["additional_guarantee_cash"] + occupation)
    price = Decimal(str(preclose))
    threshold_text = item.get("reference_threshold_price", "")
    threshold = Decimal(str(threshold_text)) if threshold_text else added_value / added_shares
    if (not price.is_finite() or price <= 0 or added_value <= 0
            or not threshold.is_finite() or threshold < 0):
        raise ValueError("Invalid holder-restructuring reference value")
    return ((price * shares["shares_before"] + added_value) / sum(shares.values())
            if price > threshold else price)


def holder_bonus_ratio_bounds(item):
    """Published precision bounds are a receipt check, never an allocation rule."""
    ratio = Decimal(str(item["holder_bonus_ratio"]))
    kind = item.get("holder_bonus_ratio_kind", "") or "exact"
    if not ratio.is_finite() or not 0 < ratio <= 1 or kind not in {"exact", "rounded_approximate"}:
        raise ValueError("Invalid restricted holder bonus ratio")
    if kind == "exact":
        return ratio, ratio
    quantum = Decimal(1).scaleb(ratio.as_tuple().exponent)
    # Keep one published unit of uncertainty; the issuer did not specify rounding/tails.
    if not 0 < quantum <= Decimal("0.000001") or not 0 < ratio - quantum < ratio + quantum <= 1:
        raise ValueError("Approximate holder ratio needs the reviewed decimal precision")
    return ratio - quantum, ratio + quantum


def holder_distribution_action(item):
    """Canonical restricted bonus action; never distribute the total capital increase."""
    ratio = Decimal(str(item["holder_bonus_ratio"]))
    base = Decimal(str(item["eligible_share_base"]))
    total = Decimal(str(item["holder_shares"]))
    lower, upper = holder_bonus_ratio_bounds(item)
    if (not ratio.is_finite() or not 0 < ratio <= 1 or not base.is_finite()
            or base <= 0 or base != int(base)
            or not total.is_finite() or total <= 0 or total != int(total)
            or not isinstance(item["excluded_holders"], list) or not item["excluded_holders"]
            or date.fromisoformat(item["holder_listing_date"]) < date.fromisoformat(item["date"])):
        raise ValueError("Invalid restricted holder-distribution terms")
    approximate = lower != upper
    if approximate:
        if (not lower * base <= total <= upper * base
                or base > Decimal(str(item["shares_before"]))
                or item["holder_fraction_policy"] != "account_record_required"
                or item["holder_eligibility_rule"] != "exclude_named_parties"):
            raise ValueError("Approximate holder distribution requires actual account records")
    elif (int(base * ratio) != total or item["holder_fraction_policy"] != "csdc_tail_ranked"
          or item["holder_eligibility_rule"] != "exclude_named_parties_and_successors"):
        raise ValueError("Invalid restricted holder-distribution terms")
    action = {"symbol": item["symbol"], "dividRegistDate": item["registration_date"],
            "dividOperateDate": item["date"], "dividStockMarketDate": item["holder_listing_date"],
            "dividPayDate": "", "dividCashPsBeforeTax": "0", "dividStocksPs": "0",
            "dividReserveToStockPs": str(ratio), "action_kind": "restricted_restructuring",
            "holder_source_file": item["holder_source_file"],
            "holder_source_sha256": item["holder_source_sha256"]}
    if approximate:
        action.update(holder_bonus_ratio_kind="rounded_approximate", holder_fraction_policy="account_record_required")
    return action


def price_limits(preclose, day, is_st=False, board="main"):
    if board not in _QUANTITY_RULES or day < "2023-01-01":
        raise ValueError("This rule profile supports established SH/SZ ordinary stocks from 2023 only")
    rate = (Decimal("0.20") if board in {"chinext", "star"} else
            Decimal("0.05") if is_st and day < "2026-07-06" else Decimal("0.10"))
    return cents(Decimal(str(preclose)) * (1 - rate)), cents(Decimal(str(preclose)) * (1 + rate))


def fees(notional_cents, side, config, *, equity_etf=False, day=None):
    commission = max(cents(config.minimum_commission), round(notional_cents * config.commission)) if notional_cents else 0
    if equity_etf:
        # Legacy keyword selects ETF costs, not equity classification. All-in
        # commission is a simulation assumption, not a verified broker tariff.
        return commission
    # sell_tax is the post-cut rate. The dated multiplier preserves explicit
    # cost sensitivities (including zero tax) and undated legacy calculations.
    multiplier = 2 if day is not None and "2023-01-01" <= day < "2023-08-28" else 1
    return commission + round(notional_cents * config.transfer_fee) + (round(notional_cents * config.sell_tax * multiplier) if side == "sell" else 0)
