from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.config import StrategyConfig
from trading.engine import advance
from trading.ledger import new_state
from trading.research.bulk_feed import BulkResearchFeed, DailyRows, adjusted_reference_delta
from trading.research.price_boundaries import RENAME_SHA256


class BulkSessionTests(unittest.TestCase):
    def feed(self):
        feed = object.__new__(BulkResearchFeed)
        feed.rename = None
        feed.unsupported_instruments = {}
        feed.manifest = {"replay_only": True}
        feed.actions_by_ex = defaultdict(list)
        feed.unresolved_references = defaultdict(list)
        feed.allow_adjusted_continuity = False
        feed.adjusted_reference_continuity = {}
        return feed

    def action(self):
        return {"symbol": "600000.SSE", "dividOperateDate": "2024-07-18",
                "dividCashPsBeforeTax": "0.321", "dividStocksPs": "0.000000",
                "dividReserveToStockPs": "", "dividPayDate": "2024-07-18",
                "dividStockMarketDate": ""}

    def test_unknown_unheld_action_does_not_filter_or_block_universe(self):
        feed = self.feed()
        feed.actions_by_ex["2024-07-18"].append(self.action())
        feed.unresolved_references["2024-07-18"].append(("600000.SSE", "unresolved_blank_terms"))
        feed.validate_session(new_state(140000), "2024-07-18")

    def test_held_unknown_terms_fail_before_any_account_mutation(self):
        feed = self.feed()
        feed.actions_by_ex["2024-07-18"].append(self.action())
        state = new_state(140000)
        state["entitlements"]["600000.SSE:2024-07-18"] = 100
        before = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "missing=dividReserveToStockPs"):
            advance(state, feed, "2024-07-18", StrategyConfig(), 0)
        self.assertEqual(state, before)

    def test_explicit_zero_terms_allow_cash_only_action(self):
        feed = self.feed()
        action = self.action()
        action["dividReserveToStockPs"] = "0"
        feed.actions_by_ex["2024-07-18"].append(action)
        state = new_state(140000)
        state["entitlements"]["600000.SSE:2024-07-18"] = 100
        feed.validate_session(state, "2024-07-18")

    def test_known_positive_bonus_requires_tax_basis_before_account_mutation(self):
        for basis in (None, "", "NaN", "0", "0.4"):
            feed = self.feed()
            action = {**self.action(), "dividStocksPs": "0.4", "dividReserveToStockPs": "0",
                      "dividStockMarketDate": "2024-07-18"}
            if basis is not None:
                action["bonus_taxable_cash_ps"] = basis
            feed.actions_by_ex["2024-07-18"].append(action)
            state = new_state(140000)
            state["entitlements"]["600000.SSE:2024-07-18"] = 100
            before = deepcopy(state)
            if basis == "0.4":
                feed.validate_session(state, "2024-07-18")
            else:
                with self.subTest(basis=basis), self.assertRaisesRegex(ValueError, "bonus taxable basis"):
                    advance(state, feed, "2024-07-18", StrategyConfig(), 0)
            self.assertEqual(state, before)

    def test_pending_execution_with_unresolved_reference_stops(self):
        feed = self.feed()
        feed.unresolved_references["2024-07-18"].append(("600000.SSE", "reference_mismatch"))
        state = new_state(140000)
        state["pending_targets"] = {"600000.SSE": 0.16}
        with self.assertRaisesRegex(ValueError, "reference_mismatch"):
            feed.validate_session(state, "2024-07-18")

    def test_adjusted_continuity_override_is_narrow_and_research_only(self):
        feed = self.feed()
        feed.allow_adjusted_continuity = True
        feed.unresolved_references["2024-07-18"].append(("600000.SSE", "reference_mismatch"))
        feed.adjusted_reference_continuity[("600000.SSE", "2024-07-18")] = 0.00002
        state = new_state(140000)
        state["pending_targets"] = {"600000.SSE": 0.16}
        feed.validate_session(state, "2024-07-18")

        feed.unresolved_references["2024-07-19"].append(("600000.SSE", "unresolved_blank_terms"))
        state["pending_targets"] = {"600000.SSE": 0.16}
        with self.assertRaisesRegex(ValueError, "unresolved_blank_terms"):
            feed.validate_session(state, "2024-07-19")

    def test_adjusted_continuity_delta_compares_panel_preclose_to_prior_close(self):
        days = ["2024-07-17", "2024-07-18"]
        precloses = pd.DataFrame([[10.0], [20.00002]], index=days, columns=["600000.SSE"])
        closes = pd.DataFrame([[20.0], [21.0]], index=days, columns=["600000.SSE"])
        day_index = {day: i for i, day in enumerate(days)}
        self.assertAlmostEqual(
            adjusted_reference_delta(precloses, closes, day_index, "600000.SSE", "2024-07-18"),
            0.00002, places=6)
        self.assertIsNone(
            adjusted_reference_delta(precloses, closes, day_index, "600000.SSE", "2024-07-18", tolerance=0.00001))

    def test_dated_alias_is_not_silently_executed_with_future_code(self):
        feed = self.feed()
        feed.rename = {"old": "sz.300114", "new": "sz.302132", "effective_date": "2025-02-17"}
        feed.code_symbols = {"sz.302132": "302132.SZSE"}
        state = new_state(140000)
        state["pending_targets"] = {"302132.SZSE": 0.16}
        with self.assertRaisesRegex(ValueError, "tradable code=sz.300114"):
            feed.validate_session(state, "2025-02-14")
        feed.validate_session(state, "2025-02-17")

    def test_invalid_positive_action_dates_do_not_credit_cash_or_early_shares(self):
        for field, value in (("dividPayDate", "bad-date"), ("dividStockMarketDate", "2024-07-17")):
            feed = self.feed()
            action = {**self.action(), "dividReserveToStockPs": "0.1", "dividStockMarketDate": "2024-07-18", field: value}
            feed.actions_by_ex["2024-07-18"].append(action)
            state = new_state(140000)
            state["entitlements"]["600000.SSE:2024-07-18"] = 100
            with self.assertRaisesRegex(ValueError, "date"):
                feed.validate_session(state, "2024-07-18")

    def test_daily_execution_quotes_stay_raw_and_turnover_alignment_is_enforced(self):
        day = "2024-01-02"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.parquet"
            row = {**dict.fromkeys(BAOSTOCK_FIELDS.split(","), ""), "date": day, "code": "sh.600000", "adjustflag": "3", "open": "10", "close": "10",
                   "high": "11", "low": "9", "preclose": "10", "volume": "10000", "amount": "100000",
                   "tradestatus": "1", "isST": "0"}
            pq.write_table(pa.Table.from_pylist([row]), path)
            feed = SimpleNamespace(calendar=[day], day_index={day: 0}, codes=np.array(["sh.600000"]),
                day_members=[np.array([0])], rename=None, unsupported_instruments={},
                code_symbols={"sh.600000": "600000.SSE"}, closes=pd.DataFrame([[5.]]),
                amounts=pd.DataFrame([[100000.]]), volumes=pd.DataFrame([[10000.]]))
            quotes = DailyRows(feed, path)
            try:
                self.assertEqual(quotes.get(day)["600000.SSE"]["close"], 10.)
                self.assertEqual(feed.closes.iloc[0, 0], 5.)
                feed.amounts.iloc[0, 0] = 1
                quotes.load.cache_clear()
                with self.assertRaisesRegex(ValueError, "amount alignment"):
                    quotes.get(day)
            finally:
                quotes.close()

    def test_raw_alias_reuses_proven_duplicate_and_boundary_blank_rules(self):
        days = ["2025-02-14", "2025-02-17"]
        rename = {"old": "sz.300114", "new": "sz.302132", "effective_date": days[1],
                  "published_date": days[0], "disclosure_sha256": RENAME_SHA256,
                  "share_count_unchanged_on_rename": True, "pre_rename_forward_scale": "0.986265"}
        base = {**dict.fromkeys(BAOSTOCK_FIELDS.split(","), ""), "adjustflag": "3", "open": "72.18", "close": "72.18",
                "high": "73", "low": "71", "preclose": "72", "volume": "10000", "amount": "721800",
                "tradestatus": "1", "isST": "0"}
        for conflict in (None, "pre_rename", "boundary_blank"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as folder:
                rows = []
                for day in days:
                    new = {**base, "date": day, "code": rename["new"]}
                    old = {**new, "code": rename["old"]}
                    if day == days[1]:
                        old.update({key: "" for key in ("open", "high", "low", "close", "preclose", "volume", "amount")})
                        old["tradestatus"] = "0"
                        if conflict == "boundary_blank":
                            old["close"] = "1"
                    elif conflict == "pre_rename":
                        old["peTTM"] = "1"
                    rows.append([old, new])
                path = Path(folder) / "raw.parquet"
                with pq.ParquetWriter(path, pa.Table.from_pylist(rows[0]).schema) as writer:
                    for values in rows:
                        writer.write_table(pa.Table.from_pylist(values))
                feed = SimpleNamespace(calendar=days, day_index={day: i for i, day in enumerate(days)},
                    codes=np.array([rename["new"]]), day_members=[np.array([0])] * 2,
                    rename=rename, unsupported_instruments={}, code_symbols={rename["new"]: "302132.SZSE"},
                    amounts=pd.DataFrame([[721800.]] * 2), volumes=pd.DataFrame([[10000.]] * 2))
                quotes = DailyRows(feed, path)
                try:
                    if conflict:
                        day = days[0] if conflict == "pre_rename" else days[1]
                        with self.assertRaises(ValueError):
                            quotes.get(day)
                    else:
                        for day in days:
                            self.assertEqual(set(quotes.get(day)), {"302132.SZSE"})
                            self.assertEqual(quotes.get(day)["302132.SZSE"]["close"], 72.18)
                finally:
                    quotes.close()


if __name__ == "__main__":
    unittest.main()
