"""Provider date parsing must preserve observations imported from CSV panels."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import pandas as pd

from trading.research.factors import write_provider


class QlibProviderDateTests(unittest.TestCase):
    def panel(self, dates):
        return pd.DataFrame({"date": dates, "symbol": ["600000.SSE"] * len(dates),
                             "close": [10., 11.][:len(dates)]})

    def test_csv_dates_and_timestamp_dates_export_identical_observed_values(self):
        calendar = ["2024-01-02", "2024-01-03", "2024-01-04"]
        strings = self.panel(calendar[:2])
        original = strings.copy(deep=True)
        timestamps = strings.assign(date=pd.to_datetime(strings.date))
        with TemporaryDirectory() as root:
            folders = [Path(root) / "csv", Path(root) / "timestamps"]
            for panel, folder in zip((strings, timestamps), folders):
                write_provider(panel, calendar, folder, provider_fields=["close"])
            binaries = [(folder / "features/sh600000/close.day.bin").read_bytes()
                        for folder in folders]
            self.assertEqual(binaries[0], binaries[1])
            values = np.frombuffer(binaries[0], dtype="<f4")
            np.testing.assert_array_equal(values[:3], [0., 10., 11.])
            self.assertTrue(np.isnan(values[3]))
        pd.testing.assert_frame_equal(strings, original)

    def test_duplicate_identity_after_date_parsing_is_rejected_before_writing(self):
        panel = self.panel(pd.Series(["2024-01-02", pd.Timestamp("2024-01-02")], dtype=object))
        with TemporaryDirectory() as root:
            folder = Path(root) / "provider"
            with self.assertRaisesRegex(ValueError, "Unique observed panel"):
                write_provider(panel, ["2024-01-02"], folder, provider_fields=["close"])
            self.assertFalse(folder.exists())

    def test_nondaily_or_unknown_date_does_not_export_a_silent_gap(self):
        for value in ("2024-01-02 15:00:00", "2024-01-02T00:00:00+08:00", None, "invalid"):
            with self.subTest(value=value), TemporaryDirectory() as root:
                folder = Path(root) / "provider"
                with self.assertRaisesRegex(ValueError, "valid daily dates"):
                    write_provider(self.panel([value]), ["2024-01-02"], folder,
                                   provider_fields=["close"])
                self.assertFalse(folder.exists())

    def test_off_calendar_date_is_rejected_before_writing(self):
        with TemporaryDirectory() as root:
            folder = Path(root) / "provider"
            with self.assertRaisesRegex(ValueError, "Unique observed panel"):
                write_provider(self.panel(["2024-01-03"]), ["2024-01-02"], folder,
                               provider_fields=["close"])
            self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
