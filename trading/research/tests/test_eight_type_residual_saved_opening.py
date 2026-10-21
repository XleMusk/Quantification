"""New family integration from real synthetic Engine books to saved products."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import leader_eight_type_opening as bridge
from trading.research import leader_eight_type_position_view as positions
from trading.research import leader_eight_type_residual_paper as method
from trading.research import opening_daily as coordinator
from trading.research import opening_daily_artifacts as factory
from trading.research import opening_daily_view as view
from trading.research import opening_pack
from trading.research import saved_leader_account_intent as saved
from trading.research.opening_daily_detail import opening_daily_detail
from trading.research.saved_account_intent import read_request
from trading.research.tests.test_leader_eight_type_forward_run import evidence, CALENDAR


FAMILY = "eight_type_residual"
NOW = datetime.fromisoformat("2026-10-19T20:30:00+08:00")
E = "2026-10-20"
CASE = "space_height_leader__unguarded__uncapped"


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def residual_books(evidence, tmp_path, monkeypatch):
    e = evidence
    # The upstream synthetic loader is unchanged; add real saved quote files
    # only so the existing opening bridge can read its ordinary parquet path.
    for day, panel in e["panels"].items():
        loaded = e["loaded"][str(panel)]
        raw = tmp_path / ("raw_" + day + ".parquet")
        pq.write_table(pa.Table.from_pandas(loaded["raw"], preserve_index=False), raw)
        write(panel, {"synthetic_only": True, "source_days": loaded["source_days"],
            "raw_file": str(raw), "raw_sha256": sha256(raw.read_bytes()).hexdigest()})
        loaded["_bindings"]["input_manifest_sha256"] = sha256(panel.read_bytes()).hexdigest()
    for publication in e["pubs"]:
        record = json.loads(publication.read_text(encoding="utf-8"))
        record["source"] = deepcopy(e["loaded"][record["source"]["input_manifest"]]["_bindings"])
        write(publication, record)
    calendar = tmp_path / "calendar.json"
    write(calendar, CALENDAR)
    monkeypatch.setattr(method, "_now", lambda: datetime.fromisoformat("2026-10-15T23:00:00+08:00"))
    registration = tmp_path / "method_registration"
    method.register(e["pubs"][0], calendar, "2026-10-16", registration)
    pubs = []
    for day, base in zip(("2026-10-16", "2026-10-19"), e["pubs"][1:]):
        monkeypatch.setattr(method, "_now", lambda day=day: datetime.fromisoformat(day + "T19:00:00+08:00"))
        destination = tmp_path / ("residual_publication_" + day)
        method.publish(base, e["panels"][day], registration / "registration.json", destination)
        pubs.append(destination / "paper_plan.json")
    directory = tmp_path / "residual_books"
    result = method.forward(pubs, e["panels"]["2026-10-19"], None, None, directory,
        registration_file=registration / "registration.json")
    assert result["newly_committed_case_sessions"] == 64
    assert not e["run"].exists()
    return directory


def test_real_engine_saved_residual_positions_intent_pack_sidecar_detail_and_readonly_status(
        residual_books, tmp_path, monkeypatch):
    directory = residual_books
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    original = json.loads((directory / "head.json").read_text(encoding="utf-8"))
    checked = positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    row = next(row for row in checked["rows"] if row["case_id"] == CASE)
    assert checked["case_count"] == 32 and row["position_count"] > 0
    assert all(p["assessment"]["status"] == "invalid" and p["queued_risk_exit"]["execution_on"] == E
        and p["anchor"]["decision_day"] == "2026-10-16" for p in row["positions"])
    payload = saved.export_request(directory, CASE, E, current=NOW, source_family=FAMILY)
    assert payload["schema"] == saved.RESIDUAL_SCHEMA and payload["source_family"] == FAMILY
    assert set(payload["remaining_exit_symbols"]) == {p["symbol"] for p in row["positions"]}
    assert read_request(payload, current=NOW) == payload
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path / "data")
    output, result = bridge.build(directory, E, tmp_path / "opening", current=NOW, source_family=FAMILY)
    assert result["case_count"] == result["preview_count"] == 32
    pack = json.loads(Path(result["opening_pack"]).read_text(encoding="utf-8"))
    sidecar = Path(pack["holding_review_ref"]["path"])
    assert sha256(sidecar.read_bytes()).hexdigest() == pack["holding_review_ref"]["sha256"]
    review = json.loads(sidecar.read_text(encoding="utf-8"))
    assert review["source_family"] == FAMILY and len(review["case_reviews"]) == 32
    route = {"id": FAMILY, "family": FAMILY, "source": str(directory),
        "adopt": {"result": str(output / "result.json"), "opening_pack": result["opening_pack"]}}
    product = factory.adopt(route, factory._reference(directory / "head.json"), route["adopt"])
    assert product["case_count"] == product["ready_case_count"] == 32
    monkeypatch.setattr(coordinator, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(coordinator, "_lock", lambda _: nullcontext())
    run = tmp_path / "data" / view.RUN_RELATIVE
    coordinator.register(run, [route], current=NOW)
    monkeypatch.setattr(coordinator, "_factory", lambda: pytest.fail("Read-only status imported producer"))
    status = view.opening_daily_view(tmp_path / "data", now=NOW)
    assert status["routes"][0]["family"] == FAMILY and status["factory_called"] is False
    detail = opening_daily_detail(tmp_path / "data", FAMILY, now=NOW)
    case = next(case for case in detail["cases"] if case["case_id"] == CASE)
    assert case["holding_review_status"] == "saved_product_bound"
    assert case["holding_review"]["positions"] == row["positions"]
    assert case["intent"]["remaining_exit_symbols"] == payload["remaining_exit_symbols"]
    assert case["fills"] is None and case["returns"] is None
    assert before == {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    # Old policy or protocol dimensions cannot masquerade as the new method.
    changed = deepcopy(original)
    changed["binding"]["protocol"]["types"] = changed["binding"]["protocol"]["types"][:-1]
    write(directory / "head.json", changed)
    with pytest.raises(ValueError, match="residual method"):
        positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    changed = deepcopy(original)
    changed["cases"][CASE]["state"]["replay_risk_exit_policy"]["policy_id"] = positions.RISK_PREFIX + "space_height_leader"
    write(directory / "head.json", changed)
    with pytest.raises(ValueError, match="policy or context"):
        positions.read_saved_positions(directory, now=NOW, source_family=FAMILY)
    (directory / "head.json").write_bytes(before[directory / "head.json"])
    code = ('import sys; from trading.research.opening_daily import status; '
        'value=status(sys.argv[1]); assert value["routes"][0]["family"]=="eight_type_residual"; '
        'assert "trading.engine" not in sys.modules; '
        'assert "trading.research.leader_eight_type_residual_paper" not in sys.modules')
    proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(run)],
        capture_output=True, text=True, env={**os.environ, "QUANT_TRADING_HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_residual_head_is_unknown_32_and_never_creates_accounts(tmp_path):
    source = tmp_path / "not_created"
    result = saved.export_requests(source, E, current=NOW, source_family=FAMILY)
    assert list(result) == list(positions.CASE_IDS)
    assert all(row["schema"] == saved.RESIDUAL_NOT_READY_SCHEMA and row["status"] == "not_ready"
        and "weights" not in row for row in result.values())
    assert not source.exists()
