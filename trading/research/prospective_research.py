"""Freeze three existing research accounts and consume only subsequent complete sessions."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import shutil

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..report import metrics as portfolio_metrics
from ..settings import DATA_ROOT, ROOT
from ..worker import lock, market_now
from .alpha158_linear import SEEDS, equal_seed_ranks
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import dated_eligibility, native_binding
from .experiment import ScoreFeed, baseline_ledger_result, export_scores, load_replay_checkpoint, write_json
from .factors import catalog
from .family_portfolios import FamilyFeed, NATIVE_COLUMNS, read_native_monthends
from .low_risk import features as low_risk_features
from .monthly_timing import month_ends


ID = "fixed_three_account_prospective_diagnostic_v1"
RULES = ("stock_low_vol_control", "stock_low_vol_half_momentum_control")
MODEL = "alpha158_allseed_rank_mean"
END = "2026-12-31"
TERMINAL_POLICY = "Defer the terminal source session until a following exchange session confirms monthly boundaries"
LIMITS = [
    "Prospectively registered diagnostic snapshot REPLAY, not admitted paper/live/broker execution; NO_TRADE",
    "Original research cash/positions are carried, not reset to140000 or bound to the user's THS account",
    "Only complete exchange sessions after registration may enter; pre-registration gaps cannot become forward results",
    "Inputs remain retrieval-time/reconstructed and not independently PIT-verified; no untouched-holdout admission",
    "Two monthly20-stock rules and one five-session5-stock model have different predeclared portfolios",
    "Three separate existing research hypotheses share the same Engine/Ledger implementation; no NAV averaging",
    "The all-seed model is a failed diagnostic control, not a selected best seed or a profitable live candidate",
    "Native model inputs are supplied from the pinned provider; no training, new labels or parameter search",
    "Old economic prefixes must remain exact; revisions require separate historical repair, not invisible forward rewriting",
    "Record receipt times; next-open fills are still simulated, not observed broker execution or timely-feed proof",
    "Terminal source sessions await calendar confirmation; this delayed diagnostic is not a timely daily trading service",
    "30-60sessions can validate operations, not establish long-term returns or black-swan protection",
]


def folder(name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError("Use a short plain suite name, not a path")
    return DATA_ROOT / "research/prospective_research" / name


def publish_head(root, value):
    temporary = root / "head.json.tmp"
    write_json(temporary, value)
    temporary.replace(root / "head.json")


def incoming_dates(calendar, last_day, frozen_day, current):
    if calendar != sorted(set(calendar)) or last_day not in calendar:
        raise ValueError("Source must contain the saved account calendar endpoint")
    cutoff = current.date().isoformat()
    selected = [day for day in calendar if last_day < day <= END
                and (day < cutoff or day == cutoff and (current.hour, current.minute) >= (16, 15))]
    if selected and selected[0] <= frozen_day:
        raise ValueError("Unprocessed pre-registration sessions require historical repair, not forward relabeling")
    # The original monthly rule requires a following exchange-calendar session.
    return [day for day in selected if day != calendar[-1]]


def same_frame(before, after, keys, label):
    if before.columns.tolist() != after.columns.tolist():
        raise ValueError(label + " schema changed")
    old = before.sort_values(keys).reset_index(drop=True)
    new = after.sort_values(keys).reset_index(drop=True)
    if not old.equals(new):
        raise ValueError(label + " changed; repair separately before continuation")


def unchanged_daily_prefix(old_path, new_path, last_day):
    if old_path.resolve() == new_path.resolve():
        return
    with pq.ParquetFile(old_path) as old, pq.ParquetFile(new_path) as new:
        if new.num_row_groups < old.num_row_groups or old.schema.names != new.schema.names:
            raise ValueError("Daily snapshot removed row groups or changed schema")
        for index in range(old.num_row_groups):
            first = old.read_row_group(index).to_pandas()
            if first.empty or first.date.nunique() != 1:
                raise ValueError("Expected one complete date per source row group")
            if first.date.iloc[0] > last_day:
                break
            second = new.read_row_group(index).to_pandas()
            same_frame(first, second, ["date", "code"], "Past daily quotes")


def validate_extension(before, after, last_day):
    """Explicit extension admission; the ordinary same-path resume guard is unchanged."""
    for key in ("scope", "source_codes", "canonical_codes", "unsupported_instruments", "selection_date"):
        if before[key] != after[key]:
            raise ValueError("Frozen source membership changed: " + key)
    if not after.get("replay_only") or after.get("execution_admitted") is not False:
        raise ValueError("Prospective diagnostic cannot promote a source's execution mode")
    for key, file in (("price_view", "canonical_daily.parquet"), ("raw_view", "raw_daily.parquet")):
        unchanged_daily_prefix(Path(before[key]) / file, Path(after[key]) / file, last_day)
    old = pd.read_csv(before["listing_master"], dtype=str, keep_default_na=False)
    new = pd.read_csv(after["listing_master"], dtype=str, keep_default_na=False)
    same_frame(old, new, ["code"], "Frozen listing metadata")
    old_actions = pd.read_parquet(Path(before["event_view"]) / "canonical_actions.parquet").fillna("")
    new_actions = pd.read_parquet(Path(after["event_view"]) / "canonical_actions.parquet").fillna("")
    def past_actions(frame):
        # A new future ex-date with a past registration could already change entitlements.
        return frame.loc[frame.dividOperateDate.le(last_day) | frame.dividRegistDate.le(last_day)]
    same_frame(past_actions(old_actions), past_actions(new_actions), ["symbol", "dividOperateDate"], "Past action/registration terms")
    old_ref, new_ref = Path(before["reference_view"]), Path(after["reference_view"])
    old_special = json.loads((old_ref / "reviewed_special_references.json").read_text(encoding="utf-8"))
    new_special = json.loads((new_ref / "reviewed_special_references.json").read_text(encoding="utf-8"))
    if old_special != new_special:
        raise ValueError("Special holder/rights accounting changed")
    def reference_prefix(path):
        # Filter typed rows before pandas inference: a future null must not
        # turn an unchanged past numeric column into an object column.
        # Null dates retain the old fillna("") <= cutoff inclusion semantics.
        table = pq.read_table(path)
        mask = pc.less_equal(pc.fill_null(table["date"], ""), last_day)
        return table.filter(mask).to_pandas().fillna("")
    old = reference_prefix(old_ref / "raw_reference_changes.parquet")
    new = reference_prefix(new_ref / "raw_reference_changes.parquet")
    same_frame(old, new, ["code", "date"], "Past price references")


def register(name, family_id, model_id, current=None):
    current = current or market_now()
    if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) for identifier in (family_id, model_id)):
        raise ValueError("Source run IDs must be plain directory names")
    sources = {"family": str(ROOT / "outputs/market_replay" / family_id / "run.json"),
               "model": str(ROOT / "outputs/market_replay" / model_id / "run.json")}
    family, model = (json.loads(Path(sources[key]).read_text(encoding="utf-8")) for key in ("family", "model"))
    if (family.get("status") != "completed_family_portfolio_diagnostics"
            or model.get("status") != "completed_alpha158_linear_diagnostics"
            or any(source.get("execution_admitted") is not False for source in (family, model))
            or family["end"] != model["end"] or not family["end"] < current.date().isoformat() <= END):
        raise ValueError("Completed matching endpoint research accounts required before registration")
    root = folder(name)
    root.mkdir(parents=True, exist_ok=True)
    with lock(root / "runner.lock"):
        if (root / "protocol.json").exists():
            prior = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
            if prior["source_runs"] != sources or prior["id"] != ID:
                raise ValueError("Suite already registered with different sources")
            return root, json.loads((root / "head.json").read_text(encoding="utf-8"))
        cases = []
        for signal, source in [(signal, family) for signal in RULES] + [(MODEL, model)]:
            config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][signal]).validate()
            result = next(row for row in source["results"] if row["signal"] == signal)
            if result["status"] != "completed" or result["completed_sessions"] != source["sessions"]:
                raise ValueError("Each fixed account must have a complete terminal checkpoint")
            manifest = source["protocol"]["stock_manifest"]
            calendar = pd.read_csv(Path(manifest["event_view"]) / "source_input_calendar.csv", dtype=str).date.tolist()
            dates = [day for day in calendar if source["start"] <= day <= source["end"]]
            account_folder = Path(source["artifacts"]) / signal
            checkpoint = load_replay_checkpoint(account_folder, config, dates)
            cases.append({"signal": signal, "recipe": result.get("recipe"), "config": config.to_dict(),
                "start": source["start"], "anchor_day": checkpoint["state"]["day"],
                "anchor_equity": checkpoint["daily"][-1]["equity"], "source_account": str(account_folder),
                "checkpoint": str(account_folder), "completed_sessions": len(dates), "manifest": manifest,
                "status": "waiting_for_new_complete_data", "forward_sessions": 0})
        original = json.loads(Path(model["source_run"]).read_text(encoding="utf-8"))
        for seed in SEEDS:
            source = Path(original["model_sources"][f"alpha158_seed{seed}"])
            destination = root / "models" / str(seed)
            destination.mkdir(parents=True, exist_ok=True)
            for filename in ("model.txt", "preprocessing.csv"):
                shutil.copy2(source / filename, destination / filename)
        catalog().to_csv(root / "alpha158_catalog.csv", index=False)
        shutil.copy2(__file__, root / "implementation.py")
        protocol = {"id": ID, "name": name, "registered_at": current.isoformat(),
            "frozen_day": current.date().isoformat(), "end": END, "source_runs": sources,
            "signals": list(RULES) + [MODEL], "cases": deepcopy(cases),
            "default_inputs": model["protocol"]["stock_inputs"],
            "training_directions": family["protocol"]["training_directions"],
            "terminal_session_policy": TERMINAL_POLICY,
            "mode": "prospective_diagnostic_replay", "account_reset": False,
            "execution_admitted": False, "fresh_holdout_claimed": False, "limits": LIMITS}
        head = {"id": ID, "name": name, "status": "waiting_for_new_complete_data",
                "cases": cases, "last_batch": None, "execution_admitted": False, "forward_sessions": 0}
        write_json(root / "protocol.json", protocol)
        publish_head(root, head)
        report(root, protocol, head)
        return root, head


def predict_new_native(feed, native_view, dates, root, config):
    definitions = catalog()
    if not pd.read_csv(root / "alpha158_catalog.csv", keep_default_na=False).equals(definitions):
        raise ValueError("Frozen feature definitions changed")
    native_view, receipt, _ = native_binding(feed, native_view, definitions)
    wanted_days = pd.to_datetime(dates)
    eligibility = dated_eligibility(feed, config, dates=dates)
    models, medians = {}, {}
    for seed in SEEDS:
        model_folder = root / "models" / str(seed)
        models[seed] = lgb.Booster(model_file=str(model_folder / "model.txt"))
        medians[seed] = pd.read_csv(model_folder / "preprocessing.csv", index_col=0).train_median.astype("float32")
        if (models[seed].feature_name() != definitions.name.tolist()
                or medians[seed].index.tolist() != definitions.name.tolist()
                or not np.isfinite(medians[seed]).all()):
            raise ValueError("Frozen original model/preprocessing feature identities changed")
    predictions, native_rows = {seed: [] for seed in SEEDS}, []
    for part in receipt["partitions"]:
        path = (native_view / part["file"]).resolve()
        path.relative_to(native_view)
        frame = pd.read_parquet(path)
        expected = pd.MultiIndex.from_product([pd.to_datetime(feed.calendar), part["symbols"]], names=["date", "symbol"]).sort_values()
        if not frame.index.equals(expected) or frame.columns.tolist() != definitions.name.tolist():
            raise ValueError("Native date/symbol/formula grid changed")
        frame = frame.loc[frame.index.get_level_values("date").isin(wanted_days)]
        native_rows.append(frame[list(NATIVE_COLUMNS)])
        selected = frame.loc[eligibility.reindex(frame.index, fill_value=False)]
        if np.isinf(selected.to_numpy()).any():
            raise ValueError("Infinite native features cannot enter original model inference")
        for seed in SEEDS:
            values = selected.fillna(medians[seed])
            result = models[seed].predict(values, num_threads=2) if len(values) else np.array([])
            if not np.isfinite(result).all():
                raise ValueError("Nonfinite original model inference")
            predictions[seed].append(pd.Series(result, index=selected.index, name="score"))
    scores = {seed: pd.concat(parts).sort_index() for seed, parts in predictions.items()}
    score = equal_seed_ranks(scores)
    if set(score.index.get_level_values("date")) != set(wanted_days):
        raise ValueError("A new model day has no complete dated score batch")
    return export_scores(score), pd.concat(native_rows).sort_index()


def report(root, protocol, head):
    out = ROOT / "outputs/prospective_research" / protocol["name"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", {**head, "protocol": str(root / "protocol.json"), "artifacts": str(root)})
    lines = ["# Fixed Prospective Research Intake", "", f"Status: {head['status']}. NO_TRADE.",
             f"Registered: {protocol['registered_at']}; original account anchor: {protocol['cases'][0]['anchor_day']}.",
             "Original research accounts carried, no cash reset. No THS connection or main simulation worker started.", "",
             "|Case|Original anchor equity|New observed sessions|Last saved day|State|", "|---|---:|---:|---|---|"]
    for case in head["cases"]:
        lines.append(f"|{case['signal']}|{case['anchor_equity']:.2f}|{case['forward_sessions']}|"
                     f"{case.get('last_day', case['anchor_day'])}|{case['status']}|")
        if case.get("error"):
            lines.append("Failure: " + case["error"])
    if head.get("reason"):
        lines += ["", head["reason"]]
    if head.get("error"):
        lines += ["", "Preparation failure: " + head["error"]]
    lines += ["", *["- " + value for value in LIMITS], "", f"Artifacts: {root}"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def advance_suite(name, inputs=None, current=None):
    current = current or market_now()
    root = folder(name)
    with lock(root / "runner.lock"):
        protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
        head = json.loads((root / "head.json").read_text(encoding="utf-8"))
        if (protocol["id"] != ID or protocol["signals"] != list(RULES) + [MODEL]
                or protocol.get("terminal_session_policy") != TERMINAL_POLICY
                or head["id"] != ID or head["execution_admitted"] is not False):
            raise ValueError("Frozen unbound three-case diagnostic suite required")
        if [case["signal"] for case in head["cases"]] != protocol["signals"]:
            raise ValueError("Frozen case order/identities changed")
        for original, case in zip(protocol["cases"], head["cases"]):
            if any(original[key] != case[key] for key in ("signal", "recipe", "config", "start", "anchor_day", "anchor_equity", "source_account")):
                raise ValueError("Frozen case policy/anchor changed")
        feed, batch, record = None, None, None
        try:
            inputs = dict(inputs or protocol["default_inputs"])
            if not all(inputs.get(key) for key in ("price_view", "event_view", "reference_view", "listing_master", "native_view")):
                raise ValueError("Complete price/event/reference/listing/native input bundle required")
            price_receipt = json.loads((Path(inputs["price_view"]) / "result.json").read_text(encoding="utf-8"))
            if price_receipt.get("status") != "research_reconstruction_completed":
                raise ValueError("A completed canonical price snapshot is required")
            cutoff = price_receipt["cutoff"]
            if cutoff > current.date().isoformat():
                raise ValueError("Source snapshot contains future dates relative to this intake")
            if cutoff <= min(case.get("last_day", case["anchor_day"]) for case in head["cases"]):
                head.update(status="waiting_for_new_complete_data", reason=f"Latest supplied complete data ends {cutoff}; no new account sessions, orders or fills.")
                head.pop("error", None)
                publish_head(root, head)
                report(root, protocol, head)
                return root, head
            batch = root / "batches" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            batch.mkdir(parents=True, exist_ok=False)
            record = {"status": "preparing", "received_at": current.isoformat(), "inputs": inputs,
                      "mode": "prospective_diagnostic_replay", "results": [], "execution_admitted": False}
            write_json(batch / "run.json", record)
            feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
            available = incoming_dates(feed.calendar, protocol["cases"][0]["anchor_day"], protocol["frozen_day"], current)
            if not available:
                record["status"] = "waiting_for_calendar_confirmation"
                write_json(batch / "run.json", record)
                head.update(status=record["status"], last_batch=str(batch),
                            reason="No post-registration sessions confirmed by both the16:15 EOD cutoff and a following exchange-calendar session.")
                head.pop("error", None)
                publish_head(root, head)
                report(root, protocol, head)
                return root, head
            end = available[-1]
            new_dates = sorted({day for case in head["cases"] for day in available
                                if day > case.get("last_day", case["anchor_day"])})
            if not new_dates:
                return root, head
            preparation_errors = {}
            model_case = next(case for case in head["cases"] if case["signal"] == MODEL)
            model_dates = [day for day in available if day > model_case.get("last_day", model_case["anchor_day"])]
            if model_dates:
                try:
                    model_config = StrategyConfig(**model_case["config"]).validate()
                    predictions, _ = predict_new_native(feed, inputs["native_view"], model_dates, root, model_config)
                    predictions.to_parquet(batch / "historical_scores.parquet", index=False)
                except Exception as error:
                    preparation_errors[MODEL] = f"{type(error).__name__}: {error}"
            rule_cases = [case for case in head["cases"] if case["signal"] in RULES
                          and case.get("last_day", case["anchor_day"]) < end]
            if rule_cases:
                try:
                    values = low_risk_features(feed, end)
                    last_rule_day = min(case.get("last_day", case["anchor_day"]) for case in rule_cases)
                    rule_dates = [day for day in month_ends(feed.calendar, end) if day > last_rule_day]
                    # Keep the original shared native-feature pool without depending on model inference.
                    native = (read_native_monthends(feed, inputs["native_view"], rule_dates) if rule_dates else
                              pd.DataFrame(columns=NATIVE_COLUMNS, index=pd.MultiIndex.from_arrays(
                                  [pd.DatetimeIndex([]), []], names=["date", "symbol"])))
                except Exception as error:
                    preparation_errors.update({case["signal"]: f"{type(error).__name__}: {error}"
                                               for case in rule_cases})
            record.update(status="running", end=end)
            record["preparation_errors"] = preparation_errors
            write_json(batch / "run.json", record)
            for case in head["cases"]:
                last_day = case.get("last_day", case["anchor_day"])
                if last_day >= end:
                    continue
                if case["signal"] in preparation_errors:
                    message = preparation_errors[case["signal"]]
                    result = {"status": "failed_preparation", "error": message,
                              "completed_sessions": case["completed_sessions"]}
                    case.update(status="failed_preparation", error=message)
                    record["results"].append({"signal": case["signal"], **result})
                    write_json(batch / "run.json", record)
                    head.update(status="partial", last_batch=str(batch),
                                forward_sessions=min(row["forward_sessions"] for row in head["cases"]))
                    publish_head(root, head)
                    continue
                config = StrategyConfig(**case["config"]).validate()
                dates = [day for day in feed.calendar if case["start"] <= day <= end]
                try:
                    checkpoint = load_replay_checkpoint(Path(case["checkpoint"]), config, dates)
                    validate_extension(case["manifest"], feed.manifest, last_day)
                    active = (ScoreFeed(feed, predictions) if case["signal"] == MODEL else
                              FamilyFeed(feed, values, native, case["recipe"], protocol["training_directions"], end))
                    result = baseline_ledger_result(active, config, dates, batch / case["signal"], checkpoint=checkpoint)
                    saved = load_replay_checkpoint(batch / case["signal"], config, dates)
                    case.update(checkpoint=str(batch / case["signal"]), last_day=saved["state"]["day"],
                                completed_sessions=len(saved["daily"]), manifest=deepcopy(feed.manifest), status=result["status"])
                    observed = [row for row in saved["daily"] if row["date"] > case["anchor_day"]]
                    case["forward_sessions"] = len(observed)
                    case["forward_metrics"] = portfolio_metrics(observed, case["anchor_equity"]) if observed else None
                    case.pop("error", None)
                    if result.get("error"):
                        case["error"] = result["error"]
                except Exception as error:
                    result = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
                    case.update(status="failed", error=result["error"])
                record["results"].append({"signal": case["signal"], **result})
                write_json(batch / "run.json", record)
                # Persist each account's saved cursor so interruption cannot reset a successful case.
                head.update(status="partial", last_batch=str(batch),
                            forward_sessions=min(row["forward_sessions"] for row in head["cases"]))
                publish_head(root, head)
            record["status"] = "completed" if all(case["status"] == "completed"
                and case.get("last_day", case["anchor_day"]) >= end for case in head["cases"]) else "partial"
            write_json(batch / "run.json", record)
            head.update(status=record["status"], last_batch=str(batch), reason=None,
                        forward_sessions=min(case["forward_sessions"] for case in head["cases"]))
            head.pop("error", None)
            publish_head(root, head)
            report(root, protocol, head)
            return root, head
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            if batch is None:
                batch = root / "batches" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                batch.mkdir(parents=True, exist_ok=False)
                record = {"received_at": current.isoformat(), "inputs": inputs, "results": [],
                          "mode": "prospective_diagnostic_replay", "execution_admitted": False}
            record.update(status="failed_preparation", error=message)
            write_json(batch / "run.json", record)
            head.update(status="failed_preparation", error=message, last_batch=str(batch))
            publish_head(root, head)
            report(root, protocol, head)
            return root, head
        finally:
            if feed is not None:
                feed.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    registration = sub.add_parser("register")
    registration.add_argument("--name", required=True)
    registration.add_argument("--family-run", required=True)
    registration.add_argument("--model-run", required=True)
    advance = sub.add_parser("advance")
    advance.add_argument("--name", required=True)
    advance.add_argument("--inputs", type=Path, help="JSON containing the completed price/event/reference/listing/native views")
    args = parser.parse_args(argv)
    if args.command == "register":
        root, result = register(args.name, args.family_run, args.model_run)
    else:
        inputs = json.loads(args.inputs.read_text(encoding="utf-8")) if args.inputs else None
        root, result = advance_suite(args.name, inputs)
    print(json.dumps({"artifacts": str(root), "status": result["status"], "forward_sessions": result["forward_sessions"]}), flush=True)
    return 1 if result["status"] in {"failed", "partial", "failed_preparation"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
