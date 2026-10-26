from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from trading.research.shortline_baostock_raw_snapshot import capture


FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST".split(",")


class _Result:
    error_code = "0"
    error_msg = "success"

    def get_data(self):
        return pd.DataFrame([{
            "date": "2026-10-15", "code": "sz.002815", "open": "23.25", "high": "24.0",
            "low": "22.5", "close": "23.25", "preclose": "21.14", "volume": "1000",
            "amount": "23250", "adjustflag": "3", "turn": "1", "tradestatus": "1",
            "pctChg": "9.98", "peTTM": "1", "pbMRQ": "1", "psTTM": "1",
            "pcfNcfTTM": "1", "isST": "0",
        }], columns=FIELDS)


class _Client:
    def query_history_k_data_plus(self, *args, **kwargs):
        return _Result()


@contextmanager
def _session():
    yield _Client()


def test_capture_writes_hash_bound_parquet_manifest(tmp_path):
    result = capture(["002815.SZSE"], "2026-10-15", "2026-10-16", tmp_path / "out",
                     session_factory=_session, sleep_seconds=0)
    assert result["execution_admitted"] is False
    assert result["parquet"]["rows"] == 1
    assert (tmp_path / "out" / "manifest.json").is_file()
    frame = pd.read_parquet(tmp_path / "out" / "raw_daily.parquet")
    assert list(frame.columns) == ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st", "pre_close"]
    assert frame.iloc[0]["code"] == "002815"


def test_capture_rejects_noncanonical_symbol(tmp_path):
    with pytest.raises(ValueError, match="ordinary canonical"):
        capture(["002815"], "2026-10-15", "2026-10-16", tmp_path / "out",
                session_factory=_session, sleep_seconds=0)
