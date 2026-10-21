"""Prepare an explicit new-cash low-volatility paper inception, without execution."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

from ..contracts import MARKET_TZ
from .native_lowvol_publication import load_publication
from .recent_action_extension import extension_readiness
from .leader_known_identity_daily import _read, _ref, _stamp


SCHEMA = "native_lowvol_explicit_new_bootstrap_generation_v1"


def _separate(out, roots):
    out = Path(out).resolve()
    for root in roots:
        root = Path(root).resolve()
        if out == root or out in root.parents or root in out.parents:
            raise ValueError("New low-volatility account/source must be separate from prior accounts and evidence")


def prepare(head, publication, forward_run_dir, source_directory, recent_action_extension,
        route_id="native_lowvol_bootstrap_20261019"):
    """Validate a real new publication; return metadata only, never start Engine."""
    loaded = load_publication(publication)
    old = head["lowvol"]
    day = head["profile"]["last_source_day"]
    run, source = Path(forward_run_dir).resolve(), Path(source_directory).resolve()
    if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", route_id) or route_id in {"native_lowvol", "fixed_lowvol"}
            or loaded["source_day"] != day or head["last_source_day"] != day
            or loaded.get("inception") != "bootstrap_latest_saved_monthly_rank"
            or loaded.get("bootstrap_applied") is not True
            or loaded.get("account_ref") is not None or loaded.get("previous_publication_ref") is not None
            or loaded.get("account_snapshot") is not None
            or loaded["publication_ref"]["file"] in old["publications"]):
        raise ValueError("Only an explicit independent source-D bootstrap, never a replay/reset, may be adopted")
    previous = _read(old["publications"][-1])
    if (previous.get("config") != loaded["config"]
            or previous.get("signal_source_identity") != loaded["signal_source_identity"]):
        raise ValueError("New bootstrap cannot change the fixed low-volatility strategy/configuration")
    if (Path(old["run_directory"]) / "head.json").exists():
        raise ValueError("This never-started generation replacement cannot migrate an existing low-volatility account")
    roots = [old["run_directory"], old["source_directory"], Path(publication).resolve().parent,
        Path(recent_action_extension).resolve().parent]
    _separate(run, roots + [source]); _separate(source, roots + [run])
    if run.exists() or source.exists():
        raise FileExistsError("New bootstrap account and saved source need unused directories")
    extension = extension_readiness(recent_action_extension, old["action_event_view"],
        old["action_reference_view"], loaded["calendar_ref"]["file"])
    # The explicit prospective supplement is a prefix, not a claim that the
    # future E or its complete action observations already exist.
    if _stamp(loaded["published_at"]) > datetime.now(MARKET_TZ):
        raise ValueError("New bootstrap publication cannot use a future actual clock")
    value = {**deepcopy(old), "run_directory": str(run), "source_directory": str(source),
        "route_id": route_id, "publications": [loaded["publication_ref"]["file"]],
        "raw_closes": [loaded["source_proof"]["source_folder"]],
        "recent_action_extension": str(Path(recent_action_extension).resolve())}
    value.pop("last_processed_day", None)
    generation = {"schema": SCHEMA, "adopted_at": datetime.now(MARKET_TZ).isoformat(),
        "source_day": loaded["source_day"], "first_execution_day": loaded["execution_day"],
        "published_at": loaded["published_at"], "publication": loaded["publication_ref"],
        "route_id": route_id, "previous_route_id": old.get("route_id", "native_lowvol"),
        "prior_lowvol_snapshot": deepcopy(old), "recent_action_extension": _ref(recent_action_extension),
        "extension_observation": {k: extension.get(k) for k in ("window_start", "window_end", "ready_for_day")},
        "original_account_migrated": False, "original_failure_rewritten": False,
        "research_account_created": False, "engine_advanced": False, "orders_generated": False,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
        "candidate_panel_only": True}
    return {"lowvol": value, "generation": generation}
