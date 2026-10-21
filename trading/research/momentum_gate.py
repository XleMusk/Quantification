"""Fixed twenty-stock momentum / CSI300 monthly trend paired experiment."""
import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .low_risk import portfolio
from .monthly_timing import index_inputs, month_ends
from .stock_defensive import saved_stock_controls, stage_metrics

CONTROL = "momentum_12_1_20"
VARIANT = CONTROL + "_csi300_10m"
SOURCE_URL = "https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf"
START, END = "2024-01-02", "2026-09-30"


def cached_ranks(run_folder, base, config, start, end):
    """Top retain_rank rows preserve the selector, not a capped stock universe."""
    decisions, ranks, receipts, visited = {}, {}, [], set()
    folder = Path(run_folder).resolve()
    fixed_inputs = ("price_view", "raw_view", "listing_master", "source_codes",
                    "canonical_codes", "unsupported_instruments")
    while folder is not None:
        if folder in visited:
            raise ValueError("Cyclic stock-rank lineage")
        visited.add(folder)
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        cases = [row for row in record["results"] if row["signal"] == CONTROL]
        if (record["status"] != "completed_low_risk_diagnostic"
                or record["research_family"] != "low_risk_stock_selection"
                or record["protocol"]["id"] != "low_risk_price_volume_ablation_v1"
                or record["protocol"]["portfolio"] != config.to_dict()
                or any(record["inputs"][key] != base.manifest[key] for key in fixed_inputs)
                or record["start"] != start or record["end"] > end
                or len(cases) != 1 or cases[0]["status"] != "completed"
                or cases[0]["completed_sessions"] != record["sessions"]):
            raise ValueError("Cached ranks require completed unchanged full-source momentum ledgers")
        artifacts = Path(record["artifacts"]).resolve()
        artifacts.relative_to((DATA_ROOT / "research/market_replay").resolve())
        dp, rp = artifacts / (CONTROL + "_decisions.csv"), artifacts / (CONTROL + "_ranks_1_30.csv")
        df, rf = pd.read_csv(dp), pd.read_csv(rp)
        if (df.date.duplicated().any() or not df.variant.eq(CONTROL).all()
                or not df.date.isin(base.calendar).all() or not df.date.between(start, record["end"]).all()
                or rf.duplicated(["date", "rank"]).any() or rf.duplicated(["date", "symbol"]).any()
                or not rf.date.isin(df.date).all()):
            raise ValueError("Invalid cached rank/decision identities")
        for day, rows in rf.groupby("date"):
            rows = rows.sort_values("rank")
            if (rows["rank"].tolist() != list(range(1, len(rows) + 1))
                    or len(rows) > config.retain_rank or not rows.symbol.isin(base.symbols).all()
                    or rows.symbol.isin(base.unsupported_instruments).any()
                    or not np.isfinite(rows[["momentum", "volatility", "median_amount", "score"]]).all().all()
                    or not rows.score.eq(rows.momentum).all()
                    or rows.symbol.tolist() != rows.sort_values(["score", "symbol"], ascending=[False, True]).symbol.tolist()):
                raise ValueError("Cached top30 does not match fixed momentum ordering")
            values = rows.drop(columns=["date", "rank"]).to_dict("records")
            if day in ranks and ranks[day] != values:
                raise ValueError("Conflicting cached rank lineage")
            ranks[day] = values
        for row in df.to_dict("records"):
            day = row.pop("date")
            values = {key: None if pd.isna(value) else value for key, value in row.items()}
            if day in decisions and decisions[day] != values:
                raise ValueError("Conflicting cached decision lineage")
            decisions[day] = values
        receipts.append({"source_run": str(folder), "decision_file": str(dp), "rank_file": str(rp),
            "decision_rows": len(df), "rank_rows": len(rf),
            "decision_sha256": sha256(dp.read_bytes()).hexdigest(), "rank_sha256": sha256(rp.read_bytes()).hexdigest()})
        previous = record["protocol"].get("continuation", {}).get("source_run")
        folder = Path(previous).resolve() if previous else None
    ends = [day for day in month_ends(base.calendar, end) if day >= start]
    for day in ends:
        row = decisions.get(day)
        if row is None:
            raise ValueError(f"Missing full-source month-end decision: {day}")
        expected = row["candidate_count"]
        if row["intent"] == "FULL_TARGET":
            if (expected is None or int(expected) != expected or expected < 0
                    or row["eligible_count"] != expected
                    or len(ranks.get(day, [])) != min(int(expected), config.retain_rank)
                    or row["decision"] != ("monthly_ranking" if expected else "known_empty_eligible_pool")):
                raise ValueError("Cached candidates do not cover the unchanged selector")
        elif row["intent"] != "NO_CHANGE" or expected is not None or day in ranks:
            raise ValueError("Unavailable stock inputs cannot become a target")
    return {day: (decisions[day], ranks.get(day, [])) for day in ends}, receipts


