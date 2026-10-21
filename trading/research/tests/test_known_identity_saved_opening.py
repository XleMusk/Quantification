"""The new method's actual synthetic Engine books use the saved opening path."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trading.research import leader_eight_type_opening as bridge
from trading.research import leader_eight_type_position_view as positions
from trading.research import opening_daily as coordinator
from trading.research import opening_daily_artifacts as factory
from trading.research import opening_daily_view as view
from trading.research import opening_pack
from trading.research import saved_leader_account_intent as saved
from trading.research.opening_daily_detail import opening_daily_detail
from trading.research.opening_daily_ledger import opening_daily_ledger
from trading.research.saved_account_intent import read_request
from trading.research.tests.test_leader_known_identity_paper import crossing_evidence, evidence, run_actual


FAMILY = "known_identity"
NOW = datetime.fromisoformat("2026-10-19T20:30:00+08:00")
E = "2026-10-20"
CASE = "cross_regime_candidate__unguarded__uncapped"


def test_real_four_engine_books_to_intent_pack_detail_and_selected_ledger_are_readonly(
        crossing_evidence, tmp_path, monkeypatch):
    directory, result = run_actual(crossing_evidence, tmp_path, monkeypatch)
    assert result["case_count"] == 4 and result["newly_committed_case_sessions"] == 8
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    original = json.loads((directory / "head.json").read_text(encoding="utf-8"))
    checked = positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    row = next(row for row in checked["rows"] if row["case_id"] == CASE)
    assert checked["case_count"] == 4 and row["position_count"] > 0
    assert row["context"]["account_id"].startswith("leader-known-identity-forward:")
    payload = saved.export_request(directory, CASE, E, current=NOW, source_family=FAMILY)
    assert payload["schema"] == saved.KNOWN_SCHEMA and payload["source_family"] == FAMILY
    assert read_request(payload, current=NOW) == payload
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path / "data")
    output, result = bridge.build(directory, E, tmp_path / "opening", current=NOW, source_family=FAMILY)
    assert result["case_count"] == result["preview_count"] == 4
    pack = json.loads(Path(result["opening_pack"]).read_text(encoding="utf-8"))
    sidecar = Path(pack["holding_review_ref"]["path"])
    assert sha256(sidecar.read_bytes()).hexdigest() == pack["holding_review_ref"]["sha256"]
    assert json.loads(sidecar.read_text(encoding="utf-8"))["source_family"] == FAMILY
    route = {"id": FAMILY, "family": FAMILY, "source": str(directory),
        "adopt": {"result": str(output / "result.json"), "opening_pack": result["opening_pack"]}}
    product = factory.adopt(route, factory._reference(directory / "head.json"), route["adopt"])
    assert product["case_count"] == product["ready_case_count"] == 4
    monkeypatch.setattr(coordinator, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(coordinator, "_lock", lambda _: nullcontext())
    run = tmp_path / "data" / view.RUN_RELATIVE
    coordinator.register(run, [route], current=NOW)
    monkeypatch.setattr(coordinator, "_factory", lambda: pytest.fail("Read-only status imported producer"))
    status = view.opening_daily_view(tmp_path / "data", now=NOW)
    assert status["routes"][0]["family"] == FAMILY and status["factory_called"] is False
    detail = opening_daily_detail(tmp_path / "data", FAMILY, now=NOW)
    selected = next(case for case in detail["cases"] if case["case_id"] == CASE)
    assert selected["holding_review_status"] == "saved_product_bound"
    assert selected["holding_review"]["positions"] == row["positions"]
    assert selected["intent"]["context"] == payload["context"]
    ledger = opening_daily_ledger(tmp_path / "data", FAMILY, CASE, now=NOW)
    assert ledger["status"] == "saved_completed_research_ledger"
    assert ledger["context"] == row["context"] and ledger["ledger_day"] == "2026-10-19"
    assert before == {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    changed = deepcopy(original)
    changed["binding"]["protocol"]["types"] = []
    (directory / "head.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="crossing method"):
        positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    changed = deepcopy(original)
    changed["cases"][CASE]["state"]["replay_risk_exit_policy"]["policy_id"] = "wrong-policy"
    (directory / "head.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="policy or context"):
        positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    (directory / "head.json").write_bytes(before[directory / "head.json"])
    code = ('import sys; from trading.research.opening_daily import status; '
        'value=status(sys.argv[1]); assert value["routes"][0]["family"]=="known_identity"; '
        'assert "trading.engine" not in sys.modules; '
        'assert "trading.research.leader_known_identity_paper" not in sys.modules')
    proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(run)],
        capture_output=True, text=True, env={**os.environ, "QUANT_TRADING_HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_head_remains_four_unknown_cases_without_account_or_orders(tmp_path):
    source = tmp_path / "not_created"
    result = saved.export_requests(source, E, current=NOW, source_family=FAMILY)
    assert list(result) == list(positions.KNOWN_CASE_IDS)
    assert all(row["schema"] == saved.KNOWN_NOT_READY_SCHEMA and row["status"] == "not_ready"
        and "weights" not in row for row in result.values())
    assert not source.exists()
