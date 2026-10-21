"""Saved-file append boundaries with synthetic source readers and type producers.

The existing assembler fixture supplies source/IPO/feature adapters. Manifest,
Parquet, saved-input validation, paper source bindings, _facts and _same_lineage
remain real. This is not a provider-to-account end-to-end or a strategy replay.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_eight_type_inputs as inputs
from trading.research import leader_eight_type_panel_replay as replay
from trading.research import leader_eight_type_paper as paper
from trading.research import leader_eight_type_forward_run as forward
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_leader_eight_type_inputs import A, B, C, DAYS, source

REAL_INDUSTRY = inputs._industry
EXTENDED = [*DAYS, "2026-10-15", "2026-10-16"]
CROSS = "cross_regime_candidate"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


@pytest.fixture
def append_sources(source, tmp_path, monkeypatch):
    e = source
    clock = {"value": datetime.fromisoformat("2026-10-15T18:00:00+08:00")}
    monkeypatch.setattr(inputs, "_now", lambda: clock["value"])

    class SyntheticClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["value"].astimezone(tz) if tz is not None else clock["value"]

    monkeypatch.setattr(replay, "datetime", SyntheticClock)
    protocol = read(e["protocol"])
    protocol.update(deepcopy(replay.PROTOCOL))
    protocol.update(schema="leader_eight_type_fixed_comparison_protocol_v1",
        recorded_at_utc="2026-10-15 09:00:00 UTC",
        pre_run_clarification={"recorded_at_utc": "2026-10-15 09:00:00 UTC"},
        risk_rule_fixed_before_new_actual_run={"recorded_at_utc": "2026-10-15 09:00:00 UTC"})
    save(e["protocol"], protocol)

    # The ordinary full-date source adapters stay synthetic; their saved preview
    # and raw/feature file pins are actual bytes, not made-up hash strings.
    for day in EXTENDED[3:]:
        envelope, features, provenance = deepcopy(e["loaded"][DAYS[-1]])
        envelope["day"] = day
        envelope["source"]["source_folder"] = str(tmp_path / day)
        features["date"] = day
        old_raw, metadata = deepcopy(e["actual"][str(tmp_path / DAYS[-1])])
        old_raw["date"] = day
        metadata["day"] = day
        e["actual"][str(tmp_path / day)] = old_raw, metadata
        e["loaded"][day] = envelope, features, provenance
    previews, proofs = {}, []
    for day in EXTENDED:
        raw, metadata = e["actual"][str(tmp_path / day)]
        folder = tmp_path / day
        folder.mkdir(exist_ok=True)
        raw_file = folder / "synthetic_ordinary.csv"
        raw.to_csv(raw_file, index=False)
        proof = {"day": day, "source_folder": str(folder), "source_updated_at": day + "T17:00:00+08:00",
            "source_completed_at": day + "T17:00:00+08:00", "raw_daily_sha256": digest(raw_file)}
        proofs.append(proof)
        envelope, features, provenance = e["loaded"][day]
        envelope["created_at"] = ("2026-10-15T17:30:00+08:00" if day <= DAYS[-1] else day + "T18:30:00+08:00")
        provenance["recent_source_view"]["source_proofs"] = deepcopy(proofs)
        provenance["recent_source_view"]["source_days_used"] = EXTENDED[:len(proofs)]
        if day > DAYS[-1]:
            provenance["recent_source_view"]["shared_view_id"] = "new-derived-view-" + day
        preview = tmp_path / "synthetic_previews" / day
        preview.mkdir(parents=True)
        save(preview / "result.json", envelope)
        features.to_csv(preview / "current_features.csv", index=False)
        provenance.update(preview=str(preview / "result.json"), preview_sha256=digest(preview / "result.json"),
            feature_file=str(preview / "current_features.csv"), feature_sha256=digest(preview / "current_features.csv"),
            source_folder=str(folder), raw_daily_sha256=proof["raw_daily_sha256"], features_match_current_active_raw=True)
        previews[day] = preview
    def preview(folder, **kwargs):
        day = Path(folder).name
        e["calls"]["preview"].append(folder)
        return deepcopy(e["loaded"][day])
    monkeypatch.setattr(inputs, "read_preview", preview)
    def proof(raw, metadata, calendar_hash):
        return deepcopy(next(p for p in proofs if p["day"] == metadata["day"]))
    monkeypatch.setattr(inputs, "source_proof", proof)
    monkeypatch.setattr(replay, "load_complete", inputs.load_complete)
    monkeypatch.setattr(replay, "source_proof", proof)
    monkeypatch.setattr(replay, "load_ipo_qualification", inputs.load_ipo_qualification)
    anchor = {"day": "2026-10-09", "closes": {s: 10. for s in (A, B, C)}}
    monkeypatch.setattr(replay, "_context_anchor", lambda v: (deepcopy(anchor), {"synthetic_fixed_anchor": "2026-10-09"}))

    industry = tmp_path / "dated_industry"
    industry.mkdir()
    industry_file = industry / "2026-09-30.parquet"
    pd.DataFrame({"date": ["2026-09-30"]*3, "symbol": [A, B, C], "industry": ["x"]*3,
        "industryClassification": ["broad"]*3, "updateDate": ["2026-09-30"]*3}).to_parquet(industry_file)
    save(industry / "result.json", {"status": "completed_requested_industry_history",
        "source": "BaoStock.query_stock_industry(date=decision_day)", "execution_admitted": False,
        "entries": [{"date": "2026-09-30", "file": industry_file.name, "rows": 3,
                     "retrieved_at": "2026-10-04T12:00:00+08:00"}]})
    monkeypatch.setattr(inputs, "_industry", REAL_INDUSTRY)

    registry = tmp_path / "fixed_registry.json"
    save(registry, {"synthetic_only": True, "fixed_members": [A]})
    tracking = {"registry": str(registry), "registry_sha256": digest(registry),
        "read_at": clock["value"].isoformat(), "members": [{"symbol": A}],
        "registered_at": "2026-10-15T11:54:00+08:00", "observations": [], "execution_admitted": False}
    current = {"value": tracking}
    monkeypatch.setattr(inputs, "report", lambda p: deepcopy(current["value"]))
    missing = inputs.build_missing_type_candidates
    def dated_missing(**kwargs):
        value = missing(**kwargs)
        if kwargs["identity_tracking"].get("observations"):
            item = next(row for row in value["types"] if row["id"] == CROSS)
            item.update(status="matched", candidates=[{"symbol": A, "amount_cny": 100000000.,
                "date": kwargs["day"], "source_day": kwargs["day"],
                "evidence": {"registry_sha256": kwargs["identity_tracking"]["registry_sha256"],
                             "registered_at": kwargs["identity_tracking"]["registered_at"]}}], unresolved_symbols=[])
        return value
    monkeypatch.setattr(inputs, "build_missing_type_candidates", dated_missing)

    def assemble(days, out, append_from=None, identity=None):
        return inputs.assemble([previews[d] for d in days], e["calendar"], "ipo", "master",
            registry if identity is None else identity, industry_file, [], e["protocol"], out,
            **({"append_from": append_from} if append_from is not None else {}))

    base_dir = tmp_path / "immutable_base"
    base = assemble(DAYS, base_dir)
    base_file = base_dir / "manifest.json"
    base_bytes = {p: p.read_bytes() for p in base_dir.rglob("*") if p.is_file()}
    clock["value"] = datetime.fromisoformat("2026-10-15T19:00:00+08:00")
    current["value"] = {**deepcopy(tracking), "read_at": clock["value"].isoformat(), "observations": [{"day": "2026-10-15"}]}
    return {**e, "assemble": assemble, "base": base, "base_file": base_file, "base_bytes": base_bytes,
        "clock": clock, "current_tracking": current, "registry": registry, "previews": previews,
        "proofs": proofs, "append_dir": tmp_path / "appended"}


def as_loaded(path):
    # Real loader: all saved source rows/nulls/proofs/calendar/protocol are checked.
    return replay.load_inputs(path, "ipo", "master")


def test_append_only_derives_new_D_preserves_old_unknown_and_frozen_lineage(append_sources):
    e = append_sources
    before_env = len(e["calls"]["environment"])
    before_missing = len(e["calls"]["missing"])
    result = e["assemble"](["2026-10-15"], e["append_dir"], append_from=e["base_file"])
    assert len(e["calls"]["environment"]) == before_env + 1
    assert e["calls"]["environment"][-1] == [*DAYS, "2026-10-15"]
    assert len(e["calls"]["missing"]) == before_missing + 1 and e["calls"]["missing"][-1]["day"] == "2026-10-15"
    assert result["panels"][:-1] == e["base"]["panels"]
    assert result["scope_members"] == [A, B, C] and result["scope_frozen_at"] == e["base"]["scope_frozen_at"]
    assert result["source_proofs"][:-1] == e["base"]["source_proofs"]
    assert result["preview_sources"][:-1] == e["base"]["preview_sources"]
    assert result["shared_view_id"] == "new-derived-view-2026-10-15" and result["shared_view_id"] != e["base"]["shared_view_id"]
    assert result["identity_report"]["registry_sha256"] == e["base"]["identity_report"]["registry_sha256"]
    old_type = lambda row: next(item for item in row["types"] if item["id"] == CROSS)
    assert all(old_type(row)["status"] == "unknown" and old_type(row)["unresolved_symbols"] == [A]
               for row in result["panels"][:-1])
    assert old_type(result["panels"][-1])["status"] == "matched"
    assert old_type(result["panels"][-1])["candidates"][0]["symbol"] == A
    lineage = result["append_lineage"]
    assert lineage["parent_manifest_sha256"] == digest(e["base_file"])
    assert lineage["prior_last_day"] == DAYS[-1] and lineage["appended_day"] == "2026-10-15"
    assert lineage["old_panels_recomputed"] is False and lineage["old_panel_values_unchanged"] is True
    original, latest = as_loaded(e["base_file"]), as_loaded(e["append_dir"] / "manifest.json")
    for day in DAYS:
        assert forward._facts(original["panel_by_day"][day]) == forward._facts(latest["panel_by_day"][day])
    forward._same_lineage(original, latest)
    old_bound = paper._source_bindings(original, e["clock"]["value"])
    new_bound = paper._source_bindings(latest, e["clock"]["value"])
    assert old_bound["scope_frozen_at"] == new_bound["scope_frozen_at"]
    assert old_bound["source_proofs"] == new_bound["source_proofs"][:-1]
    assert all(p.read_bytes() == value for p, value in e["base_bytes"].items())


@pytest.mark.parametrize("fault", ["skip_exact_next", "changed_old_raw", "changed_registry"])
def test_append_rejects_incompatible_prefix_before_creating_output(append_sources, fault):
    e = append_sources
    day, identity = "2026-10-15", None
    if fault == "skip_exact_next":
        day = "2026-10-16"
        e["clock"]["value"] = datetime.fromisoformat(day + "T19:00:00+08:00")
        e["current_tracking"]["value"]["read_at"] = e["clock"]["value"].isoformat()
    elif fault == "changed_old_raw":
        path = Path(e["base"]["raw_file"])
        rows = pd.read_parquet(path)
        rows.loc[0, "close"] = "10.01"
        rows.to_parquet(path, index=False)
    else:
        identity = e["registry"].with_name("different_registry.json")
        save(identity, {"synthetic_only": True, "fixed_members": [B]})
        e["current_tracking"]["value"].update(registry=str(identity), registry_sha256=digest(identity))
    with pytest.raises(ValueError):
        e["assemble"]([day], e["append_dir"], append_from=e["base_file"], identity=identity)
    assert not e["append_dir"].exists()
    if fault != "changed_old_raw":
        assert all(p.read_bytes() == value for p, value in e["base_bytes"].items())