class MomentumGateFeed:
    def __init__(self, base, ranks, table, config, end):
        self.base, self.ranks, self.config, self.end = base, ranks, config, end
        self.ends = set(month_ends(base.calendar, end))
        self.gates = table.set_index("date").to_dict("index")
        self.audit = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError("Fixed cached momentum gate is replay-only with unchanged costs/selection")
        if day > self.end or day not in self.base.day_index:
            raise ValueError("Decision outside the pinned calendar/cutoff")
        reason, gate, candidates, pool_size = "not_month_end", None, None, None
        if day in self.ends:
            gate = self.gates.get(day)
            reason = gate["gate"] if gate else "index_gate_batch_missing"
            if reason == "risk_off":
                candidates = []
            elif reason == "risk_on":
                decision, rows = self.ranks.get(day, ({"intent": "NO_CHANGE",
                    "decision": "cached_rank_batch_missing", "candidate_count": None}, []))
                pool_size = decision["candidate_count"]
                if decision["intent"] == "FULL_TARGET":
                    candidates = rows
                else:
                    reason = decision["decision"]
        else:
            completed = [d for d, row in self.gates.items() if d < day
                         and d in self.ends and row["gate"] in {"risk_on", "risk_off"}]
            gate = self.gates[max(completed)] if completed else None
            if gate and gate["gate"] == "risk_off":
                reason, candidates = "carry_cash_target", []
        self.audit.append({"date": day, "variant": VARIANT, "decision": reason,
            "full_source_candidate_count": pool_size,
            "cached_selector_rows": len(candidates) if candidates is not None else None,
            "index_close": gate["index_close"] if gate else None,
            "sma_10_months": gate["sma_10_months"] if gate else None,
            "intent": "NO_CHANGE" if candidates is None else "FULL_TARGET"})
        if candidates is None:
            return SignalUnavailable(reason)
        return SignalFrame.from_legacy(candidates, day, "research:" + VARIANT, context)


