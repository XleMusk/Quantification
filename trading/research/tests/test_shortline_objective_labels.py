"""Only new paired training labels, using tiny saved raw/adjusted endpoints."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import shortline_objective_labels as m


CALENDAR = ["2024-01-02", "2025-12-19", "2025-12-22", "2025-12-23", "2025-12-24",
    "2025-12-25", "2025-12-26", "2025-12-29", "2025-12-30", "2025-12-31",
    "2026-01-05", "2026-01-06", "2026-09-30"]
A, B, X = "600001.SSE", "600002.SSE", "300114.SZSE"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def fixture(tmp_path):
    source, registration = tmp_path/"source", tmp_path/"registration"
    source.mkdir(); registration.mkdir()
    events = [dict(event_id="known", date="2025-12-19", symbol=A, stage=1, decision_eligible=True,
        label_status="complete", label_witness_day="2025-12-23", hit_5=1),
        dict(event_id="exit_purge", date="2025-12-24", symbol=A, stage=1, decision_eligible=True,
        label_status="complete", label_witness_day="2025-12-25", hit_5=0),
        dict(event_id="alias_unknown", date="2025-12-19", symbol=X, stage=2, decision_eligible=True,
        label_status="complete", label_witness_day="2025-12-23", hit_5=0),
        dict(event_id="halted_exit", date="2025-12-19", symbol=B, stage=3, decision_eligible=True,
        label_status="complete", label_witness_day="2025-12-23", hit_5=0),
        dict(event_id="witness_purge", date="2025-12-22", symbol=A, stage=1, decision_eligible=True,
        label_status="complete", label_witness_day="2026-01-05", hit_5=1)]
    frame = pd.DataFrame(events)
    features, labels = source/"event_features.csv", source/"event_panel.parquet"
    frame[m.KEYS+["decision_eligible"]].to_csv(features, index=False)
    frame.to_parquet(labels, index=False)
    panel = write(source/"result.json", {"schema": "native_shortline_event_panel_v1",
        "status": "completed_native_causal_shortline_research_panel",
        "event_features_path": str(features), "events_path": str(labels),
        "events_sha256": m.edge.pin(labels)["sha256"],
        "files": {features.name: m.edge.pin(features)["sha256"], labels.name: m.edge.pin(labels)["sha256"]}})
    panels = {}
    for kind in ("raw", "adjusted"):
        path = source/(kind+".parquet")
        writer = None
        for day in CALENDAR:
            rows = []
            for symbol in (A, B, X):
                opening = 11.5 if day == "2025-12-22" else 12. if day == "2025-12-29" else 10.
                rows.append({"date": day, "code": ("sh." if symbol.endswith(".SSE") else "sz.")+symbol[:6],
                    "open": opening*(2 if kind == "adjusted" else 1), "close": opening,
                    "volume": 0. if symbol == B and day == "2025-12-29" else 1000000.,
                    "tradestatus": "1", "preclose": 10., "isST": "0"})
            table = pa.Table.from_pandas(pd.DataFrame(rows), preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema)
            writer.write_table(table)
        writer.close()
        panels[kind] = m.edge.pin(path)
    refs = {}
    for key, payload in (("raw_plan", {"days": CALENDAR}), ("raw_result", {}), ("adjusted_result", {})):
        refs[key] = m.edge.pin(write(source/(key+".json"), payload))
    edge_registration = write(registration/"registration.json", {
        "schema": m.edge.METHOD+"_registration", "calendar": CALENDAR, "panels": panels, "refs": refs})
    return panel, edge_registration


def test_real_saved_endpoint_reader_purges_before_2026_and_keeps_common_ID_intersection(tmp_path, monkeypatch):
    panel, registration = fixture(tmp_path)
    original = m.edge._load_endpoints
    observed = []
    def load(record, formation, schedule):
        observed.extend(formation.to_dict("records"))
        assert set(formation.symbol) == {A, B} and set(formation.date) == {"2025-12-19"}
        data, summary = original(record, formation, schedule)
        assert all(day <= m.END for day, symbol in data)
        return data, summary
    monkeypatch.setattr(m.edge, "_load_endpoints", load)
    monkeypatch.setattr(m.edge, "endpoint_proxy", lambda *args: pytest.fail("Future clearance must not filter training labels"))
    result = m.build_training_labels(panel, registration, tmp_path/"targets")
    targets = pd.read_parquet(result["training_targets"]["path"]).set_index("event_id")
    assert len(observed) == 2 and result["rows"] == 5
    assert result["joint_training_rows"] == 1 and result["purged_rows"] == 2
    assert targets.loc["known", "h5_return"] == pytest.approx(12/11.5-1)
    assert targets.loc["known", "max_label_day"] == "2025-12-29"
    assert targets.loc["known", "joint_training_eligible"]
    assert targets.loc["exit_purge", "exit_day"] == "2026-01-05"
    assert not targets.loc["halted_exit", "joint_training_eligible"]
    assert np.isnan(targets.loc["alias_unknown", "h5_return"])
    assert np.isnan(targets.loc["witness_purge", "hit5"])
    assert result["source_read"]["actual_latest_price_day_read"] == "2025-12-29"


def test_changed_original_hash_and_overlapping_outputs_are_rejected_before_price_read(tmp_path, monkeypatch):
    panel, registration = fixture(tmp_path)
    monkeypatch.setattr(m.edge, "_load_endpoints", lambda *args: pytest.fail("Rejected sources cannot read endpoint rows"))
    with pytest.raises(ValueError, match="overlap"):
        m.build_training_labels(panel, registration, panel.parent/"invalid_output")
    metadata = m.edge.read(panel)
    path = Path(metadata['event_features_path'])
    path.write_text(path.read_text(encoding="utf-8")+"\n", encoding="utf-8")
    with pytest.raises(ValueError, match="input changed"):
        m.build_training_labels(panel, registration, tmp_path/"changed_output")


def test_hit5_maturity_eligibility_and_finite_price_labels_are_joint_without_future_fallback():
    formation = pd.DataFrame([dict(event_id="not_eligible", date="2025-12-19", symbol=A, stage=1,
        decision_eligible=False, label_status="complete", label_witness_day="2025-12-23", hit_5=1),
        dict(event_id="censored", date="2025-12-19", symbol=A, stage=1,
        decision_eligible=True, label_status="right_censored", label_witness_day=None, hit_5=np.nan)])
    schedule = m.edge.make_schedule(["2025-12-19"], CALENDAR)
    rows = m._base_targets(formation, schedule)
    before = deepcopy(rows)
    value = m._finish_targets(rows, {})
    assert not value.joint_training_eligible.any() and value.h5_return.isna().all()
    assert "decision_not_eligible" in value.loc[0, "exclusion_reason"]
    assert "hit5_complete_witness_unknown" in value.loc[1, "exclusion_reason"]
    assert before[0]["entry_day"] == "2025-12-22" and before[0]["exit_day"] == "2025-12-29"
