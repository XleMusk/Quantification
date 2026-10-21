"""Original synthetic exercise: CSV, merge, groupby, rolling and Parquet."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent


def main():
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    calendar = pd.bdate_range("2025-01-06", periods=8)
    panels = [pd.DataFrame({"date": calendar, "symbol": symbol,
                           "close": np.arange(base, base + 8, dtype=float),
                           "volume": np.arange(100, 108), "source": "synthetic"})
              for symbol, base in [("SYN_A", 10), ("SYN_B", 20)]]
    data = pd.concat(panels, ignore_index=True)
    data.to_csv(output / "synthetic_prices.csv", index=False)
    prices = pd.read_csv(output / "synthetic_prices.csv", parse_dates=["date"])
    industry = pd.DataFrame({"symbol": ["SYN_A", "SYN_B"], "group": ["teaching_1", "teaching_2"]})
    enriched = prices.merge(industry, on="symbol", how="left", validate="many_to_one")
    enriched = enriched.sort_values(["symbol", "date"])
    enriched["mean_close_3"] = enriched.groupby("symbol")["close"].transform(lambda x: x.rolling(3, min_periods=3).mean())
    totals = enriched.groupby("date").agg(volume_total=("volume", "sum"), stocks=("symbol", "nunique"))
    enriched.to_parquet(output / "synthetic_enriched.parquet", index=False)
    restored = pd.read_parquet(output / "synthetic_enriched.parquet")
    pd.testing.assert_frame_equal(enriched.reset_index(drop=True), restored.reset_index(drop=True))
    assert len(enriched) == 16 and enriched["group"].notna().all()
    assert enriched.loc[enriched["symbol"] == "SYN_A", "mean_close_3"].iloc[2] == 11
    assert totals["volume_total"].iloc[0] == 200
    assert enriched.groupby("symbol")["mean_close_3"].apply(lambda x: x.iloc[:2].isna().all()).all()
    totals.to_csv(output / "daily_totals.csv")
    result = {"status": "executed", "source": "synthetic", "rows": len(enriched), "symbols": 2, "sessions": 8,
              "checks": ["Parquet round trip equal", "16 rows and complete many-to-one join", "3-day mean = 11", "first-date volume = 200", "warm-up missing per symbol"],
              "market_backtest": False}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