def run(price_view, event_view, reference_view, listing_master, indices, control_run, source_pdf, initial_cash=140000):
    config = portfolio(initial_cash)
    source_pdf = Path(source_pdf).resolve()
    if not source_pdf.read_bytes().startswith(b"%PDF"):
        raise ValueError("Expected saved author PDF, not a blocked HTML response")
    base = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    try:
        if (base.manifest["scope"] != BULK_SCOPE or START not in base.calendar
                or END not in base.calendar or base.manifest["end"] < END):
            raise ValueError("Require covered full-source diagnostic stock inputs")
        dates = [day for day in base.calendar if START <= day <= END]
        controls = saved_stock_controls(control_run, base, config, dates, (CONTROL,))
        ranks, receipts = cached_ranks(control_run, base, config, START, END)
        table, index_receipt = index_inputs(indices, base.calendar, END, terminal_calendar=True)
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        inputs = {key: base.manifest[key] for key in ("price_view", "event_view", "reference_view",
            "raw_view", "listing_master", "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
        protocol = {"id": "twenty_stock_momentum_csi300_10m_v1", "portfolio": config.to_dict(),
            "signals": [VARIANT], "saved_control": CONTROL,
            "signal_portfolio_configs": {VARIANT: config.to_dict()}, "bulk_inputs": inputs,
            "index_inputs": index_receipt, "cached_ranks": receipts,
            "source": {"url": SOURCE_URL, "local_pdf": str(source_pdf),
                "review": "Author monthly10-month rule/execution/risk sections reviewed; not full-paper replication"},
            "rule": "Unchanged full-source supported SH/SZ eligible pool,252-minus21-session momentum;20 names/rank30 retention; equal4percent targets/gross80percent. No parameter or model fitting",
            "gate": "Confirmed month-end CSI300 close above its10-month SMA: original stock target; below: empty target; equality/warmup/missing: NO_CHANGE. Carry prior cash target between months; existing engine retries blocked exits",
            "cache": "Original monthly top30 computed on5368 supported ordinary stocks; sufficient for existing20-name/rank30 selector, not a30-stock universe. No feature recomputation",
            "control_basis": "Read-only completed same-input/cost/dates account, prefix-input guard unchanged; no control rerun or curve combination",
            "execution": "Existing next-session Engine/Ledger raw prices,actions,ST,T1,lots,limits,liquidity,costs; cash earns zero. Final terminal month-end is not guessed",
            "paper_differences": "Market-wide price-index timing of stock momentum, not each original asset's total-return timing; next-session fills instead of same-close, explicit costs, zero-interest cash",
            "hypothesis": "Preexisting fixed market gate may reduce matched20-stock drawdown; retain weaker returns/Sharpe and failures without tuning",
            "basis": "New fixed diagnostic on already-inspected2024-2026, not untouched/prospective evidence or an unchanged rerun of the prior negative five-stock experiment",
            "tuning": False, "fresh_holdout_claimed": False, "selection_is_point_in_time": False,
            "execution_admitted": False, "limitations": [*base.manifest["limitations"],
                "Monthly timing cannot guarantee black-swan protection or exits during limits/suspensions",
                "No all-ETF/BSE/independent PIT coverage, profitability or live readiness claim"]}
        record = {"status": "running", "research_family": "twenty_stock_momentum_gate",
            "purpose": "fixed_twenty_stock_market_gate_paired_diagnostic", "dataset": str(base.folder.resolve()),
            "artifacts": str(artifacts.resolve()), "scope": base.manifest["scope"], "symbols": len(base.symbols),
            "start": START, "end": END, "sessions": len(dates), "inputs": inputs, "protocol": protocol,
            "results": [], "saved_controls": controls, "strategy_validated": False, "live_ready": False,
            "started_at": datetime.now().astimezone().isoformat()}
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        table.to_csv(artifacts / "month_end_gate.csv", index=False)
        wrapper = MomentumGateFeed(base, ranks, table, config, END)
        try:
            result = baseline_ledger_result(wrapper, config, dates, artifacts / VARIANT)
            result.update(signal=VARIANT, label="20-stock momentum + CSI300 monthly10-month gate",
                          decisions=dict(Counter(row["decision"] for row in wrapper.audit)))
            if result["status"] == "completed":
                result["annual_same_account"] = stage_metrics(artifacts / VARIANT, initial_cash)
            record["results"].append(result)
            record.update(status=result["status"] + "_momentum_gate_diagnostic", completed_at=datetime.now().astimezone().isoformat())
        except Exception as error:
            record.update(status="failed_momentum_gate_diagnostic", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            pd.DataFrame(wrapper.audit).to_csv(artifacts / (VARIANT + "_decisions.csv"), index=False)
            write_json(out / "run.json", record)
            report(out, record)
        print(json.dumps({"output": str(out), "status": record["status"], "results": record["results"],
                          "saved_controls": controls}, ensure_ascii=False), flush=True)
        return out
    finally:
        base.close()


def report(out, record):
    rows = record["saved_controls"] + record["results"]
    lines = ["# Fixed20-stock momentum / monthly market gate", "", f"{record['status']}. NO_TRADE.",
        f"{record['symbols']} source stock identities;5368 supported ordinary SH/SZ,one unsupportedCDR. {START}..{END}, {record['sessions']} sessions.",
        "Only the gated account is newly simulated; completed matched control is read-only.", "",
        "| Case | Net return | Max drawdown | Sharpe zero rf | Fees | Stale days |",
        "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        if row["status"] == "completed":
            sharpe = row["sharpe_zero_rf_252"]
            lines.append(f"| {row['signal']} | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} | {row['fees']:.2f} | {row['stale_valuation_days']} |")
        else:
            lines.append(f"| {row['signal']} | failed: {row.get('error')} | unavailable | unavailable | unavailable | unavailable |")
    lines += ["", "## Carried Annual Account", "", "| Case | Year | Net return | Max drawdown | Sharpe zero rf |", "|---|---|---:|---:|---:|"]
    for row in rows:
        for stage in row.get("annual_same_account", []):
            sharpe = stage["sharpe_zero_rf_252"]
            lines.append(f"| {row['signal']} | {stage['year']} | {stage['net_return']:.2%} | {stage['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} |")
    lines += ["", "## Rules And Boundaries", "", *[record["protocol"][key] for key in (
        "rule", "gate", "cache", "control_basis", "execution", "paper_differences", "basis")],
        *record["protocol"]["limitations"], "No native client,broker binding,submission or actual14wan account verification.",
        f"Author reference: [original PDF]({SOURCE_URL})", f"Ledger artifacts: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price-view", "event-view", "reference-view", "listing-master", "indices", "control-run", "source-pdf"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, default=140000)
    args = parser.parse_args(argv)
    out = run(args.price_view, args.event_view, args.reference_view, args.listing_master,
              args.indices, args.control_run, args.source_pdf, args.initial_cash)
    return 0 if json.loads((out / "run.json").read_text(encoding="utf-8"))["status"].startswith("completed_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
