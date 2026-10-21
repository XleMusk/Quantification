"""Build an explicit raw-RMB quote observation for the THS handoff consumer."""
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re


def _price(value):
    text = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise ValueError("Quote price must be a positive decimal RMB value")
    value = Decimal(text)
    if not value.is_finite() or value <= 0 or value != value.quantize(Decimal(".01")):
        raise ValueError("Quote price must be positive and representable to two RMB decimals")
    return f"{value:.2f}"


def _optional_bool(value, *, name):
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean")


def build_quote_observation(path, *, source, observed_at, trading_date):
    stamp = datetime.fromisoformat(observed_at)
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("Quote observation requires an explicit non-future timezone timestamp")
    date.fromisoformat(trading_date)
    if not str(source).strip():
        raise ValueError("Quote source is required")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"symbol", "market", "price_rmb"}
        if not reader.fieldnames or not required <= set(reader.fieldnames):
            raise ValueError("Quote CSV requires symbol, market and price_rmb columns")
        prices = {}
        for row in reader:
            symbol = str(row["symbol"]).strip()
            market = str(row["market"]).strip()
            if not re.fullmatch(r"\d{6}", symbol) or market not in {"SSE", "SZSE", "BSE"}:
                raise ValueError("Quote CSV requires six-digit symbols and explicit SSE/SZSE/BSE markets")
            qualified = f"{symbol}.{market}"
            if qualified in prices:
                raise ValueError(f"Duplicate quote symbol: {qualified}")
            row_source = str(row.get("quote_source") or source).strip()
            if not row_source:
                raise ValueError(f"Quote source is required for {qualified}")
            prices[qualified] = {
                "price_rmb": _price(row["price_rmb"]),
                "source": row_source,
            }
            if row.get("preclose_rmb") not in (None, ""):
                prices[qualified]["preclose_rmb"] = _price(row["preclose_rmb"])
            if row.get("is_st") not in (None, ""):
                prices[qualified]["is_st"] = _optional_bool(
                    row["is_st"], name=f"{qualified}.is_st"
                )
            if row.get("tradable") not in (None, ""):
                prices[qualified]["tradable"] = _optional_bool(
                    row["tradable"], name=f"{qualified}.tradable"
                )
    if not prices:
        raise ValueError("Quote CSV is empty")
    return {
        "schema": "ths_raw_quote_observation_v1",
        "source": str(source).strip(),
        "observed_at": observed_at,
        "trading_date": trading_date,
        "price_basis": "raw_rmb",
        "prices": prices,
    }
