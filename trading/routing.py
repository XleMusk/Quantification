"""Offline broker-route decisions for the single-account execution path.

The route is a planning decision, not proof of connectivity or permission.  A
personal account currently has one supported manual route: main-board stocks
through the Tonghuashun order form.  Unsupported boards must remain visible as
unroutable instead of silently falling through to another account or adapter.
"""

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class RouteDecision:
    symbol: str
    route_id: str
    channel: str
    mode: str
    status: str
    reason: str
    execution_admitted: bool = False

    def to_dict(self):
        return asdict(self)


def route_order(symbol: str) -> RouteDecision:
    """Resolve one order to the only configured local route.

    This deliberately does not inspect the network, a desktop window, an
    account, or current quotes.  It only answers whether the instrument is in
    the supported manual THS route.
    """
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(SSE|SZSE|BSE)", symbol):
        return RouteDecision(
            symbol=str(symbol), route_id="none", channel="none", mode="none",
            status="invalid_symbol", reason="explicit_exchange_qualified_symbol_required",
        )
    if re.fullmatch(r"60[0-9]{4}\.SSE|00[0-9]{4}\.SZSE", symbol):
        return RouteDecision(
            symbol=symbol, route_id="ths_manual_mainboard", channel="ths",
            mode="manual_prefill_only", status="route_available",
            reason="supported_mainboard_manual_route; account_and_quote_observation_still_required",
        )
    return RouteDecision(
        symbol=symbol, route_id="none", channel="none", mode="none",
        status="unsupported_instrument_route",
        reason="local THS route is restricted to SSE 60xxxx and SZSE 00xxxx main-board stocks",
    )

