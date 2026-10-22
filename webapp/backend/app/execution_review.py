"""Read saved THS observations and calculate explicit fill/reference costs.

This module never initializes the journal, captures a client, imports a ledger,
creates an order, or treats a missing view as an observed empty table.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import csv
import io
import json
import re
import sqlite3
from urllib.parse import quote

from trading import settings as trading_settings
from trading.ths.journal import decode as decode_plan
from trading.ths.observations import identifier, integer

SHANGHAI = timezone(timedelta(hours=8))
VIEW_NAMES = {"account": "账户与持仓", "orders": "当日委托", "trades": "当日成交", "cashflows": "累计资金流水"}
FILL_FIELDS = {"account_alias", "trading_date", "execution_id", "symbol", "side", "qty", "price_rmb", "executed_at"}
QUOTE_FIELDS = {"account_alias", "trading_date", "execution_id", "symbol", "reference_at", "reference_price_rmb", "reference_kind", "source"}
CSV_MAX_BYTES = 2_000_000
CSV_MAX_ROWS = 10000


def _stamp(value, name):
    try:
        result = datetime.fromisoformat(str(value))
    except (ValueError, TypeError) as error:
        raise ValueError(f"{name} 必须为带时区的 ISO 时间") from error
    if result.tzinfo is None or result > datetime.now(timezone.utc):
        raise ValueError(f"{name} 必须带时区且不晚于当前时间")
    return result


def _view(value, current):
    if value is None:
        return {"observed": False, "row_count": None, "observed_at": None, "trading_date": None,
                "age_seconds": None, "freshness": "missing", "source": None}
    try:
        stamp = _stamp(value.get("observed_at"), "观察时间")
        age = max(0., (current - stamp).total_seconds())
        freshness = "fresh" if age <= 300 else "historical"
    except ValueError:
        age, freshness = None, "invalid_timestamp"
    rows = value.get("rows")
    return {"observed": True, "row_count": len(rows) if isinstance(rows, list) else None,
            "observed_at": value.get("observed_at"), "trading_date": value.get("trading_date"),
            "age_seconds": age, "freshness": freshness, "source": value.get("source")}


def workspace(run_id=None):
    """A transactional read of existing rows, including a truly empty database."""
    current = datetime.now(timezone.utc)
    result = {"schema": "execution_review_v1", "generated_at": current.isoformat(),
              "status": "no_saved_observations", "runs": [], "selected_run_id": run_id,
              "accounts": [], "plans": [], "issues": [], "client_interacted": False,
              "ledger_mutated": False, "submission_enabled": False,
              "workflow": [
                  {"label": "检查四表与列映射", "command": r"D:\Quantification\env\python.exe D:\Quantification\run_committee.py ths preview-observation-bundle --bundle-dir D:\QuantificationData\trading\ths\observation_bundle --out <新的预览.json>"},
                  {"label": "预览同一账本的准确变化", "command": r"D:\Quantification\env\python.exe D:\Quantification\run_committee.py ths preview-ledger-import --run-id <暂停的模拟运行ID> --id <已核对的计划ID> --bundle-dir D:\QuantificationData\trading\ths\observation_bundle --out <新的入账预览.json>"},
              ],
              "calibration": {"status": "requires_timestamped_reference", "fill_count": None,
                              "reason": "保存的成交回读不保证含实际成交时间与同期原价参考；上传明确对应的两张表后计算"}}
    path = trading_settings.DB_PATH
    if not path.is_file():
        if run_id:
            raise ValueError("运行不存在")
        return result
    with sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = db.execute("SELECT id,name,mode,status,state,error FROM runs ORDER BY created_at DESC LIMIT 100").fetchall() if "runs" in tables else []
        selected = None
        for row in runs:
            state = json.loads(row["state"])
            binding = state.get("broker_binding") or {}
            item = {key: row[key] for key in ("id", "name", "mode", "status", "error")}
            item.update(account_alias=binding.get("account_alias"), channel=binding.get("channel"),
                        broker_account_kind=binding.get("broker_account_kind"), day=state.get("day"),
                        bound=binding.get("channel") == "ths")
            result["runs"].append(item)
            if row["id"] == run_id:
                selected = item
        if run_id and selected is None:
            # A selected older run may be outside the bounded chooser.
            row = db.execute("SELECT id,name,mode,status,state,error FROM runs WHERE id=?", (run_id,)).fetchone() if "runs" in tables else None
            if row is None:
                raise ValueError("运行不存在")
            binding = json.loads(row["state"]).get("broker_binding") or {}
            selected = {key: row[key] for key in ("id", "name", "mode", "status", "error")}
            selected.update(account_alias=binding.get("account_alias"), channel=binding.get("channel"), broker_account_kind=binding.get("broker_account_kind"), bound=binding.get("channel") == "ths")
            result["runs"].append(selected)
        alias_filter = selected.get("account_alias") if selected and selected.get("channel") == "ths" else None
        if selected and not alias_filter:
            result["issues"].append("该运行没有绑定同花顺观察账户；回放成交不作为券商实测成交")
        observations = []
        if "ths_observations" in tables and (not selected or alias_filter):
            query = "SELECT payload FROM (SELECT payload, ROW_NUMBER() OVER(PARTITION BY account_alias,kind ORDER BY julianday(observed_at) DESC,id DESC) n FROM ths_observations"
            params = []
            if alias_filter:
                query += " WHERE account_alias=?"
                params.append(alias_filter)
            query += ") WHERE n=1 LIMIT 400"
            observations = [json.loads(row[0]) for row in db.execute(query, params)]
        aliases = sorted({value["account_alias"] for value in observations} | ({alias_filter} if alias_filter else set()))
        for alias in aliases:
            views = {value["kind"]: value for value in observations if value["account_alias"] == alias}
            account = views.get("account")
            summaries = {kind: _view(views.get(kind), current) for kind in VIEW_NAMES}
            days = {value["trading_date"] for value in summaries.values() if value["observed"] and value["trading_date"]}
            issues = [f"缺少{label}观察" for kind, label in VIEW_NAMES.items() if kind not in views]
            if len(days) > 1:
                issues.append("四类观察的交易日不一致，不能作为同一期对账资料")
            if account and account.get("cash_basis") != "cash_balance_includes_frozen":
                issues.append("账户缺含冻结余额的明确资金口径")
            if account and all(key in account for key in ("cash_total", "cash_frozen", "cash_available")):
                if account["cash_total"] - account["cash_frozen"] != account["cash_available"]:
                    issues.append("资金余额减冻结资金与可用资金不一致")
            result["accounts"].append({"account_alias": alias, "views": summaries,
                "status": "saved_four_views" if not issues else "observation_incomplete", "issues": issues,
                "cash_total_rmb": account.get("cash_total", 0) / 100 if account and account.get("cash_total") is not None else None,
                "cash_frozen_rmb": account.get("cash_frozen", 0) / 100 if account and account.get("cash_frozen") is not None else None,
                "cash_available_rmb": account.get("cash_available", 0) / 100 if account and account.get("cash_available") is not None else None})
        if "ths_plans" in tables and (not selected or alias_filter):
            where, params = (" WHERE account_alias=?", [alias_filter]) if alias_filter else ("", [])
            rows = db.execute("SELECT * FROM ths_plans" + where + " ORDER BY julianday(updated_at) DESC,id DESC LIMIT 100", params)
            for row in rows:
                plan = decode_plan(row)
                receipt = plan.get("result") or {}
                imported = receipt.get("ledger_import") or {}
                result["plans"].append({key: plan.get(key) for key in ("id", "account_alias", "symbol", "side", "qty", "status", "trading_date", "updated_at")}
                    | {"confirmed_filled_qty": receipt.get("confirmed_filled_qty"),
                       "confirmed_execution_count": len(receipt["confirmed_executions"]) if isinstance(receipt.get("confirmed_executions"), list) else None,
                       "account_reconciliation": receipt.get("account_reconciliation"),
                       "confirmed_account_reconciliation": receipt.get("confirmed_account_reconciliation"),
                       "ledger_imported": bool(run_id and imported.get("ledger_imported") is True and imported.get("run_id") == run_id),
                       "reason": receipt.get("reason"), "error": receipt.get("error") or receipt.get("ledger_import_error")})
        if "ths_control" in tables:
            row = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
            result["adapter_paused"] = row[0] == "true" if row else None
        else:
            result["adapter_paused"] = None
    if result["accounts"]:
        result["status"] = "saved_observations"
    return result


def _csv(text, required, optional=()):
    if not isinstance(text, str) or len(text.encode("utf-8")) > CSV_MAX_BYTES:
        raise ValueError("每张 CSV 须为 UTF-8 文本，最大 2 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = reader.fieldnames or []
    if len(set(fields)) != len(fields) or not required.issubset(fields) or set(fields) - required - set(optional):
        raise ValueError("CSV 表头须使用指定字段，不能缺列、重名或含未映射列")
    rows = []
    for row in reader:
        if len(rows) >= CSV_MAX_ROWS:
            raise ValueError("每张 CSV 最多 10000 条记录")
        if None in row or any(value is None for value in row.values()):
            raise ValueError("CSV 行结构不完整")
        rows.append({key: value.strip() for key, value in row.items()})
    return rows


def _decimal(value, name, *, zero=False):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{name} 须为有限数字") from error
    if not number.is_finite() or number < 0 or (not zero and number == 0):
        raise ValueError(f"{name} 须为{'非负' if zero else '正'}有限数字")
    if abs(number) > Decimal("1e15"):
        raise ValueError(f"{name} 数值过大")
    return number


def _identity(row):
    alias = row["account_alias"]
    if not alias or len(alias) > 80:
        raise ValueError("账户别名不能为空且最长 80 字符")
    day = date.fromisoformat(row["trading_date"])
    if day.isoformat() != row["trading_date"] or not re.fullmatch(r"\d{6}\.(SSE|SZSE|BSE)", row["symbol"]):
        raise ValueError("交易日须为 YYYY-MM-DD，证券须含六位代码与明确交易所")
    return alias, row["trading_date"], identifier(row["execution_id"]), row["symbol"]


def calibrate(payload):
    """Cost against explicit per-execution historical reference observations."""
    if not isinstance(payload, dict) or (payload.get("currency"), payload.get("price_basis"), payload.get("quantity_unit")) != ("CNY", "raw_rmb", "share"):
        raise ValueError("须明确声明 CNY、raw_rmb、share 单位；不接受复权价或手数")
    if payload.get("evidence_source") not in {"broker_export", "ths_user_export"}:
        raise ValueError("成交来源须明确为 broker_export 或 ths_user_export")
    max_age = payload.get("max_reference_age_seconds", 300)
    if type(max_age) is not int or not 1 <= max_age <= 3600:
        raise ValueError("参考价最大间隔须为 1 至 3600 秒的整数")
    fills = _csv(payload.get("fills_csv"), FILL_FIELDS, {"fees_rmb", "order_id"})
    quotes = _csv(payload.get("quotes_csv"), QUOTE_FIELDS)
    references, used, seen = {}, set(), set()
    for row in quotes:
        key = _identity(row)
        if key in references:
            raise ValueError("同账户、交易日、成交号和证券的参考记录重复")
        if row["reference_kind"] not in {"arrival", "decision", "midpoint"} or not row["source"] or len(row["source"]) > 200:
            raise ValueError("参考类型须为 arrival/decision/midpoint，且需提供实际来源")
        stamp = _stamp(row["reference_at"], "参考价时间")
        if stamp.astimezone(SHANGHAI).date().isoformat() != row["trading_date"]:
            raise ValueError("参考价上海交易日与记录交易日不一致")
        references[key] = (row, stamp, _decimal(row["reference_price_rmb"], "参考价"))
    output, totals = [], {}
    for row in fills:
        key = _identity(row)
        # A broker's execution ID cannot identify two different securities.
        if key[:3] in seen:
            raise ValueError("同账户和交易日的成交号重复，未重复累计")
        seen.add(key[:3])
        qty, price = integer(row["qty"]), _decimal(row["price_rmb"], "成交价")
        if not qty or qty > 1_000_000_000 or row["side"] not in {"buy", "sell"}:
            raise ValueError("成交方向须为 buy/sell，数量须为 1 至 10 亿整数股")
        executed = _stamp(row["executed_at"], "成交时间")
        if executed.astimezone(SHANGHAI).date().isoformat() != row["trading_date"]:
            raise ValueError("成交上海交易日与记录交易日不一致")
        fees = _decimal(row["fees_rmb"], "逐笔实际费用", zero=True) if row.get("fees_rmb") else None
        value = {key: row[key] for key in ("account_alias", "trading_date", "execution_id", "symbol", "side", "executed_at")}
        value.update(qty=qty, price_rmb=float(price), fees_rmb=float(fees) if fees is not None else None,
                     reference_at=None, reference_price_rmb=None, reference_kind=None, reference_age_seconds=None,
                     slippage_bps=None, slippage_cost_rmb=None, all_in_cost_rmb=None, status="reference_missing")
        reference = references.get(key)
        if reference:
            used.add(key)
            raw, stamp, benchmark = reference
            age = (executed - stamp).total_seconds()
            value.update(reference_at=raw["reference_at"], reference_price_rmb=float(benchmark),
                         reference_kind=raw["reference_kind"], reference_source=raw["source"], reference_age_seconds=age)
            if age < 0:
                value["status"] = "reference_after_fill"
            elif age > max_age:
                value["status"] = "reference_too_old"
            else:
                cost = (price - benchmark) * qty * (1 if row["side"] == "buy" else -1)
                notional = benchmark * qty
                value.update(status="matched", slippage_bps=float(cost / notional * 10000),
                             slippage_cost_rmb=float(cost), all_in_cost_rmb=float(cost + fees) if fees is not None else None)
                group = totals.setdefault((raw["reference_kind"], row["side"]), {"reference_kind": raw["reference_kind"], "side": row["side"],
                    "fill_count": 0, "quantity": 0, "reference_notional_rmb": Decimal(0), "slippage_cost_rmb": Decimal(0), "fees_rmb": Decimal(0), "fees_known_count": 0})
                group["fill_count"] += 1
                group["quantity"] += qty
                group["reference_notional_rmb"] += notional
                group["slippage_cost_rmb"] += cost
                if fees is not None:
                    group["fees_known_count"] += 1
                    group["fees_rmb"] += fees
        output.append(value)
    groups = []
    for group in totals.values():
        all_known = group["fees_known_count"] == group["fill_count"]
        group["slippage_bps"] = float(group["slippage_cost_rmb"] / group["reference_notional_rmb"] * 10000)
        group["all_in_cost_bps"] = float((group["slippage_cost_rmb"] + group["fees_rmb"]) / group["reference_notional_rmb"] * 10000) if all_known else None
        group["fees_rmb"] = float(group["fees_rmb"]) if all_known else None
        group["reference_notional_rmb"] = float(group["reference_notional_rmb"])
        group["slippage_cost_rmb"] = float(group["slippage_cost_rmb"])
        groups.append(group)
    matched = sum(row["status"] == "matched" for row in output)
    return {"schema": "execution_cost_preview_v1", "status": "matched" if matched == len(output) and output else "partial" if matched else "no_matched_fills",
            "fill_count": len(output), "matched_count": matched, "unmatched_count": len(output) - matched,
            "unused_reference_count": len(references) - len(used), "rows": output, "groups": groups,
            "input_fingerprint": sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "basis": "explicit_per_execution_reference_raw_rmb", "max_reference_age_seconds": max_age,
            "adverse_cost_positive": True, "ledger_mutated": False, "client_interacted": False,
            "native_verified": False, "submission_enabled": False,
            "notes": ["费用为空保持未知，委托累计费用须先有逐笔分摊依据；不自动平均分摊", "参考时间必须不晚于成交，按明确的账户、交易日、成交号和证券匹配", "买入高于参考价、卖出低于参考价为正成本；不同参考类型与方向分别汇总", "此处只预览上传资料，不更新研究费用配置、观察日志或账户"]}
