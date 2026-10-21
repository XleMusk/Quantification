"""
Qlib Signal Generator
---------------------
Trains a LightGBM model on Alpha158 factors, generates daily prediction
scores, converts them to the bridge signal format, and exports to Parquet.

Usage (Windows -- must be guarded):
    python bridge/qlib_signal.py
"""

import sys
from pathlib import Path

# Ensure D:\Quantification is on Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np

import qlib
from qlib.constant import REG_CN
from qlib.data import D
from qlib.data.dataset import DatasetH
from qlib.data.dataset.handler import DataHandlerLP

from bridge.config import (
    QLIB_DATA_DIR, SIGNALS_DIR,
    QLIB_DATA_START, QLIB_DATA_END,
    TRAIN_START, TRAIN_END, VALID_START, VALID_END,
    TEST_START, TEST_END, DEFAULT_TOPK, DEFAULT_N_DROP,
)
from bridge.signal_format import write_signals
from bridge.code_mapping import qlib_to_vnpy


class QlibSignalPipeline:
    """End-to-end: Qlib init -> data prepare -> train -> predict -> signals."""

    def __init__(self, market="csi300", topk=DEFAULT_TOPK, n_drop=DEFAULT_N_DROP):
        self.market = market
        self.topk = topk
        self.n_drop = n_drop
        self.model = None
        self._initialized = False

    def init_qlib(self):
        """Initialize Qlib with threading backend (Windows-safe)."""
        if self._initialized:
            return
        qlib.init(
            provider_uri=str(QLIB_DATA_DIR),
            region=REG_CN,
            expression_cache=None,
            dataset_cache=None,
            joblib_backend="threading",
        )
        self._initialized = True

    def prepare_data(self):
        """Build DatasetH with train/valid/test segments using Alpha158."""
        self._validate_date_ranges()
        handler_config = {
            "start_time": TRAIN_START,
            "end_time": TEST_END,
            "fit_start_time": TRAIN_START,
            "fit_end_time": TRAIN_END,
            "instruments": self.market,
        }

        try:
            from qlib.contrib.data.handler import Alpha158
            handler = Alpha158(**handler_config)
        except ImportError:
            handler = DataHandlerLP(**handler_config)

        dataset = DatasetH(
            handler=handler,
            segments={
                "train": (TRAIN_START, TRAIN_END),
                "valid": (VALID_START, VALID_END),
                "test": (TEST_START, TEST_END),
            },
        )
        return dataset

    def _validate_date_ranges(self):
        """Fail fast when callers try to use the stale bundled Qlib data as live data."""
        dates = {
            "TRAIN_START": TRAIN_START,
            "TRAIN_END": TRAIN_END,
            "VALID_START": VALID_START,
            "VALID_END": VALID_END,
            "TEST_START": TEST_START,
            "TEST_END": TEST_END,
            "QLIB_DATA_START": QLIB_DATA_START,
            "QLIB_DATA_END": QLIB_DATA_END,
        }
        parsed = {key: pd.Timestamp(value) for key, value in dates.items()}
        if parsed["TRAIN_START"] < parsed["QLIB_DATA_START"] or parsed["TEST_END"] > parsed["QLIB_DATA_END"]:
            raise ValueError(
                "Qlib date range is outside bundled data coverage "
                f"{QLIB_DATA_START} ~ {QLIB_DATA_END}. "
                "Use a refreshed Qlib dataset or run `run_committee.py market-trend` for current A-share trend checks."
            )
        ordered = [
            parsed["TRAIN_START"],
            parsed["TRAIN_END"],
            parsed["VALID_START"],
            parsed["VALID_END"],
            parsed["TEST_START"],
            parsed["TEST_END"],
        ]
        if ordered != sorted(ordered):
            raise ValueError("Qlib train/valid/test dates must be chronological and non-overlapping.")

    def train_model(self, dataset):
        """Train LightGBM model (or use pre-trained)."""
        from qlib.contrib.model.gbdt import LGBModel

        print("Training LightGBM model...")
        self.model = LGBModel(
            loss="mse",
            num_leaves=64,
            learning_rate=0.05,
            n_estimators=200,
            early_stopping_rounds=30,
        )
        self.model.fit(dataset)
        print("  Training complete.")
        return self.model

    def predict(self, dataset):
        """Generate prediction pd.DataFrame with columns [score]."""
        print("Generating predictions on test set...")
        pred = self.model.predict(dataset, segment="test")
        if isinstance(pred, pd.Series):
            pred = pred.to_frame("score")
        print(f"  Predictions: {len(pred)} rows")
        return pred

    def convert_to_signals(self, pred_df: pd.DataFrame) -> pd.DataFrame:
        """Convert Qlib predictions to bridge signal format.

        Per date: rank by score desc, top-K -> BUY(1), bottom-N -> SELL(-1).
        """
        print(f"Converting to signals (topk={self.topk}, n_drop={self.n_drop})...")

        pred_df = pred_df.reset_index()  # MultiIndex -> columns

        records = []
        for _, row in pred_df.iterrows():
            records.append({
                "datetime": pd.Timestamp(row["datetime"]),
                "vt_symbol": qlib_to_vnpy(row["instrument"]),
                "qlib_code": row["instrument"],
                "score": float(row["score"]),
                "direction": 0,
                "weight": np.nan,
            })

        signals = pd.DataFrame(records)

        # Per-date ranking
        def assign_direction(group):
            n = len(group)
            if n == 0:
                return group
            t = min(self.topk, n)
            d = min(self.n_drop, max(0, n - t))
            ranked = group.sort_values("score", ascending=False)
            top_idx = ranked.head(t).index
            bottom_idx = ranked.tail(d).index if d > 0 else pd.Index([])
            group.loc[top_idx, "direction"] = 1
            group.loc[bottom_idx, "direction"] = -1
            group.loc[top_idx, "weight"] = 1.0 / t
            return group

        signals = signals.groupby("datetime", group_keys=False).apply(
            assign_direction
        )
        signals = signals.reset_index(drop=True)
        signals["direction"] = signals["direction"].astype("int8")

        n_buy = (signals["direction"] == 1).sum()
        n_sell = (signals["direction"] == -1).sum()
        n_dates = signals["datetime"].nunique()
        print(f"  {n_dates} trading days, {n_buy} BUY signals, {n_sell} SELL signals")
        return signals

    def run(self, output_path: Path = None) -> pd.DataFrame:
        """Full pipeline: init -> data -> train -> predict -> signals -> save."""
        self.init_qlib()
        dataset = self.prepare_data()
        self.train_model(dataset)
        pred_df = self.predict(dataset)
        signals = self.convert_to_signals(pred_df)

        if output_path is None:
            output_path = SIGNALS_DIR / "signals_latest.parquet"
        write_signals(signals, output_path)
        print(f"\nSignals exported to: {output_path}")
        print(f"  Size: {output_path.stat().st_size / 1024:.0f} KB")
        return signals


def main():
    """Generate signals from Qlib and export to Parquet."""
    pipeline = QlibSignalPipeline(market="csi300", topk=30, n_drop=5)
    pipeline.run()


if __name__ == "__main__":
    main()
