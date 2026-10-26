import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs/architecture_v3/google_applied_execution_map_20261026.json"
BINDING = ROOT / "docs/architecture_v3/work_packages_v3_binding.json"


def test_google_execution_map_binds_existing_packages_and_closed_gate():
    plan = json.loads(PLAN.read_text(encoding="utf-8-sig"))
    binding = json.loads(BINDING.read_text(encoding="utf-8"))
    package_ids = {item["id"] for item in binding["packages"]}
    decisions = plan["design_decisions"]

    assert {item["id"] for item in decisions} == {"A", "B", "C", "D"}
    assert plan["constraints"] == {
        "execution_admitted": False,
        "candidate_panel_only": True,
        "live_execution_ready": False,
        "no_real_login_or_orders": True,
    }
    referenced = {package for item in decisions for package in item["packages"]}
    assert referenced <= package_ids
    assert all(item["acceptance"] for item in decisions)


def test_google_execution_map_waves_cover_all_decisions_once():
    plan = json.loads(PLAN.read_text(encoding="utf-8-sig"))
    wave_decisions = [decision for wave in plan["delivery_waves"] for decision in wave["decisions"]]

    assert wave_decisions == ["A", "B", "C", "D"]
    assert all(wave["customer_outcome"] and wave["exit"] for wave in plan["delivery_waves"])
    assert plan["operating_loop"][0] == "select_customer_outcome"
    assert plan["operating_loop"][-1] == "dispatch_next_ready_package"
