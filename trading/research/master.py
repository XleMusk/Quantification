"""Bounded, offline MASTER adaptation feeding the existing historical ledger."""

import argparse
import ast
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..feed import HistoricalFeed
from ..settings import DATA_ROOT
from .experiment import ScoreFeed, export_scores, replay, write_json
from .factors import LABEL, catalog, daily_ic, ic_summary, qcode, segment_mask
from .market_indices import SOURCE_COMMIT, build as market_features, feature_names


SOURCE = DATA_ROOT / "research/primary_sources/MASTER" / SOURCE_COMMIT / "master.py"
SOURCE_HASH = "e5e798952c9636c8c43fc91cff51a6dbbebc009b99d16a1ae090be32d0744fe4"
CLASSES = ("PositionalEncoding", "SAttention", "TAttention", "Gate", "TemporalAttention", "MASTER")
DEFAULT_CONFIG = Path(__file__).with_name("master_replay_20260930.json")


def architecture(source=SOURCE):
    """Load only reviewed architecture definitions, not the external trainer/main."""
    source = Path(source)
    content = source.read_bytes()
    if sha256(content).hexdigest() != SOURCE_HASH:
        raise ValueError("MASTER architecture differs from the reviewed commit")
    tree = ast.parse(content.decode("utf-8"), filename=str(source))
    selected = [node for node in tree.body if (
        isinstance(node, ast.Import) or
        isinstance(node, ast.ImportFrom) and node.module != "base_model" or
        isinstance(node, ast.ClassDef) and node.name in CLASSES)]
    if [node.name for node in selected if isinstance(node, ast.ClassDef)] != list(CLASSES):
        raise ValueError("Unexpected MASTER architecture definitions")
    namespace = {"__name__": "reviewed_master_architecture"}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["MASTER"]


def fit_scaler(values, start, end):
    from qlib.data.dataset.processor import RobustZScoreNorm
    frame = values.copy()
    if isinstance(frame.index, pd.MultiIndex):
        frame.index = frame.index.set_names(["datetime", "instrument"])
    else:
        # Qlib's fit slicer requires two levels; market observations remain one per date.
        frame.index = pd.MultiIndex.from_arrays([frame.index, ["market"] * len(frame)],
                                               names=["datetime", "instrument"])
    processor = RobustZScoreNorm(start, end, clip_outlier=True)
    processor.fit(frame)
    return processor


def scale(values, processor):
    from qlib.data.dataset.processor import Fillna
    result = Fillna()(processor(values.copy()))
    if not np.isfinite(result.to_numpy()).all():
        raise ValueError("Nonfinite normalized features")
    return result.astype(np.float32)


class Sequences:
    """Exact exchange-calendar windows; current eligibility never depends on labels."""
    def __init__(self, stocks, market, eligible, calendar, symbols, step_len=8):
        self.calendar = pd.DatetimeIndex(pd.to_datetime(calendar))
        if self.calendar.has_duplicates or not self.calendar.is_monotonic_increasing:
            raise ValueError("Invalid sequence calendar")
        self.symbols = list(symbols)
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Duplicate sequence symbols")
        self.step_len = step_len
        self.day_lookup = {day: i for i, day in enumerate(self.calendar)}
        self.symbol_lookup = {symbol: i for i, symbol in enumerate(self.symbols)}
        complete = pd.MultiIndex.from_product([self.calendar, self.symbols], names=["date", "symbol"])
        self.stock = stocks.reindex(complete).fillna(0).to_numpy(dtype=np.float32).reshape(
            len(self.calendar), len(symbols), stocks.shape[1])
        self.market = market.reindex(self.calendar).to_numpy(dtype=np.float32)
        if not np.isfinite(self.stock).all() or not np.isfinite(self.market).all():
            raise ValueError("Sequence inputs must be normalized and finite")
        if eligible.has_duplicates or not set(eligible.get_level_values("symbol")) <= set(symbols):
            raise ValueError("Invalid current-day eligibility")
        self.by_day = {}
        for day, group in pd.Series(0, index=eligible).groupby(level="date", sort=True):
            day = pd.Timestamp(day)
            if day not in self.day_lookup or self.day_lookup[day] + 1 < step_len:
                raise ValueError("Eight calendar sessions including warmup required")
            self.by_day[day] = group.index.sort_values()

    def batch(self, day):
        index = self.by_day[pd.Timestamp(day)]
        position = self.day_lookup[pd.Timestamp(day)]
        history = np.arange(position - self.step_len + 1, position + 1)
        positions = [self.symbol_lookup[s] for s in index.get_level_values("symbol")]
        stock = self.stock[history[:, None], positions].transpose(1, 0, 2)
        market = np.broadcast_to(self.market[history], (len(index), self.step_len, self.market.shape[1]))
        return np.concatenate([stock, market], axis=2), index

    def days(self, segment):
        start, end = map(pd.Timestamp, segment)
        return [day for day in self.by_day if start <= day <= end]


