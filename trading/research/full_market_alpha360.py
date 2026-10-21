"""Research-only Alpha360 signal diagnostics on an existing frozen Qlib provider."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .full_market import verify_chunks
from .full_market_alpha158 import (BATCH, FORWARD, FIRST, decision_dates, evaluate_both,
                                   fetch_features, qcode, rank_summary)
from .full_market_mix import END, MARKET_GROUPS, load_rows, market_panel


def catalog360():
    from qlib.contrib.data.loader import Alpha360DL

    formulas, names = Alpha360DL.get_feature_config()
    if len(names) != 360 or len(set(names)) != 360:
        raise ValueError("Expected the installed default Alpha360 definition")
    return pd.DataFrame({"name": names, "formula": formulas})


def dual_positive_count(summary):
    good = summary.loc[summary.mean_rank_ic.gt(0) & summary.mean_top_minus_universe.gt(0)]
    return int(good.groupby(["factor", "direction"]).segment.nunique().eq(2).sum())


def vectorized_scores(frame, pct, eligible, label, decisions, definitions, checkpoint, batch_size):
    calendar = pct.index.tolist()
    positions = {date: i for i, date in enumerate(calendar)}
    if not decisions or min(positions[date] for date, _ in decisions) < 59:
        raise ValueError("Alpha360 decisions require at least 59 prior sessions")
    close = frame.pivot(index="date", columns="code", values="close").reindex_like(pct)
    volume = frame.pivot(index="date", columns="code", values="volume").reindex_like(pct)
    live = close.gt(0) & volume.gt(0)
    index = (1 + pct.fillna(0)).cumprod().where(pct.notna())
    adjusted_close = index.where(live).astype("float32")
    adjusted_volume = volume.where(live).astype("float32")
    rows = []
    active_field = source = None
    for number, item in enumerate(definitions.itertuples(index=False), start=1):
        match = re.fullmatch(r"(CLOSE|OPEN|HIGH|LOW|VWAP|VOLUME)(\d+)", item.name)
        if match is None:
            raise ValueError(f"Unrecognized Alpha360 definition: {item.name}")
        field, lag_text = match.groups()
        lag = int(lag_text)
        numerator = f"Ref(${field.lower()}, {lag})" if lag else f"${field.lower()}"
        denominator = "($volume+1e-12)" if field == "VOLUME" else "$close"
        if item.formula != numerator + "/" + denominator:
            raise ValueError(f"Alpha360 formula changed: {item.name}")
        if field != active_field:
            if field == "CLOSE":
                source = adjusted_close
            elif field == "VOLUME":
                source = adjusted_volume
            else:
                raw = (frame.pivot(index="date", columns="code", values="amount") / volume
                       if field == "VWAP" else
                       frame.pivot(index="date", columns="code", values=field.lower()))
                source = (index * raw.reindex_like(pct) / close).where(live).astype("float32")
            active_field = field
        denominator_values = adjusted_volume + 1e-12 if field == "VOLUME" else adjusted_close
        for day, segment in decisions:
            score = source.iloc[positions[day] - lag] / denominator_values.loc[day]
            allowed = eligible.loc[day]
            forward = label.loc[day]
            for direction, pairs, missing, ic, spread in evaluate_both(score, forward, allowed):
                rows.append({"date": day, "segment": segment, "factor": item.name,
                             "direction": direction, "eligible": int(allowed.sum()),
                             "pairs": pairs, "missing_forward_labels": missing,
                             "rank_ic": ic, "top_minus_universe": spread})
        if number % batch_size == 0 or number == len(definitions):
            checkpoint(rows, number)
    return rows


def run(alpha_run, out=None, batch_size=BATCH, engine="qlib"):

    alpha_run = Path(alpha_run)
    previous = json.loads((alpha_run / "protocol.json").read_text(encoding="utf-8"))
    if previous["status"] != "completed_research_only" or previous["completed_definitions"] != 158:
        raise ValueError("A completed Alpha158 provider is required")
    snapshot = Path(previous["snapshot"])
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    manifest_hash = sha256(manifest_bytes).hexdigest()
    if manifest_hash != previous["snapshot_manifest_sha256"]:
        raise ValueError("Snapshot changed since the Alpha158 run")
    manifest = json.loads(manifest_bytes)
    verify_chunks(snapshot, manifest)
    group = previous["group"]
    groups = {name: (prefixes, min_amount, name == "bse_equities")
              for name, prefixes, min_amount in MARKET_GROUPS}
    if group not in groups or batch_size < 1 or engine not in {"qlib", "vectorized"}:
        raise ValueError("Unknown market group, batch size or engine")
    prefixes, min_amount, bse = groups[group]
    provider = Path(previous["artifacts"]) / "qlib_provider"
    if not provider.is_dir():
        raise ValueError("Frozen Qlib provider is missing")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8] + "_" + group
    out = Path(out) if out else ROOT / "outputs" / "full_market_alpha360" / identifier
    out.mkdir(parents=True, exist_ok=False)
    definitions = catalog360()
    definitions.to_csv(out / "alpha360_catalog.csv", index=False)
    record = {"status": "running", "alpha158_run": str(alpha_run), "snapshot": str(snapshot),
              "snapshot_manifest_sha256": manifest_hash,
              "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "group": group,
              "definitions": 360, "completed_definitions": 0, "batch_size": batch_size,
              "engine": engine,
              "source": "StockDB_provider_pct_chg_research_proxy", "account_backtest": False,
              "portfolio_proxy_replay": False, "trading_authorized": False,
              "fresh_holdout_claimed": False}
    write_json(out / "protocol.json", record)
    try:
        frame = load_rows(snapshot, manifest, prefixes)
        _, eligible, pct, _, _, _, coverage = market_panel(frame, min_amount, bse=bse)
        calendar = pct.index.tolist()
        index = (1 + pct.fillna(0)).cumprod().where(pct.notna())
        label = index.shift(-FORWARD) / index - 1
        decisions = decision_dates(calendar)
        def checkpoint(rows, completed):
            record["completed_definitions"] = completed
            pd.DataFrame(rows).assign(group=group).to_csv(
                out / "daily_factor_diagnostics.csv", index=False)
            write_json(out / "protocol.json", record)
            print(f"{group} ({engine}): {completed}/360", flush=True)
        if engine == "vectorized":
            rows = vectorized_scores(frame, pct, eligible, label, decisions,
                                     definitions, checkpoint, batch_size)
            for row in rows:
                row["group"] = group
        else:
            import qlib

            del frame
            symbols = [qcode(code) for code in pct.columns]
            qlib.init(provider_uri=str(provider), region="cn", expression_cache=None,
                      dataset_cache=None, joblib_backend="threading", kernels=2)
            rows = []
            for start in range(0, len(definitions), batch_size):
                part = definitions.iloc[start:start + batch_size]
                feature = fetch_features(provider, symbols, part.formula.tolist(), part.name.tolist())
                feature = feature.set_index(["date", "code"])
                for day, segment in decisions:
                    day_scores = feature.loc[day]
                    allowed = eligible.loc[day]
                    forward = label.loc[day]
                    for name in part.name:
                        for direction, pairs, missing, ic, spread in evaluate_both(
                                day_scores[name], forward, allowed):
                            rows.append({"group": group, "date": day, "segment": segment,
                                         "factor": name, "direction": direction,
                                         "eligible": int(allowed.sum()), "pairs": pairs,
                                         "missing_forward_labels": missing,
                                         "rank_ic": ic, "top_minus_universe": spread})
                checkpoint(rows, min(start + batch_size, len(definitions)))
        summary = rank_summary(pd.DataFrame(rows))
        summary.to_csv(out / "factor_summary.csv", index=False)
        evaluated = int(summary.loc[summary.evaluated_dates.gt(0), "factor"].nunique())
        positive = dual_positive_count(summary)
        record.update(status="completed_research_only", panel_coverage=coverage,
                      decision_dates=len(decisions), evaluated_definitions=evaluated,
                      dual_positive_directions=positive)
        lines = ["# Full-market Alpha360 signal diagnostics", "",
                 f"Group: `{group}`; observed codes: {coverage['codes']:,}; source rows: {coverage['source_rows']:,}.",
                 f"Definitions completed: 360/360; evaluable: {evaluated}/360; decision dates: {len(decisions)}.",
                 f"Two-direction, two-segment sign-positive IC and top-decile-excess screens: {positive}/720.",
                 "", "This is a StockDB provider-return signal diagnostic only, with no portfolio proxy",
                 "or engine/ledger account replay. Positive signs do not establish significance, net",
                 "profit or tradability. The 2026 later period was inspected before this run and is",
                 "not an untouched holdout. Volume/VWAP fields, point-in-time membership and",
                 "execution prices remain research-data risks. ETF history is not included.", "",
                 "See `alpha360_catalog.csv`, `daily_factor_diagnostics.csv`, `factor_summary.csv`",
                 "and `protocol.json` for every formula and both directional results."]
        (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(out / "protocol.json", record)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpha-run", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--batch-size", type=int, default=BATCH)
    parser.add_argument("--engine", choices=["qlib", "vectorized"], default="qlib")
    args = parser.parse_args(argv)
    print(run(args.alpha_run, args.out, args.batch_size, args.engine))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
