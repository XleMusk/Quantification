import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(
    r"D:\Quantification\outputs\shortline_research\20261014_limitup_continuation\check_shortline_freshness.py"
)
SPEC = importlib.util.spec_from_file_location("shortline_freshness", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

CN_TZ = timezone(timedelta(hours=8))


def test_market_phase_is_clock_only_and_explicit():
    assert MODULE.market_phase(datetime(2026, 10, 14, 8, 59, tzinfo=CN_TZ)) == "pre_open"
    assert MODULE.market_phase(datetime(2026, 10, 14, 10, 0, tzinfo=CN_TZ)) == "intraday"
    assert MODULE.market_phase(datetime(2026, 10, 14, 15, 6, tzinfo=CN_TZ)) == "post_close"
    assert MODULE.market_phase(datetime(2026, 10, 17, 12, 0, tzinfo=CN_TZ)) == "non_trading_weekend"


def test_pinned_calendar_distinguishes_open_session_from_holiday():
    assert MODULE.calendar_status(date(2026, 10, 14)) == "scheduled_open_session"
    assert MODULE.calendar_status(date(2026, 10, 1)) == "closed_scheduled_holiday"
    assert MODULE.calendar_status(date(2026, 10, 17)) == "closed_weekend"
