"""Run fixed F01 signals through the existing Engine/Ledger once.

This adapter supplies the missing research-side bridge for F2.  It wraps an
existing feed, injects a typed F01 SignalFrame at each session, and records the
shared account deltas.  The simulated broker remains the only broker adapter;
no external client or live permission is reachable from this module.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from .f01_financial_factor import F01Result
from .f01_signal import signal_frame, signal_provenance
from .run_identity import attempt_key, run_key


STAGES = ("Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position")
COST_MODEL_ID = "cn_a_share_strategy_config_fees_v1"


@dataclass
class F01SignalFeed:
    """Delegate all market/account feed behavior and replace only its signal source."""

    base: Any
    results_by_day: Mapping[str, Mapping[str, F01Result]]
    context: RunContext

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context != self.context:
            raise ValueError("F01 signal context differs from account context")
        results = self.results_by_day.get(day, {})
        return signal_frame(results, day, context)


def replay_f01(feed: Any, results_by_day: Mapping[str, Mapping[str, F01Result]],
               config: StrategyConfig, context: RunContext | None = None,
               *, identity: Mapping[str, Any] | None = None, attempt: int = 0) -> dict[str, Any]:
    """Replay all pinned feed sessions and return a traceable account bundle."""

    config = config.validate()
    context = context or RunContext("f01_account_replay", TradingMode.REPLAY)
    if not isinstance(context, RunContext) or context.mode not in {TradingMode.REPLAY, TradingMode.PAPER}:
        raise ValueError("F01 account replay requires REPLAY or PAPER context")
    if not hasattr(feed, "calendar") or not isinstance(results_by_day, Mapping):
        raise TypeError("feed calendar and F01 results are required")
    wrapper = F01SignalFeed(feed, results_by_day, context)
    state = new_state(config.initial_cash)
    daily = []
    for session_number, day in enumerate(wrapper.calendar):
        before = {
            "orders": set(state["orders"]), "fills": set(state["fills"]),
            "events": len(state["events"]), "cash": state["cash"],
        }
        advance(state, wrapper, day, config, session_number, context=context)
        signal = wrapper.signal_frame(day, config, context)
        result_rows = wrapper.results_by_day.get(day, {})
        accepted_symbols = [row["symbol"] for row in signal.ranked_candidates()] if isinstance(signal, SignalFrame) else []
        new_orders = sorted(set(state["orders"]) - before["orders"])
        new_fills = sorted(set(state["fills"]) - before["fills"])
        daily.append({
            "day": day,
            "data": {"source": "pinned_feed", "rows": len(wrapper.rows.get(day, {}))},
            "signal": signal.to_dict() if isinstance(signal, SignalFrame) else {"status": "UNAVAILABLE", "reason": signal.reason},
            "signal_provenance": signal_provenance(result_rows, accepted_symbols=accepted_symbols),
            "target_portfolio": deepcopy(state.get("targets") or state.get("pending_targets")),
            "orders": [deepcopy(state["orders"][key]) for key in new_orders],
            "broker_adapter": {"kind": "simulated_broker", "execution_admitted": False},
            "fills": [deepcopy(state["fills"][key]) for key in new_fills],
            "cash_position": {"cash_cents": state["cash"], "positions": deepcopy(state["positions"]),
                               "equity_cents": state.get("equity")},
            "event_count_delta": len(state["events"]) - before["events"],
        })
    result = {
        "schema": "f2_f01_account_replay_v1", "status": "completed_f01_account_replay",
        "context": context.to_dict(), "stages": list(STAGES), "daily": daily,
        "final_state": deepcopy(state), "account_replay": True,
        "execution_admitted": False,
        "broker_adapter": "simulated_broker_only",
        "comparison_contract": {
            "benchmark_config_id": identity.get("benchmark_config_id") if identity is not None else None,
            "benchmark_binding_status": "BOUND" if identity is not None and identity.get("benchmark_config_id") else "UNBOUND_IDENTITY",
            "cost_model_id": COST_MODEL_ID,
            "cost_model": {
                "commission": config.commission,
                "minimum_commission": config.minimum_commission,
                "sell_tax": config.sell_tax,
                "transfer_fee": config.transfer_fee,
                "slippage_bps": config.slippage_bps,
                "participation": config.participation,
                "dividend_tax_reserve": config.dividend_tax_reserve,
            },
            "observation_window": {
                "first_session": wrapper.calendar[0] if wrapper.calendar else None,
                "last_session": wrapper.calendar[-1] if wrapper.calendar else None,
                "session_count": len(wrapper.calendar),
                "calendar_id": identity.get("calendar_id") if identity is not None else None,
            },
        },
        "limitations": ["The feed and F01 facts must be bound to a real source before market evaluation.",
                         "This adapter never logs into, submits to or cancels an external broker."],
    }
    if identity is not None:
        result["run_key"] = run_key(identity)
        result["attempt"] = attempt
        result["attempt_key"] = attempt_key(identity, attempt)
    if not isinstance(result["final_state"].get("cash"), int):
        raise ValueError("Completed F01 account replay must contain integer-cent cash")
    return result


__all__ = ["F01SignalFeed", "STAGES", "replay_f01"]


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def publish_bundle(result: Mapping[str, Any], destination: str | Path) -> Path:
    """Publish a completed replay only after all stage files are durable."""

    if not isinstance(result, Mapping) or result.get("status") != "completed_f01_account_replay":
        raise ValueError("Only a completed F01 replay can be published")
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"Bundle destination must be new: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    daily_path, state_path, run_path = destination / "daily.json", destination / "final_state.json", destination / "run.json"
    _write_json_atomic(daily_path, result["daily"])
    _write_json_atomic(state_path, result["final_state"])
    run = {key: deepcopy(value) for key, value in result.items() if key not in {"daily", "final_state"}}
    run.update({"published": False, "daily_file": daily_path.name, "final_state_file": state_path.name})
    _write_json_atomic(run_path, run)
    files = {}
    for path in (daily_path, state_path, run_path):
        files[path.name] = {"sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    manifest = {"schema": "f2_f01_account_bundle_manifest_v1", "status": "PUBLISHED",
                "execution_admitted": False, "account_replay": True, "files": files}
    _write_json_atomic(destination / "manifest.json", manifest)
    run["published"] = True
    run["manifest"] = "manifest.json"
    _write_json_atomic(run_path, run)
    # The run hash in the manifest is updated after its final write.
    files[run_path.name] = {"sha256": sha256(run_path.read_bytes()).hexdigest(), "bytes": run_path.stat().st_size}
    _write_json_atomic(destination / "manifest.json", {**manifest, "files": files})
    return destination


def load_bundle(destination: str | Path) -> dict[str, Any]:
    """Read a published bundle and verify every registered file hash."""

    destination = Path(destination).resolve()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "PUBLISHED" or manifest.get("execution_admitted") is not False:
        raise ValueError("Only an unadmitted published F01 bundle is readable")
    for name, info in manifest.get("files", {}).items():
        path = (destination / name).resolve()
        path.relative_to(destination)
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != info.get("sha256"):
            raise ValueError(f"Published bundle file hash mismatch: {name}")
    run = json.loads((destination / "run.json").read_text(encoding="utf-8"))
    if not run.get("published") or run.get("account_replay") is not True:
        raise ValueError("Published F01 run marker is incomplete")
    return {"manifest": manifest, "run": run,
            "daily": json.loads((destination / run["daily_file"]).read_text(encoding="utf-8")),
            "final_state": json.loads((destination / run["final_state_file"]).read_text(encoding="utf-8"))}


def restore_bundle(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Restore a published bundle into an empty directory and re-verify it.

    Only files named by the source manifest are copied.  The manifest itself
    is published last, so a failed copy cannot be mistaken for a readable
    bundle.  This is a paper/research artifact restore and never restores a
    live account or authorizes an external broker action.
    """

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("Source and destination must differ")
    source_loaded = load_bundle(source)
    manifest = source_loaded["manifest"]
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Restore destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in manifest.get("files", {}):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.name != name:
            raise ValueError(f"Unsafe bundle file name: {name}")
        source_file = source / relative
        if not source_file.is_file():
            raise ValueError(f"Manifest file is missing: {name}")
        temporary = destination / (name + ".tmp")
        shutil.copy2(source_file, temporary)
        temporary.replace(destination / name)
    manifest_tmp = destination / "manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_tmp.replace(destination / "manifest.json")
    restored = load_bundle(destination)
    if restored["manifest"] != source_loaded["manifest"]:
        raise ValueError("Restored manifest differs from source")
    return restored


__all__ += ["load_bundle", "publish_bundle", "restore_bundle"]
