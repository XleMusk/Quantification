"""Read saved research comparisons and the shared Ledger without running work.

Only small allowlisted result/registration receipts are discovered. Metrics from
failed or partial accounts are never promoted into full-period performance.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from trading import settings as trading_settings, store as trading_store
from trading.config import StrategyConfig
from trading.ledger import Ledger
from trading.report import quote_rmb
from trading.research_evidence_catalog import read_catalog

MAX_JSON = 4 * 1024 * 1024
MAX_RECORDS = 120
AREAS = ("factor_mining", "market_replay", "family_pipeline", "family_holdout",
         "monthly_max_vs_sigma", "monthly_max_vs_sigma_source_review", "leader_cross_year")
COST_KEYS = ("commission", "minimum_commission", "sell_tax", "transfer_fee", "slippage_bps", "dividend_tax_reserve")
CAPACITY_KEYS = ("initial_cash", "gross_limit", "single_limit", "participation", "holdings", "min_amount")
EXECUTION_KEYS = (*COST_KEYS, *CAPACITY_KEYS, "rebalance_sessions", "retain_rank", "min_listing_days")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value):
    return value if _finite(value) else None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _roots():
    return trading_settings.ROOT.resolve(), trading_settings.DATA_ROOT.resolve()


def _path(value):
    text = str(value).replace("\\", "/")
    # Saved Windows receipts can be reused after a scoped repository/data move.
    for prefix, root in (("D:/QuantificationData/trading", trading_settings.DATA_ROOT),
                         ("D:/Quantification", trading_settings.ROOT)):
        if text.lower() == prefix.lower() or text.lower().startswith(prefix.lower() + "/"):
            text = str(root / text[len(prefix):].lstrip("/"))
            break
    path = Path(text).resolve()
    if not any(path.is_relative_to(root) for root in _roots()):
        raise ValueError("研究来源不在配置的项目或数据目录内")
    return path


def _read(path):
    path = _path(path)
    if path.suffix.lower() != ".json" or path.stat().st_size > MAX_JSON:
        raise ValueError("研究结果须为不超过 4 MB 的已保存 JSON")
    raw = path.read_bytes()
    def invalid(value):
        raise ValueError("研究结果含非有限 JSON 数值")
    value = json.loads(raw.decode("utf-8-sig"), parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("研究清单须为 JSON 对象")
    return value, {"path": str(path), "sha256": sha256(raw).hexdigest()}


def _discovered():
    paths, truncated = set(), False
    for root in (trading_settings.ROOT / "outputs", trading_settings.DATA_ROOT / "research"):
        for area in AREAS:
            folder = (root / area).resolve()
            if not folder.is_dir() or not any(folder.is_relative_to(r) for r in _roots()):
                continue
            # One saved run level; linked exact receipts cover nested source reviews.
            children = sorted((p for p in folder.iterdir() if p.is_dir()), reverse=True)
            if len(children) > MAX_RECORDS:
                truncated = True
            for child in children[:MAX_RECORDS]:
                for name in ("run.json", "result.json"):
                    path = child / name
                    if path.is_file():
                        paths.add(path.resolve())
    registry = trading_settings.ROOT / "trading/research/recent_candidates.json"
    try:
        value, _ = _read(registry)
        for candidate in value.get("candidates", []):
            result = candidate.get("result") or {}
            for name in ("actual_result", "account_comparison"):
                reference = result.get(name)
                if isinstance(reference, str):
                    try:
                        path = _path(reference)
                        if path.is_file():
                            paths.add(path)
                    except (OSError, ValueError):
                        pass
    except (OSError, ValueError, TypeError):
        pass
    ordered = sorted(paths, key=lambda p: (p.stat().st_mtime_ns, str(p)), reverse=True)
    return ordered[:MAX_RECORDS], truncated or len(ordered) > MAX_RECORDS


def _input_fingerprint(record):
    hashes = record.get("input_hashes")
    if (isinstance(hashes, dict) and {"raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"} <= hashes.keys()
            and all(isinstance(value, str) and len(value) == 64 for value in hashes.values())):
        return sha256(_canonical(hashes).encode()).hexdigest()
    registration = record.get("registration")
    if isinstance(registration, dict) and registration.get("path") and registration.get("sha256"):
        _, proof = _read(registration["path"])
        if proof["sha256"] != registration["sha256"]:
            raise ValueError("研究登记文件与保存结果的版本不一致")
        return proof["sha256"]
    inputs = record.get("bulk_inputs") or record.get("inputs") or record.get("protocol", {}).get("stock_inputs")
    if isinstance(inputs, dict) and all(inputs.get(k) for k in ("price_view", "event_view", "reference_view", "listing_master")):
        refs = {}
        for name in ("price_view", "event_view", "reference_view"):
            _, proof = _read(_path(inputs[name]) / "result.json")
            refs[name] = proof["sha256"]
        master = _path(inputs["listing_master"])
        if not master.is_file() or master.stat().st_size > MAX_JSON:
            return None
        refs["listing_master"] = sha256(master.read_bytes()).hexdigest()
        # Source membership or a research-scope override must also match.
        refs["research_scope"] = record.get("dataset_manifest", {}).get("research_scope") or inputs.get("research_scope")
        return sha256(_canonical(refs).encode()).hexdigest()
    return None


def _entries(record, proof):
    protocol = record.get("protocol") or {}
    signal_configs = protocol.get("signal_portfolio_configs")
    config = record.get("portfolio_config") or protocol.get("portfolio") or record.get("config", {}).get("portfolio")
    if config is None and isinstance(record.get("config"), dict) and "initial_cash" in record["config"]:
        config = record["config"]
    rows = record.get("portfolio_metrics") or record.get("results") or record.get("cases")
    if not isinstance(rows, list) or (not isinstance(config, dict) and not isinstance(signal_configs, dict)):
        return []
    config = config if isinstance(config, dict) else {}
    study = record.get("experiment_kind") or record.get("purpose") or record.get("schema") or Path(proof["path"]).parent.parent.name
    segment = record.get("config", {}).get("segments", {}).get("test")
    spec = record.get("spec") or {}
    start = record.get("start") or spec.get("start") or (segment[0] if isinstance(segment, list) and len(segment) == 2 else None)
    end = record.get("end") or spec.get("end") or (segment[1] if isinstance(segment, list) and len(segment) == 2 else None)
    universe = record.get("scope") or record.get("dataset_manifest", {}).get("scope") or spec.get("universe")
    universe = universe if isinstance(universe, str) else None
    start = start if isinstance(start, str) else None
    end = end if isinstance(end, str) else None
    try:
        fingerprint = _input_fingerprint(record)
        source_problem = None
    except (OSError, ValueError, KeyError, TypeError) as error:
        fingerprint, source_problem = None, str(error)
    output = []
    for index, case in enumerate(rows):
        if not isinstance(case, dict):
            continue
        signal = case.get("signal") or case.get("variant") or case.get("arm") or str(index + 1)
        if "signal_portfolio_configs" in protocol:
            # Producers save complete per-signal configurations. An absent or
            # incomplete entry cannot inherit another arm's capital or fees.
            saved_config = signal_configs.get(signal) if isinstance(signal_configs, dict) else None
            own_config = dict(saved_config) if isinstance(saved_config, dict) else {}
        else:
            own_config = dict(config)
            if signal == "donchian_trend" and isinstance(protocol.get("donchian_portfolio_override"), dict):
                own_config.update(protocol["donchian_portfolio_override"])
        costs = {key: own_config[key] for key in COST_KEYS if _finite(own_config.get(key))}
        status = case.get("status") or ("completed" if record.get("status") == "completed" else "unknown")
        declared_complete = case.get("full_period_metrics_available", case.get("complete_PnL_available"))
        complete = bool(status == "completed" and declared_complete is not False and _finite(case.get("net_return")))
        metrics = ({key: _number(case.get(key)) for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees", "turnover_over_initial")}
                   if complete else None)
        if metrics is not None:
            metrics["completed_sessions"] = _number(case.get("completed_sessions", case.get("sessions")))
        missing = [key for key, value in (("数据版本", fingerprint), ("证券范围", universe), ("开始日期", start), ("结束日期", end)) if not value]
        if any(not _finite(own_config.get(key)) for key in EXECUTION_KEYS):
            missing.append("费用与执行参数")
        basis = {"data_fingerprint": fingerprint, "universe": universe, "start": start, "end": end, "costs": costs or None}
        key = None
        if not missing:
            key = sha256(_canonical({**basis, "execution": {k: own_config[k] for k in EXECUTION_KEYS},
                                     "source_record": proof["sha256"]}).encode()).hexdigest()
        caveats = ["保存的研究账户彼此独立，不合并资金；完整收益不代表策略已验证"]
        if record.get("fresh_holdout_claimed") is not True:
            caveats.append("历史区间不按未查看的新样本解释")
        if not complete:
            caveats.append(case.get("error") or "账户未明确完成，完整区间收益保持为空")
        if source_problem:
            caveats.append(source_problem)
        seed = case.get("seed")
        output.append({"id": sha256((proof["sha256"] + ":" + str(index)).encode()).hexdigest()[:24],
                       "name": str(signal) + (f" · seed {seed}" if seed is not None else ""), "study": study,
                       "kind": "saved_experiment", "status": status, "start": start, "end": end,
                       "complete": complete, "metrics": metrics, "source": proof, "basis": basis,
                       "comparison_key": key, "missing_comparison_fields": missing,
                       "capacity": {k: _number(own_config.get(k)) for k in CAPACITY_KEYS},
                       "observed_costs": {"fees": _number(case.get("fees")), "turnover_value": _number(case.get("turnover_value")),
                                          "completed_sessions": _number(case.get("completed_sessions", case.get("sessions"))),
                                          "basis": "full_period" if complete else "completed_prefix_only"}, "caveats": caveats})
    return output


def _liquidity(run, rows, day):
    if run["mode"] != "replay" or not day or not run.get("dataset"):
        return
    try:
        folder = _path(run["dataset"])
        manifest, _ = _read(folder / "manifest.json")
        if manifest.get("status") != "ready":
            return
        raw = folder / "raw.parquet"
        required = {"date", "symbol", "close", "volume", "adjustflag"}
        if not required <= set(pq.read_schema(raw).names):
            return
        date_type = pq.read_schema(raw).field("date").type
        day_value = day if str(date_type) in {"string", "large_string"} else pd.Timestamp(day)
        frame = pd.read_parquet(raw, columns=list(required), filters=[("symbol", "in", [r["symbol"] for r in rows]), ("date", "=", day_value)])
        frame = frame.loc[pd.to_datetime(frame.date).eq(pd.Timestamp(day))]
        for row in rows:
            match = frame.loc[frame.symbol.eq(row["symbol"])]
            if len(match) != 1:
                continue
            observed = match.iloc[0]
            if (str(observed.adjustflag) != "3" or not _finite(observed.volume) or observed.volume <= 0
                    or row["price"] is None or not math.isclose(float(observed.close), row["price"], rel_tol=0, abs_tol=.0005)):
                continue
            row["liquidity_participation"] = row["quantity"] / float(observed.volume)
            row["liquidity_basis"] = "持仓股数 / 账户同日同原价快照的成交股数"
    except (OSError, ValueError, TypeError, KeyError):
        return


def portfolio_metrics(run):
    state, config = run["state"], StrategyConfig(**run["config"])
    book, day = Ledger(state, config), state.get("day") or run["start"]
    positions, missing = [], []
    for symbol in sorted(state["positions"]):
        quantity = book.quantity(symbol)
        if quantity <= 0:
            continue
        raw_price = state["prices"].get(symbol)
        price = quote_rmb(raw_price, state.get("price_units", {}).get(symbol, "cent")) if type(raw_price) is int and raw_price > 0 else None
        value = book.position_value(symbol) / 100 if price is not None else None
        if value is None:
            missing.append(symbol)
        positions.append({"symbol": symbol, "quantity": quantity, "sellable": book.available(symbol, day),
                          "price": price, "value": value, "weight": None, "liquidity_participation": None})
    equity = book.equity() / 100 if not missing else None
    total = sum(row["value"] or 0 for row in positions)
    weights = []
    if equity is not None and equity > 0:
        for row in positions:
            row["weight"] = row["value"] / equity
            weights.append(row["weight"])
    hhi = sum((row["value"] / total) ** 2 for row in positions) if total > 0 and not missing else None
    _liquidity(run, positions, day)
    warnings = ["集中度按保存账本估值计算；相关性、行业历史分类和市场冲击尚未在该视图估计",
                "持仓/当日成交量用于容量观察，不是实际成交参与率或可立即卖出承诺"]
    if missing:
        warnings.append("持仓缺估值价格，权益及权重不推算：" + ", ".join(missing))
    fees = (sum(order.get("fees", 0) for order in state["orders"].values()) - sum(
        movement["cash_change_cents"] for movement in state.get("broker_cash_movements", {}).values()
        if movement["kind"] in {"tax", "fee"})) / 100
    return {"id": run["id"], "name": run["name"], "mode": run["mode"], "status": run["status"], "day": state.get("day"),
            "equity": equity, "cash": state["cash"] / 100, "available_cash": book.cash_available() / 100,
            "gross_exposure": total / equity if equity is not None and equity > 0 else None,
            "largest_weight": max(weights, default=0) if equity is not None and equity > 0 else None,
            "top5_weight": sum(sorted(weights, reverse=True)[:5]) if equity is not None and equity > 0 else None,
            "hhi": hhi, "effective_positions": 1 / hhi if hhi else None,
            "fees": fees, "position_count": len(positions), "positions": positions,
            "warnings": warnings, "source": "shared_ledger", "valuation_complete": not missing,
            "limits": {key: getattr(config, key) for key in CAPACITY_KEYS}}


def workspace():
    paths, truncated = _discovered()
    experiments, warnings = [], []
    for path in paths:
        try:
            record, proof = _read(path)
            experiments.extend(_entries(record, proof))
        except (OSError, ValueError, KeyError, TypeError) as error:
            warnings.append(f"保存研究不可读取 {path.parent.name}/{path.name}: {error}")
    try:
        catalog = read_catalog(trading_settings.ROOT, trading_settings.DATA_ROOT)
        for candidate in catalog.get("candidates", []):
            experiments.append({"id": "candidate:" + str(candidate.get("id")), "name": candidate.get("title") or candidate.get("id"),
                "study": candidate.get("id"), "kind": "candidate_reference", "status": candidate.get("reproduction_status") or candidate.get("status"),
                "start": None, "end": None, "complete": False, "metrics": None,
                "source": catalog.get("registry"), "basis": {"data_fingerprint": None, "universe": None, "start": None, "end": None, "costs": None},
                "comparison_key": None, "missing_comparison_fields": ["需读取原始账户结果及相同来源、费用、证券范围"], "capacity": None,
                "caveats": ["候选目录条目，完整账户结果在可读取的保存研究中独立展示", *[str(v) for v in candidate.get("known_gaps") or []]]})
    except (OSError, ValueError, KeyError, TypeError) as error:
        warnings.append(f"候选目录不可读取: {error}")
    portfolios = []
    for row in trading_store.runs()[:20]:
        try:
            portfolios.append(portfolio_metrics(trading_store.get(row["id"], full=True)))
        except (ValueError, KeyError, TypeError) as error:
            warnings.append(f"账本风险视图不可读取 {row['id']}: {error}")
    if truncated:
        warnings.append("保存研究列表达到 120 个结果清单范围，未遍历全部历史目录")
    return {"schema": "research_workspace_v1", "status": "available" if experiments or portfolios else "empty",
            "served_at": datetime.now(timezone.utc).isoformat(), "experiments": experiments, "portfolios": portfolios,
            "warnings": warnings, "research_only": True, "training_started": False, "ledger_mutated": False}
