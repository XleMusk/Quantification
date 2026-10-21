import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.research.market_residual_outcomes import endpoint_opens, monthly_schedule
from trading.research.price_boundaries import NEW, OLD, EFFECTIVE, RENAME_SHA256


def test_exact_monthly_endpoint_proxy_and_canonical_symbol_without_censored_fallback(tmp_path):
    calendar = ["2025-02-28", "2025-03-03", "2025-03-31", "2025-04-01", "2025-04-30"]
    schedule = monthly_schedule([calendar[0], calendar[2]], calendar)
    assert schedule[calendar[0]]["entry_day"] == "2025-03-03"
    assert schedule[calendar[0]]["exit_day"] == "2025-04-01"
    assert schedule[calendar[2]]["entry_day"] == "2025-04-01"
    assert schedule[calendar[2]]["exit_day"] is None
    assert schedule[calendar[2]]["right_censored"] is True
    raw_path, adjusted_path = tmp_path/"raw.parquet", tmp_path/"adjusted.parquet"
    raw_writer = adjusted_writer = None
    try:
        for day in calendar:
            rows = []
            for code in (NEW, "sh.600002"):
                row = {field: "" for field in BAOSTOCK_FIELDS.split(",")}
                row.update(date=day, code=code, open="10", high="11", low="9", close="10",
                    preclose="10", volume="1000", amount="10000", adjustflag="3", tradestatus="1", isST="0")
                if day == "2025-04-01" and code == NEW:
                    row.update(open="6", close="6")
                if day == "2025-04-01" and code == "sh.600002":
                    row.update(volume="0", tradestatus="0")
                rows.append(row)
            raw = pd.DataFrame(rows, columns=BAOSTOCK_FIELDS.split(","))
            adjusted = raw[["date", "code", "adjustflag", "close", "volume", "tradestatus"]].copy()
            adjusted["adjustflag"] = "2"
            adjusted["close"] = ["18" if day == "2025-04-01" else "20", "20"]
            rt, at = pa.Table.from_pandas(raw, preserve_index=False), pa.Table.from_pandas(adjusted, preserve_index=False)
            if raw_writer is None:
                raw_writer, adjusted_writer = pq.ParquetWriter(raw_path, rt.schema), pq.ParquetWriter(adjusted_path, at.schema)
            raw_writer.write_table(rt)
            adjusted_writer.write_table(at)
    finally:
        if raw_writer is not None:
            raw_writer.close()
            adjusted_writer.close()
    rename = dict(old=OLD, new=NEW, effective_date=EFFECTIVE, published_date="2025-02-14",
        disclosure_sha256=RENAME_SHA256, share_count_unchanged_on_rename=True, pre_rename_forward_scale="0.986265")
    symbols = {"302132.SZSE", "600002.SSE"}
    values, source_read = endpoint_opens(calendar,
        {"2025-03-03": symbols, "2025-04-01": symbols},
        {"raw": {"path": str(raw_path)}, "adjusted": {"path": str(adjusted_path)}}, rename)
    assert values[("2025-03-03", "302132.SZSE")] == (20.0, None)
    assert values[("2025-04-01", "302132.SZSE")] == (18.0, None)
    assert values[("2025-04-01", "302132.SZSE")][0]/values[("2025-03-03", "302132.SZSE")][0]-1 == pytest.approx(-.1)
    assert values[("2025-04-01", "600002.SSE")] == (None, "inactive_or_unknown_endpoint")
    assert source_read["endpoint_dates_read"] == ["2025-03-03", "2025-04-01"]
    assert source_read["endpoint_rowgroups_each"] == 2
    assert not source_read["large_matrix_loaded"]