def training_targets(index, labels, segment_end):
    rows = labels.reindex(index)
    values = rows[LABEL].to_numpy(dtype=float)
    mask = np.isfinite(values) & rows.exit_date.le(pd.Timestamp(segment_end)).to_numpy()
    positions = np.flatnonzero(mask)
    count = int(0.025 * len(positions))
    if count:
        ranked = np.argsort(values[positions], kind="stable")
        keep = positions[ranked[count:-count]]
        mask[:] = False
        mask[keep] = True
    target = values[mask]
    if len(target) < 2 or not np.isfinite(target.std(ddof=1)) or target.std(ddof=1) <= 0:
        return None
    target = (target - target.mean()) / target.std(ddof=1)
    return mask, target.astype(np.float32)


def full_stock_features(provider, calendar, symbols):
    import qlib
    from qlib.data import D
    definitions = catalog()
    qlib.init(provider_uri=str(provider), region="cn", expression_cache=None, dataset_cache=None,
              joblib_backend="threading", kernels=2)
    frame = D.features([qcode(s) for s in symbols], definitions.formula.tolist(),
                       start_time=calendar[0], end_time=calendar[-1])
    frame.columns = definitions.name.tolist()
    frame = frame.reset_index().rename(columns={"datetime": "date", "instrument": "symbol"})
    frame.symbol = frame.symbol.map({qcode(s): s for s in symbols})
    if frame.symbol.isna().any():
        raise ValueError("Unrecognized sequence instrument")
    return frame.set_index(["date", "symbol"]).sort_index().replace([np.inf, -np.inf], np.nan)


