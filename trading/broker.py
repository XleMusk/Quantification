"""Broker-neutral execution surface; capabilities are not client verification."""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from .contracts import OrderIntent


READ_KINDS = ("account", "orders", "trades")


@dataclass(frozen=True)
class BrokerCapabilities:
    channel: str
    mode: str
    read_kinds: tuple[str, ...]
    prefill_implemented: bool
    submit_enabled: bool
    cancel_enabled: bool
    manual_export_kinds: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


class BrokerAdapter(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Implemented operations, not proof of login, permission or fresh data."""

    @abstractmethod
    def read(self, kind: str, *, trading_date: str) -> dict:
        """Read a dated account/order/trade observation with explicit provenance."""

    @abstractmethod
    def submit(self, intent: OrderIntent) -> dict:
        """Acceptance is not a fill. Live channels must enforce separate authorization."""

    @abstractmethod
    def cancel(self, order_id: str, *, day: str) -> dict:
        """Request cancellation; do not infer confirmation from a request alone."""

    def prefill(self, plan: dict, stop_check=lambda: False) -> dict:
        raise NotImplementedError("This broker has no manual-preview form")
