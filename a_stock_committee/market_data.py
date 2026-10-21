"""A-share market data access and local cache.

The first provider is BaoStock because it is free and tokenless. Tushare and
broker data can be added later as higher-quality sources for live decisions.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import io
import os
from pathlib import Path
from typing import Iterator
from contextlib import redirect_stdout

import pandas as pd

from trading.symbols import SymbolInfo, infer_market, infer_security_type, normalize_symbol


DATA_ROOT = Path("market_data") / "a_share"
DAILY_DIR = DATA_ROOT / "daily"


BAOSTOCK_FIELDS = ",".join(
    [
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "turn",
        "tradestatus",
        "pctChg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    ]
)


def default_start_date(years: int = 3) -> str:
    return (date.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")


def default_end_date() -> str:
    return date.today().strftime("%Y-%m-%d")


@contextmanager
def _baostock_session_lock():
    """Avoid overlapping anonymous sessions and the SDK's process-global socket."""
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Quantification" / "baostock-session.lock"
    path = Path(os.environ.get("BAOSTOCK_SESSION_LOCK", default))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise RuntimeError("BaoStock session busy; finish the other query before opening another session") from error
        try:
            yield
        finally:
            release()


@contextmanager
def _baostock_transport():
    """Stop the SDK's receive loop when the peer closes an unfinished response."""
    import baostock.common.context as context
    import baostock.util.socketutil as transport

    original_send = transport.send_msg
    state = {"failed": False}

    class SocketReader:
        def __init__(self, connection):
            self.connection = connection
            self.error = None

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def recv(self, *args):
            try:
                chunk = self.connection.recv(*args)
                if not chunk:
                    raise ConnectionError("BaoStock peer closed an incomplete response")
                return chunk
            except OSError as error:
                self.error = error
                raise

    def send(message):
        connection = getattr(context, "default_socket", None)
        if connection is None:
            return original_send(message)
        reader = SocketReader(connection)
        context.default_socket = reader
        try:
            result = original_send(message)
            if reader.error is not None:
                state["failed"] = True
                raise reader.error
            return result
        finally:
            context.default_socket = connection

    transport.send_msg = send
    try:
        yield state
    finally:
        transport.send_msg = original_send
        connection = getattr(context, "default_socket", None)
        if connection is not None:
            connection.close()
            context.default_socket = None


@contextmanager
def baostock_session() -> Iterator[object]:
    """Login/logout BaoStock, exclusively across project processes."""
    import baostock as bs

    with _baostock_session_lock(), _baostock_transport() as transport:
        with redirect_stdout(io.StringIO()):
            lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {lg.error_msg}")
        try:
            yield bs
        finally:
            if not transport["failed"]:
                with redirect_stdout(io.StringIO()):
                    bs.logout()


def fetch_daily_baostock(
    symbol: str,
    start_date: str,
    end_date: str,
    adjustflag: str = "2",
) -> pd.DataFrame:
    """Fetch one stock's daily bars from BaoStock."""
    info = normalize_symbol(symbol)
    with baostock_session() as bs:
        rs = bs.query_history_k_data_plus(
            info.baostock_code,
            BAOSTOCK_FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {rs.error_msg}")

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

    if not rows:
        raise ValueError(f"no daily data returned for {symbol} from {start_date} to {end_date}")

    df = pd.DataFrame(rows, columns=rs.fields)
    df["date"] = pd.to_datetime(df["date"])
    df["vt_symbol"] = info.vt_symbol
    df["qlib_code"] = info.qlib_code
    df["security_type"] = info.security_type
    df["source"] = "baostock"

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "turn",
        "tradestatus",
        "pctChg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def daily_cache_path(symbol: str) -> Path:
    info = normalize_symbol(symbol)
    return DAILY_DIR / f"{info.vt_symbol.replace('.', '_')}.parquet"


def ensure_daily_data(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """Return cached daily data, fetching if needed."""
    start_date = start_date or default_start_date()
    end_date = end_date or default_end_date()
    path = daily_cache_path(symbol)

    if path.exists() and not force:
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        available_start = df["date"].min().strftime("%Y-%m-%d")
        available_end = df["date"].max().strftime("%Y-%m-%d")
        if available_start <= start_date and available_end >= end_date:
            return _slice_dates(df, start_date, end_date)

    df = fetch_daily_baostock(symbol, start_date=start_date, end_date=end_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df


def _slice_dates(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    return df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)


def cache_summary() -> pd.DataFrame:
    """Summarize local daily cache files."""
    rows = []
    for path in sorted(DAILY_DIR.glob("*.parquet")):
        df = pd.read_parquet(path, columns=["date", "vt_symbol"])
        rows.append(
            {
                "file": str(path),
                "vt_symbol": df["vt_symbol"].iloc[0] if not df.empty else "",
                "rows": len(df),
                "start": pd.to_datetime(df["date"]).min(),
                "end": pd.to_datetime(df["date"]).max(),
                "size_mb": path.stat().st_size / 1024 / 1024,
            }
        )
    return pd.DataFrame(rows)