def prepare(config, feed, out):
    feature_run, market_run = Path(config["feature_run"]), Path(config["market_run"])
    stocks = pd.read_parquet(feature_run / "features_and_candidates.parquet")
    labels = pd.read_parquet(feature_run / "labels.parquet")
    if (stocks.columns.tolist() != catalog().name.tolist() or stocks.index.names != ["date", "symbol"]
            or stocks.index.has_duplicates or labels.index.has_duplicates):
        raise ValueError("Expected existing native Alpha158 and dated labels")
    if (feature_run / "qlib_provider/calendars/day.txt").read_text().splitlines() != feed.calendar:
        raise ValueError("Warmup provider calendar differs from the pinned snapshot")
    portfolio = StrategyConfig(**config["portfolio"]).validate()
    # Validate eligible rows and labels against this same snapshot, not a similarly named run.
    from .factors import research_panel
    _, expected_labels, eligible, _ = research_panel(feed, portfolio)
    dates = stocks.index.get_level_values("date")
    expected_index = eligible.index[eligible & (eligible.index.get_level_values("date") >= dates.min())
                                    & (eligible.index.get_level_values("date") <= dates.max())]
    if not stocks.index.equals(expected_index.sort_values()) or not labels.index.equals(expected_labels.index):
        raise ValueError("Stored features/labels do not identify this snapshot's eligible rows")
    if not np.allclose(labels[LABEL], expected_labels[LABEL], equal_nan=True, rtol=1e-10, atol=1e-10):
        raise ValueError("Stored future labels differ from the fixed open-five-session definition")
    if not labels[["entry_date", "exit_date"]].equals(expected_labels[["entry_date", "exit_date"]]):
        raise ValueError("Stored label realization dates differ")
    market = market_features(market_run)
    if not pd.DatetimeIndex(market.index).equals(pd.DatetimeIndex(pd.to_datetime(feed.calendar))):
        raise ValueError("Market calendar differs from stock snapshot")
    train = config["segments"]["train"]
    stock_scaler, market_scaler = fit_scaler(stocks, *train), fit_scaler(market, *train)
    full = full_stock_features(feature_run / "qlib_provider", feed.calendar, feed.symbols)
    if not np.allclose(full.reindex(stocks.index), stocks, equal_nan=True, rtol=1e-6, atol=1e-7):
        raise ValueError("Warmup provider/current stock features differ")
    write_json(out / "scalers.json", {
        "fit_segment": train, "stock_names": stocks.columns.tolist(), "market_names": feature_names(),
        "stock_center": stock_scaler.mean_train.tolist(), "stock_scale": stock_scaler.std_train.tolist(),
        "market_center": market_scaler.mean_train.tolist(), "market_scale": market_scaler.std_train.tolist(),
        "stock_fit_rows": int(((dates >= pd.Timestamp(train[0])) & (dates <= pd.Timestamp(train[1]))).sum()),
        "market_fit_dates": len(market.loc[train[0]:train[1]]),
        "fit_labels_used": False, "processor": "installed_Qlib_RobustZScoreNorm_clip3_then_Fillna0",
        "all_nan_stock_features": stocks.columns[np.isnan(stock_scaler.mean_train)].tolist()})
    sequences = Sequences(scale(full, stock_scaler), scale(market, market_scaler), stocks.index,
                          feed.calendar, feed.symbols, config["step_len"])
    write_json(out / "inputs.json", {
        "eligible_rows": len(stocks), "eligible_symbols": int(stocks.index.get_level_values("symbol").nunique()),
        "history_symbols": len(feed.symbols), "calendar_sessions": len(feed.calendar),
        "source_sha256": SOURCE_HASH,
        "files": {str(path): sha256(path.read_bytes()).hexdigest() for path in (
            Path(__file__),
            feature_run / "features_and_candidates.parquet", feature_run / "labels.parquet",
            market_run / "market_features.parquet", feed.folder / "manifest.json")},
        "sequence_rule": "exact eight exchange-calendar sessions, no forward/backward fill",
        "inference_row_filter": "current-day eligibility only; future labels never consulted"})
    return sequences, labels


def predict_all(model, sequences, days, device):
    import torch
    pieces = []
    model.eval()
    with torch.no_grad():
        for day in days:
            values, index = sequences.batch(day)
            predictions = model(torch.from_numpy(values).to(device)).detach().cpu().numpy()
            if not np.isfinite(predictions).all():
                raise ValueError("Nonfinite MASTER predictions")
            pieces.append(pd.Series(predictions, index=index, name="score"))
    if not pieces:
        raise ValueError("No eligible prediction dates")
    return pd.concat(pieces).sort_index()


