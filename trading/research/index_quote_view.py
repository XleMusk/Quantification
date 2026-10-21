"""Read saved fixed-index observations; recompute age, never contact a provider."""
from datetime import datetime
import json
import math
from pathlib import Path

TARGETS = {"sh000001", "sz399006", "sh000688"}


def _index_quote_observation(workspace, *, data_root=None, now=None):
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        raise ValueError("Index observation read time requires a timezone")
    result = {"status": "missing", "quotes": [], "read_at": now.isoformat(),
              "source_file": None, "provider_contacted": False,
              "all_A_breadth": False, "is_BrokerQuote": False,
              "used_for_close_analysis": False, "selection_enabled": False, "execution_admitted": False}
    try:
        external = Path(data_root).resolve() / "research/index_observation" if data_root is not None else None
        pointer_path = external / "latest.json" if external is not None else None
        if pointer_path is not None and pointer_path.is_file():
            pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig"))
            if (pointer.get("schema") != "index_observation_latest_v1"
                    or pointer.get("execution_admitted") is not False
                    or pointer.get("selection_enabled") is not False):
                raise ValueError("Index observation locator boundary changed")
            path, job_path = (Path(pointer[key]).resolve() for key in ("attempt_receipt", "job_receipt"))
            if (not path.is_relative_to(external) or not job_path.is_relative_to(external)
                    or path.name != "receipt.json" or job_path.name != "job_receipt.json"
                    or path.parents[2] != job_path.parent or job_path.parent.name != pointer["job_id"]):
                raise ValueError("Index observation paths differ from their declared job")
            result["source_file"] = str(path)
            job = json.loads(job_path.read_text(encoding="utf-8-sig"))
            if (job.get("schema") != "index_observation_session_job_v1"
                    or any(job.get(k) is not False for k in ("execution_admitted", "selection_enabled"))):
                raise ValueError("Index observation job boundary changed")
            from .close_workbench import process_identity
            identity = (process_identity(job["pid"], job["process_started_at"])
                        if job.get("pid") and job.get("process_started_at") else {"status": "identity_unverified"})
            result["collector"] = {"source_file": str(job_path), "recorded_status": job.get("status"),
                "updated_at": job.get("updated_at"), "attempts": job.get("attempts"),
                "successes": job.get("successes"), "stop_at": job.get("stop_at"), "process": identity}
        else:
            paths = sorted((Path(workspace) / "outputs/leader_environment_research").glob(
                "*/sina_index_probe_v*/receipt.json"), key=lambda p: (p.parent.parent.name, p.parent.name), reverse=True)
            if not paths:
                return result
            path = paths[0]
            result["source_file"] = str(path)
        source = json.loads(path.read_text(encoding="utf-8"))
        if result.get("collector") and source.get("job_id") != pointer["job_id"]:
            raise ValueError("Latest index attempt belongs to another job")
        result["observation_attempt_status"] = source.get("status")
        if source.get("error"):
            result["error"] = source["error"]
        if (source.get("schema") != "sina_fixed_three_index_quote_observation_v1"
                or source.get("source_scope") != "three_fixed_index_proxies"
                or any(source.get(key) is not False for key in
                       ("all_A_breadth", "is_BrokerQuote", "selection_enabled", "execution_admitted"))):
            raise ValueError("Saved observation research boundary changed")
        if source.get("status") != "complete_current_fixed_index_proxy_observation":
            return {**result, "status": "unavailable_latest_index_observation"}
        rows = source["quotes"]
        if len(rows) != 3 or {row["symbol"] for row in rows} != TARGETS:
            raise ValueError("Expected all three unique fixed indices")
        quotes = []
        for row in rows:
            stamp = datetime.fromisoformat(row["source_quote_time"])
            if stamp.tzinfo is None:
                raise ValueError("Provider timestamp requires a timezone")
            last, previous = float(row["last_index_points"]), float(row["previous_close_index_points"])
            if not all(math.isfinite(v) and v > 0 for v in (last, previous)):
                raise ValueError("Invalid index price")
            age = (now - stamp).total_seconds()
            same_day = stamp.astimezone(now.tzinfo).date() == now.date()
            # Do not trust the saved current_quote or age_seconds after the
            # original fetch; a successful observation can now be expired.
            current = same_day and 0 <= age <= 120
            quotes.append({"symbol": row["symbol"], "name": row["source_name"],
                "source_quote_time": stamp.isoformat(), "age_seconds_at_read": age,
                "last_index_points": last, "change_percent": (last / previous - 1) * 100,
                "current_at_read": current,
                "status": "within_120_seconds" if current else "future_source_time_unknown" if age < 0 else "stale"})
        return {**result, "status": "within_120_seconds_fixed_index_observation" if all(q["current_at_read"] for q in quotes)
                else "saved_index_observation_not_current", "quotes": quotes,
                "source_observed_at": source["response_received_at"], "source_scope": source["source_scope"]}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        return {**result, "status": "unavailable_invalid_saved_observation", "error": str(error)}


def index_quote_observation(workspace, *, data_root=None, now=None):
    now = now or datetime.now().astimezone()
    result = _index_quote_observation(workspace, data_root=data_root, now=now)
    if data_root is not None and (Path(data_root) / "research/index_observation/latest.json").is_file():
        from .index_observation_change import index_observation_change
        result["recent_change"] = index_observation_change(
            data_root, now=now, expected_latest_receipt=result.get("source_file"))
    return result
