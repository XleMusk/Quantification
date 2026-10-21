"""Read-only reproduction of an existing trading SQLite replay."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform
import sqlite3
import sys

import numpy as np
import pandas as pd
import pyarrow

from .. import settings
from ..config import StrategyConfig
from ..engine import advance
from ..feed import HistoricalFeed
from ..ledger import new_state


INPUT_FILES = ("manifest.json", "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv")
CODE_FILES = ("trading/config.py", "trading/contracts.py", "trading/engine.py", "trading/feed.py",
              "trading/ledger.py", "trading/rules.py", "trading/broker.py", "trading/simulated_broker.py",
              "trading/research/reproduce_run.py")


def digest(path):
    with Path(path).open("rb") as source:
        hasher = sha256()
        for chunk in iter(lambda: source.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def reproduce(run_id, database=settings.DB_PATH):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        stored = db.execute("SELECT * FROM runs WHERE id=? AND mode='replay'", (run_id,)).fetchone()
        if stored is None:
            raise ValueError("Completed replay run not found")
        if stored["status"] != "completed":
            raise ValueError("Only completed replays can be reproduced")
        expected = {day: json.loads(payload) for day, payload in db.execute(
            "SELECT date,payload FROM daily WHERE run_id=? ORDER BY date", (run_id,))}
        old_state = json.loads(stored["state"])
        config_data = json.loads(stored["config"])
        dataset = Path(stored["dataset"])
        start, end = stored["start"], stored["end"]

    feed = HistoricalFeed(dataset)
    dates = [day for day in feed.calendar if start <= day <= end]
    state = new_state(config_data["initial_cash"])
    config = StrategyConfig(**config_data).validate()
    first_difference = None
    legacy_days_without_dataset = 0
    for index, day in enumerate(dates):
        result = advance(state, feed, day, config, index)
        before = expected.get(day, {})
        if "dataset" in before:
            result["dataset"] = str(feed.folder)
        else:
            legacy_days_without_dataset += 1
        if result != before and first_difference is None:
            first_difference = {"date": day, "fields": [key for key in sorted(set(result) | set(before))
                                                      if key not in result or key not in before or result[key] != before[key]]}
    if set(expected) != set(dates) and first_difference is None:
        first_difference = {"dates": sorted(set(expected) ^ set(dates))[:10]}
    state_matches = state == old_state
    project = Path(__file__).resolve().parents[2]
    report = {
        "work_package": "A02",
        "mode": "historical_replay_reproduction",
        "run_id": run_id,
        "dataset": str(feed.folder),
        "start": start,
        "end": end,
        "sessions": len(dates),
        "config": config_data,
        "input_sha256": {name: digest(feed.folder / name) for name in INPUT_FILES},
        "code_sha256": {name: digest(project / name) for name in CODE_FILES},
        "python": platform.python_version(),
        "interpreter": sys.executable,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "pyarrow": pyarrow.__version__,
        "all_daily_records_match": first_difference is None,
        "final_state_matches": state_matches,
        "legacy_days_without_dataset_metadata": legacy_days_without_dataset,
        "first_difference": first_difference,
        "result": "reproduced" if first_difference is None and state_matches else "mismatch",
        "strategy_validated": False,
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--database", type=Path, default=settings.DB_PATH)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = reproduce(args.run_id, args.database)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("run_id", "sessions", "all_daily_records_match",
                                                 "final_state_matches", "legacy_days_without_dataset_metadata",
                                                 "first_difference", "result")}, ensure_ascii=False))
    return 0 if report["result"] == "reproduced" else 1


if __name__ == "__main__":
    raise SystemExit(main())