def train(model, sequences, labels, config, out, device):
    import torch
    training = config["training"]
    optimizer = torch.optim.Adam(model.parameters(), lr=training["learning_rate"])
    rng = np.random.default_rng(config["seed"])
    days = sequences.days(config["segments"]["train"])
    batches = []
    skipped = []
    for day in days:
        values, index = sequences.batch(day)
        targets = training_targets(index, labels, config["segments"]["train"][1])
        if targets is None:
            skipped.append(str(day.date()))
        else:
            mask, target = targets
            batches.append((torch.from_numpy(values[mask]), torch.from_numpy(target)))
    if not batches:
        raise ValueError("No finite, nonconstant purged daily training batches")
    history, started = [], time.monotonic()
    status = "epoch_limit_criterion_unmet"
    for epoch in range(training["max_epochs"]):
        losses = []
        model.train()
        complete = True
        for i in rng.permutation(len(batches)):
            if time.monotonic() - started > training["wall_seconds"]:
                complete = False
                status = "wall_limit_criterion_unmet"
                break
            x, y = batches[i]
            optimizer.zero_grad()
            prediction = model(x.to(device))
            loss = torch.mean((prediction - y.to(device)) ** 2)
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite MASTER training loss")
            loss.backward()
            torch.nn.utils.clip_grad_value_(model.parameters(), 3.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        row = {"epoch": epoch + 1, "mean_daily_train_mse": float(np.mean(losses)) if losses else None,
               "daily_batches": len(losses), "epoch_complete": complete,
               "elapsed_seconds": time.monotonic() - started}
        history.append(row)
        write_json(out / "training_history.json", history)
        print(json.dumps(row), flush=True)
        if not complete:
            break
        if row["mean_daily_train_mse"] <= training["stop_train_loss"]:
            status = "train_loss_criterion_met"
            break
    torch.save({name: value.detach().cpu() for name, value in model.state_dict().items()}, out / "weights.pt")
    result = {"status": status, "epochs_attempted": len(history), "elapsed_seconds": time.monotonic() - started,
              "training_days": len(batches), "skipped_label_days": skipped,
              "validation_checkpoint_selection": False, "test_feedback_used": False,
              "weights_sha256": sha256((out / "weights.pt").read_bytes()).hexdigest()}
    write_json(out / "training.json", result)
    return result


def run(config_path=DEFAULT_CONFIG):
    import torch
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    out = DATA_ROOT / "research/master" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "protocol.json", config)
    result = {"status": "preparing", "execution_admitted": False, "live_ready": False,
              "protocol_id": config["protocol_id"], "training": None, "metrics": None,
              "scope": config["scope"], "limitations": config["disclosure"]}
    write_json(out / "result.json", result)
    print(f"MASTER output: {out}", flush=True)
    try:
        if config["scope"] != "historical_engineering_diagnostic_NOT_strategy_validation":
            raise ValueError("This adapter is diagnostic only")
        feed = HistoricalFeed(config["dataset"])
        previous = None
        for name in ("train", "valid", "test"):
            start, end = map(pd.Timestamp, config["segments"][name])
            if (start > end or previous is not None and start <= previous
                    or start <= pd.Timestamp(feed.manifest["selection_date"])
                    or end > pd.Timestamp(feed.calendar[-1])):
                raise ValueError("Invalid disjoint chronological segments")
            previous = end
        if config["step_len"] != 8 or config["label"] != "adjusted_open_T_plus_6_over_T_plus_1_minus_1":
            raise ValueError("Unsupported sequence/label adaptation")
        torch.set_num_threads(2)
        torch.manual_seed(config["seed"])
        np.random.seed(config["seed"])
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        if device.type == "cuda":
            torch.cuda.manual_seed_all(config["seed"])
        result["runtime"] = {"torch": torch.__version__, "device": str(device),
                             "gpu": torch.cuda.get_device_name(0) if device.type == "cuda" else None,
                             "bitwise_cross_runtime_reproducibility_guaranteed": False}
        model = architecture()(**config["model"]).to(device)
        sequences, labels = prepare(config, feed, out)
        result["status"] = "training"
        write_json(out / "result.json", result)
        training = train(model, sequences, labels, config, out, device)
        result["training"] = training
        if training["status"] != "train_loss_criterion_met":
            result["status"] = training["status"]
            write_json(out / "result.json", result)
            return out, result
        result["rank_ic"] = {}
        for name in ("valid", "test"):
            score = predict_all(model, sequences, sequences.days(config["segments"][name]), device)
            exported = export_scores(score)
            exported.to_parquet(out / f"{name}_scores.parquet", index=False)
            label = labels[LABEL].where(segment_mask(labels.index, labels, *config["segments"][name]))
            ic = daily_ic(score.to_frame(), label)
            ic.to_csv(out / f"{name}_daily_rank_ic.csv")
            result["rank_ic"][name] = {**ic_summary(ic.score), "scores": len(score),
                                      "symbols": int(score.index.get_level_values("symbol").nunique())}
            if name == "test":
                dates = [day for day in feed.calendar if config["segments"][name][0] <= day <= config["segments"][name][1]]
                portfolio = StrategyConfig(**config["portfolio"]).validate()
                result["metrics"] = replay(ScoreFeed(feed, exported), portfolio, dates, out / "replay")
        result["status"] = "completed_historical_diagnostic"
        write_json(out / "result.json", result)
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", result)
        raise
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    out, result = run(args.config)
    print(json.dumps({"out": str(out), "status": result["status"], "metrics": result["metrics"],
                      "execution_admitted": False}), flush=True)
    return 0 if result["status"] == "completed_historical_diagnostic" else 2


if __name__ == "__main__":
    raise SystemExit(main())
