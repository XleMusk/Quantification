"""Read one fixed prospective proxy cohort; never register or collect data."""
from datetime import datetime
from pathlib import Path

from .leader_identity_registry import FLAGS, REPORT_SCHEMA, report


SCHEMA = "leader_prospective_proxy_tracking_view_v1"
COHORT_ID = "20261015_initial_proxy_cohort_v1"


def leader_identity_tracking_view(data_root, *, now=None):
    path = (Path(data_root).resolve() / "research" / "leader_identity_registry" / COHORT_ID / "registry.json")
    read_at = (now or datetime.now().astimezone()).isoformat()
    base = {"schema": SCHEMA, "read_at": read_at, "source_file": str(path), "fixed_cohort_id": COHORT_ID,
        "cohort_selection": "one_fixed_initial_cohort_never_latest_or_performance_selected",
        "tracking": None, "full_cohort_retained": True, "display_is_not_portfolio_selection": True,
        "three_closes_complete_intervals": 2, "cross_regime_leader_verified": False,
        "economic_return_or_PnL": False, **FLAGS}
    if not path.is_file():
        return {**base, "status": "unavailable_fixed_registry_not_registered"}
    value = report(path, **({"_clock": lambda: now} if now is not None else {}))
    members = value.get("members") or []
    if (value.get("schema") != REPORT_SCHEMA or value.get("registry") != str(path)
            or any(value.get(key) is not expected for key, expected in FLAGS.items())
            or value.get("cross_regime_leader_verified") is not False
            or value.get("rule", {}).get("economic_return_or_PnL") is not False
            or value.get("member_count") != len(members)
            or sum(value.get("status_counts", {}).values()) != len(members)):
        raise ValueError("Fixed identity tracking report scope or research boundaries changed")
    return {**base, "status": "available_fixed_proxy_tracking", "read_at": value["read_at"], "tracking": value}
