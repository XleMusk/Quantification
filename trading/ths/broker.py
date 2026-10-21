"""THS implementation of the shared broker surface, with submission disabled."""
from copy import deepcopy

from ..broker import BrokerAdapter, BrokerCapabilities, READ_KINDS
from ..contracts import MARKET_TZ, OrderIntent
from .adapter import PrefillAdapter, from_portfolio, from_target, now
from .observations import observation
from .weights import from_weights
from .signals import from_signal


class THSAdapter(BrokerAdapter):
    def __init__(self, profile, window=None):
        self.profile = deepcopy(profile)
        self.window = window

    @property
    def capabilities(self):
        return BrokerCapabilities(
            "ths", "manual_preview", READ_KINDS, True, False, False,
            ("cashflows", "movements", "restriction_releases", "share_credits"),
        )

    def _account(self, alias):
        if not alias or alias != self.profile.get("account_alias"):
            raise ValueError("Account alias mismatch")

    def _controls(self):
        if self.window is None:
            raise ValueError("No inspected client window was supplied")
        return PrefillAdapter(self.window, self.profile)

    def prepare(self, snapshot, target, *, current=None):
        self._account(snapshot.get("account_alias"))
        return from_target(snapshot, target, current=current)

    def prepare_portfolio(self, snapshot, portfolio, *, current=None, config=None):
        self._account(snapshot.get("account_alias"))
        return from_portfolio(snapshot, portfolio, current=current, config=config)

    def prepare_weights(self, snapshot, weights, *, current=None, config=None):
        self._account(snapshot.get("account_alias"))
        return from_weights(snapshot, weights, current=current, config=config)

    def prepare_signal(self, run_id, request, *, current=None):
        self._account(request.get("account_alias"))
        return from_signal(run_id, request, current=current)

    def read(self, kind, *, trading_date):
        if kind not in READ_KINDS:
            raise ValueError("Unsupported broker observation kind")
        self._account(self.profile.get("account_alias"))
        started = now()
        if trading_date != started.astimezone(MARKET_TZ).date().isoformat():
            raise ValueError("Control capture supports today's query pages only")
        raw = self._controls().read(kind)
        observed_at = now()
        if trading_date != observed_at.astimezone(MARKET_TZ).date().isoformat():
            raise ValueError("Control capture crossed a local trading date boundary")
        return observation(kind, raw["rows"], self.profile["tables"][kind],
                           account_alias=self.profile["account_alias"], observed_at=observed_at.isoformat(),
                           trading_date=trading_date, source="ths_controls", cash=raw["cash_available"],
                           cash_total=raw.get("cash_total"), cash_frozen=raw.get("cash_frozen"))

    def prefill(self, plan, stop_check=lambda: False):
        self._account(plan.get("account_alias"))
        return self._controls().prefill(plan, stop_check=stop_check)

    def submit(self, intent: OrderIntent):
        raise PermissionError("THS automatic submission is disabled; prefill is not submission")

    def cancel(self, order_id, *, day):
        raise PermissionError("THS cancellation is disabled; query the client instead")
