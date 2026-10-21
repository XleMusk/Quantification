import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from trading.config import StrategyConfig
from trading.research.experiment import DEFAULT_CONFIG, ScoreFeed, export_scores, fit_model, replay
from trading.research import experiment
from trading.market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from trading.research.factors import (LABEL, candidates, catalog, daily_ic, group_diagnostics,
                                     native_features, segment_mask)


class FactorMiningTests(unittest.TestCase):
    def _baseline_orchestration_fixture(self, ingestion_only=False, fail_calls=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "snapshot"
            dataset.mkdir()
            metadata_name = "manifest.json" if ingestion_only else "selection.csv"
            for name in ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv", metadata_name):
                (dataset / name).write_bytes(b"synthetic orchestration fixture only")
            config = json.loads(DEFAULT_CONFIG.read_text())
            config["dataset"] = str(dataset)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            dates = ["2024-01-02", "2024-01-03", "2025-01-02", "2025-01-03", "2026-01-02", "2026-09-24"]
            symbols = [f"60000{i}.SSE" for i in range(10)]
            index = pd.MultiIndex.from_product([pd.to_datetime(dates), symbols], names=["date", "symbol"])
            features = pd.DataFrame({"ROC5": np.tile(np.arange(10), 6), "VMA5": np.tile(np.arange(10) / 10, 6)}, index=index)
            definitions = pd.DataFrame({"name": ["ROC5", "VMA5"], "family": ["ROC", "VMA"], "formula": ["fixture1", "fixture2"]})
            labels = pd.DataFrame({LABEL: features.ROC5 / 100, "exit_date": index.get_level_values("date")}, index=index)
            feed = SimpleNamespace(folder=dataset, calendar=dates, symbols=symbols,
                manifest={"scope": INGESTION_SCOPE if ingestion_only else FIXED_SCOPE,
                          "selection_date": "2023-12-29", "limitations": [],
                          "selection_is_point_in_time": not ingestion_only, "live_ready": False,
                          "selection": {"expected_symbols": 10, "complete_symbols": 10,
                                        "source_files_complete": True, "snapshot_validated": True, "missing": []}})
            model = SimpleNamespace(best_iteration_=1, booster_=SimpleNamespace(
                save_model=lambda path: Path(path).write_text("test double, not trained weights")))
            replay_configs = []
            def fake_replay(scored_feed, portfolio, sessions, destination):
                replay_configs.append(portfolio.to_dict())
                self.assertEqual(sessions, dates[-2:])
                if len(replay_configs) in fail_calls:
                    destination.mkdir(parents=True)
                    receipt = {"status": "failed", "error": "ValueError: Unresolved rights-issue entitlement",
                               "completed_sessions": 1, "last_completed_session": sessions[0],
                               "attempted_session": sessions[1], "account_basis": "last_successfully_completed_session"}
                    (destination / "replay.json").write_text(json.dumps(receipt))
                    raise ValueError("Unresolved rights-issue entitlement")
                return {"net_return": 0., "max_drawdown": 0., "turnover_over_initial": 0.}
            with patch.object(experiment, "DATA_ROOT", root / "data"), \
                    patch.object(experiment, "HistoricalFeed", return_value=feed), \
                    patch.object(experiment, "catalog", return_value=definitions), \
                    patch.object(experiment, "research_panel", return_value=(pd.DataFrame(), labels, pd.Series(True, index=index), {"symbols": 10})), \
                    patch.object(experiment, "native_features", return_value=features) as native, \
                    patch.object(experiment, "candidates", side_effect=AssertionError("candidate generation forbidden")), \
                    patch.object(experiment, "search", side_effect=AssertionError("selection forbidden")), \
                    patch.object(experiment, "fit_model", return_value=(model, features.median())), \
                    patch.object(experiment, "predict", side_effect=lambda fitted, x: x.ROC5.rename("score")), \
                    patch.object(experiment, "replay", side_effect=fake_replay):
                out, record = experiment.run(config_path, root / "out", baseline_only=True,
                                             ingestion_only=ingestion_only)
            self.assertEqual(native.call_args.args[-1], [])
            self.assertEqual(record["status"], "failed_ledger_comparison" if fail_calls else "completed")
            self.assertFalse(record["candidate_search_performed"])
            self.assertFalse(record["fresh_holdout_claimed"])
            self.assertEqual(record["selection"]["candidates_generated"], 0)
            self.assertEqual({r["variant"] for r in record["model_metrics"]}, {"B0"})
            self.assertEqual(len(replay_configs), 4)
            self.assertTrue(all(c == replay_configs[0] for c in replay_configs))
            self.assertTrue(all(d["comparison"] == "B0_minus_RULE" for d in record["paired_deltas"]))
            self.assertEqual(len(record["ledger_failures"]), len(fail_calls))
            if fail_calls:
                self.assertIn("Aggregate comparison unavailable", (out / "REPORT.md").read_text())
                for trial, comparison in enumerate(record["paired_deltas"], start=1):
                    available = trial not in fail_calls and 4 not in fail_calls
                    self.assertEqual(comparison["comparison_status"],
                                     "available" if available else "unavailable_failed_ledger")
                    if not available:
                        self.assertIsNone(comparison["delta_net_return"])
                for trial, result in enumerate(record["portfolio_metrics"], start=1):
                    self.assertEqual(result["status"], "failed" if trial in fail_calls else "completed")
                    if trial in fail_calls:
                        self.assertNotIn("net_return", result)
                        self.assertEqual(result["completed_sessions"], 1)
            self.assertFalse((out / "candidate_formulas.csv").exists())
            self.assertIn(metadata_name, record["input_hashes"])
            self.assertFalse(record["strategy_validated"])
            self.assertFalse(record["live_ready"])
            if ingestion_only:
                self.assertEqual(record["experiment_kind"], "alpha158_ingestion_ledger_comparison")
                self.assertIn("NOT strategy validation", (out / "REPORT.md").read_text())
                self.assertFalse((dataset / "selection.csv").exists())
            else:
                self.assertIn("10 fixed pre-period", (out / "REPORT.md").read_text())

    def test_baseline_only_never_generates_or_selects_candidates(self):
        self._baseline_orchestration_fixture()

    def test_ingestion_only_reuses_models_and_ledger_without_search_or_scope_promotion(self):
        self._baseline_orchestration_fixture(ingestion_only=True)

    def test_failed_first_model_ledger_does_not_skip_other_seeds_or_rule(self):
        self._baseline_orchestration_fixture(ingestion_only=True, fail_calls=(1,))

    def test_failed_rule_has_no_paired_or_aggregate_return(self):
        self._baseline_orchestration_fixture(ingestion_only=True, fail_calls=(4,))

    def test_all_failed_ledgers_still_preserve_prediction_diagnostics_and_failure_report(self):
        self._baseline_orchestration_fixture(ingestion_only=True, fail_calls=(1, 2, 3, 4))

    def test_baseline_failure_handler_does_not_hide_programming_errors(self):
        with patch.object(experiment, "replay", side_effect=TypeError("programming error")):
            with self.assertRaisesRegex(TypeError, "programming error"):
                experiment.baseline_ledger_result(None, StrategyConfig(), [], Path("unused"))

    def test_ingestion_mode_does_not_admit_search_unknown_scope_or_pit_claim(self):
        config = json.loads(DEFAULT_CONFIG.read_text())
        feed = SimpleNamespace(calendar=["2026-09-24"], symbols=["600000.SSE"],
            manifest={"selection_date": "2023-12-29", "scope": INGESTION_SCOPE,
                      "selection_is_point_in_time": False, "live_ready": False})
        experiment.validate_config(config, feed, baseline_only=True, ingestion_only=True)
        with self.assertRaisesRegex(ValueError, "factor search"):
            experiment.validate_config(config, feed, ingestion_only=True)
        for field, value in (("scope", FIXED_SCOPE), ("selection_is_point_in_time", True), ("live_ready", True)):
            changed = SimpleNamespace(**vars(feed))
            changed.manifest = {**feed.manifest, field: value}
            with self.assertRaisesRegex(ValueError, "non-PIT"):
                experiment.validate_config(config, changed, baseline_only=True, ingestion_only=True)

    def test_baseline_requires_complete_fixed_scope(self):
        config = json.loads(DEFAULT_CONFIG.read_text())
        feed = SimpleNamespace(calendar=["2026-09-24"], symbols=["600000.SSE"],
            manifest={"selection_date": "2023-12-29", "scope": "partial_acquisition"})
        with self.assertRaisesRegex(ValueError, "complete fixed"):
            experiment.validate_config(config, feed, baseline_only=True)

    def test_failed_replay_keeps_only_committed_sessions(self):
        calls = []
        def advance_one(state, feed, day, config, count):
            calls.append(day)
            if count == 1:
                state["cash"] = -123
                raise ValueError("Unresolved delisted holding")
            state["day"] = day
            return {"date": day, "equity": config.initial_cash, "events": []}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "failed"
            with patch("trading.research.experiment.advance", side_effect=advance_one):
                with self.assertRaisesRegex(ValueError, "Unresolved delisted"):
                    replay(None, StrategyConfig(), ["2024-01-02", "2024-01-03"], path)
            record = json.loads((path / "replay.json").read_text())
            state = json.loads((path / "account.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["completed_sessions"], 1)
            self.assertEqual(record["attempted_session"], "2024-01-03")
            self.assertEqual(state["day"], "2024-01-02")
            self.assertEqual(state["cash"], 10000000)
            self.assertEqual(len(pd.read_csv(path / "daily.csv")), 1)

    def test_daily_ic_is_not_pooled_correlation(self):
        index = pd.MultiIndex.from_product([pd.to_datetime(["2024-01-02", "2024-01-03"]), range(5)], names=["date", "symbol"])
        x = pd.DataFrame({"x": np.r_[np.arange(5), np.arange(5) + 100]}, index=index)
        y = pd.Series(np.r_[np.arange(5)[::-1], np.arange(5)[::-1] + 100], index=index)
        self.assertGreater(spearmanr(x.x, y).statistic, 0)
        np.testing.assert_allclose(daily_ic(x, y).x, -1.)

    def test_pairwise_missing_rank_ic(self):
        index = pd.MultiIndex.from_product([[pd.Timestamp("2024-01-02")], range(8)], names=["date", "symbol"])
        x = pd.DataFrame({"x": [1, 8, np.nan, 3, 2, 5, 7, 9], "constant": 1}, index=index)
        y = pd.Series([8, np.nan, 2, 9, 4, 1, 7, 0], index=index)
        good = x.x.notna() & y.notna()
        result = daily_ic(x, y)
        self.assertAlmostEqual(result.x.iloc[0], spearmanr(x.x[good], y[good]).statistic)
        self.assertTrue(result.constant.isna().all())

    def test_cross_boundary_labels_purged_not_signals(self):
        index = pd.MultiIndex.from_product([pd.bdate_range("2024-12-23", periods=10), ["600000.SSE"]], names=["date", "symbol"])
        dates = pd.Series(pd.bdate_range("2024-12-23", periods=10), index=index)
        labels = pd.DataFrame({LABEL: 0.01, "exit_date": dates + pd.offsets.BDay(6)}, index=index)
        labels_mask = segment_mask(index, labels, "2024-12-23", "2024-12-31")
        signals = segment_mask(index, labels, "2024-12-23", "2024-12-31", require_label=False)
        self.assertEqual(int(labels_mask.sum()), 1)
        self.assertEqual(int(signals.sum()), 7)

    def test_group_membership_does_not_look_at_future_labels(self):
        index = pd.MultiIndex.from_product([[pd.Timestamp("2024-01-02")], range(10)], names=["date", "symbol"])
        score = pd.Series(np.arange(10), index=index)
        label = pd.Series(.01, index=index)
        label.iloc[-1] = np.nan
        result = group_diagnostics(score, label)
        self.assertEqual(result.loc[result.group.eq(5), "signals"].iloc[0], 2)
        self.assertEqual(result.loc[result.group.eq(5), "observed_labels"].iloc[0], 1)

    def test_train_only_medians(self):
        config = json.loads(DEFAULT_CONFIG.read_text())
        config["model"]["n_estimators"] = 5
        config["early_stopping_rounds"] = 2
        index = pd.MultiIndex.from_product([pd.bdate_range("2024-01-02", periods=10), range(5)], names=["date", "symbol"])
        x = pd.DataFrame({"x": np.arange(50, dtype=float)}, index=index)
        y = pd.Series(np.tile(np.arange(5), 10), index=index, dtype=float)
        valid = x + 10000
        _, medians = fit_model(x, y, valid, y, config, 17)
        self.assertEqual(medians.x, 24.5)

    def test_alpha158_and_template_native_no_future_dependency(self):
        definitions = catalog()
        self.assertEqual(len(definitions), 158)
        self.assertEqual(definitions.set_index("name").at["BETA20", "formula"], "Slope($close, 20)/$close")
        dates = pd.bdate_range("2023-01-02", periods=90)
        frames = []
        rng = np.random.default_rng(1)
        for i in range(5):
            close = 10 + i + rng.normal(0, .05, 90).cumsum()
            frames.append(pd.DataFrame({"date": dates, "symbol": f"60000{i}.SSE", "open": close * .999,
                "high": close * 1.01, "low": close * .99, "close": close, "volume": rng.uniform(10000, 100000, 90),
                "vwap": close, "factor": 1.}))
        panel = pd.concat(frames, ignore_index=True)
        changed = panel.copy()
        future = changed.date.ge(dates[80])
        changed.loc[future, ["open", "high", "low", "close", "vwap", "volume"]] *= 3
        windows = [5, 10, 20, 30, 60]
        with tempfile.TemporaryDirectory() as root:
            a = native_features(panel, dates.strftime("%Y-%m-%d").tolist(), Path(root) / "one", definitions, windows)
            b = native_features(changed, dates.strftime("%Y-%m-%d").tolist(), Path(root) / "two", definitions, windows)
            before = a.index.get_level_values("date") < dates[80]
            pd.testing.assert_frame_equal(a.loc[before], b.loc[before])
            ca, forms = candidates(a, windows)
            cb, _ = candidates(b, windows)
            self.assertEqual(len(forms), 200)
            pd.testing.assert_frame_equal(ca.loc[before], cb.loc[before])
            self.assertTrue(ca["T002"].dropna().eq(0).all())

    def test_existing_ledger_executes_next_session_with_costs(self):
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        symbols = ["600000.SSE"]
        rows = {d: {symbols[0]: {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}} for d in dates}
        base = SimpleNamespace(calendar=dates, rows=rows, actions=[], instruments={symbols[0]: {"ipoDate": "2000-01-01", "outDate": ""}},
                               volumes=pd.DataFrame(1000000., index=dates, columns=symbols),
                               next_day=lambda d: dates[dates.index(d) + 1] if d != dates[-1] else "2024-01-05")
        index = pd.MultiIndex.from_product([pd.to_datetime(dates), symbols], names=["date", "symbol"])
        predictions = export_scores(pd.Series(1., index=index))
        feed = ScoreFeed(base, predictions)
        config = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=1, single_limit=.8)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "replay"
            result = replay(feed, config, dates, path)
            daily = pd.read_csv(path / "daily.csv")
            state = json.loads((path / "account.json").read_text())
            self.assertEqual(daily.new_orders.iloc[0], 0)
            self.assertTrue(all(f["day"] >= dates[1] for f in state["fills"].values()))
            self.assertGreater(result["fees"], 0)
            spent = sum(f["qty"] * f["price"] + f["fees"] for f in state["fills"].values())
            self.assertEqual(state["cash"], 1000000 - spent)
            self.assertGreaterEqual(state["cash"], 0)


if __name__ == "__main__":
    unittest.main()
