"""Project the saved opening coordinator without generating any artifacts."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re

from . import opening_daily


SCHEMA = "saved_opening_daily_view_v1"
RUN_RELATIVE = Path("research/opening_daily/20261017_v1")
FLAGS = {**opening_daily.FLAGS, "readonly": True, "factory_called": False,
         "producer_called": False, "account_accessed": False, "workers_started": False,
         "source_observation_updated": False, "read_at_is_source_time": False,
         "saved_artifacts_revalidated": False}


def _count(product, key):
    value = product.get(key)
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Saved opening counts must be explicit nonnegative integers or unknown")
    return value


def _day(value):
    if value is not None and (not isinstance(value, str) or date.fromisoformat(value).isoformat() != value):
        raise ValueError("Saved opening dates must retain exact sessions or unknown")
    return value


def _reference(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("path"), str)
            or not Path(value["path"]).is_absolute()
            or not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])):
        raise ValueError("Saved opening reference must retain its absolute path and SHA256")
    return {"path": value["path"], "sha256": value["sha256"]}


def opening_daily_view(data_root, *, now=None):
    """Read current pointers only; read_at is a page check, not source freshness."""
    current = now or datetime.now(timezone(timedelta(hours=8)))
    if current.tzinfo is None:
        raise ValueError("Opening view read clock requires an explicit timezone")
    directory = (Path(data_root) / RUN_RELATIVE).resolve()
    base = {"schema": SCHEMA, "read_at": current.isoformat(), "run_directory": str(directory),
            "routes": [], **FLAGS}
    if not directory.exists():
        return {**base, "status": "unavailable", "reason": "no_saved_opening_daily_coordinator"}
    path = directory / "head.json"
    before = path.read_bytes()
    head = json.loads(before.decode("utf-8"))
    saved = opening_daily.status(directory)
    if path.read_bytes() != before:
        raise ValueError("Opening coordinator advanced during read; refresh its saved state")
    rows = []
    for item in saved["routes"]:
        committed = head["routes"][item["id"]]
        product = item["saved_product"]
        if product != (committed["product"] if committed is not None else None):
            raise ValueError("Opening status and committed product belong to different saved heads")
        if product is not None and not isinstance(product, dict):
            raise ValueError("Saved opening product must remain an object or unknown")
        product = product if product is not None else {}
        if product and any(product.get(key) is not expected for key, expected in opening_daily.FLAGS.items()
                           if key in {"research_only", "candidate_panel_only", "execution_admitted",
                                      "selection_enabled", "strategy_validated", "prefill_allowed"}):
            raise ValueError("Saved opening product changed its research-only boundary")
        committed_at = committed.get("committed_at") if committed is not None else None
        if committed_at is not None and datetime.fromisoformat(committed_at).tzinfo is None:
            raise ValueError("Saved product commit clock requires an explicit timezone")
        refs = product.get("refs", {})
        if not isinstance(refs, dict):
            raise ValueError("Saved opening refs must remain a mapping")
        rows.append({**{key: deepcopy(item.get(key)) for key in (
            "id", "family", "status", "source_changed", "stale", "error", "attempt_status", "input_id")},
            "source_head": _reference(item.get("source_head")),
            "product_source_head": _reference(product.get("source_head")),
            "source_day": _day(product.get("source_day")), "execution_day": _day(product.get("execution_day")),
            "date_basis": "saved_product_source_head_not_current_source_head",
            **{key: _count(product, key) for key in ("case_count", "ready_case_count", "not_ready_case_count", "candidate_order_count")},
            "product_status": product.get("status"), "committed_at": committed_at,
            "refs": {key: _reference(value) for key, value in refs.items()},
            "execution_progress_unknown": item.get("execution_progress_unknown")})
    return {**base, "status": saved["status"], "routes": rows,
            "registration": _reference(saved["registration"]), "head_path": saved["head"],
            "source_head_check_only": True}
