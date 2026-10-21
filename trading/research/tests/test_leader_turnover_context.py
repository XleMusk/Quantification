"""Turnover is an exact-date descriptive overlay, never a rule replacement."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import baostock_bulk_close as bulk
from trading.research import baostock_bulk_extras as extras
from trading.research import leader_environment_run as runner
from trading.research.leader_environment import ARCHETYPES
from trading.research.leader_turnover_context import turnover_context
from trading.research.tests.test_baostock_bulk_close import sources, import_package, rewrite_bulk


@pytest.fixture
def context(sources):
    probe, _, calendar, target = sources
    def modify(frame):
        frame.loc[frame.code.eq("sz.000002"), "turn"] = ""
        frame.loc[frame.code.eq("sh.600001"), "turn"] = "3"
        frame.loc[frame.code.eq("sz.000001"), "peTTM"] = "-5.75"
    rewrite_bulk(probe, modify)
    package, _ = import_package(sources)
    metadata, raw, _ = bulk.load_complete_bulk(package)
    folder, result = extras.build(package, target.parent / "extras")
    features = raw.loc[raw.tradestatus.eq("1") & raw.isST.eq("0")].copy()
    features["symbol"] = features.code.str[3:] + features.code.map(lambda code: ".SSE" if code.startswith("sh.") else ".SZSE")
    features = features.rename(columns={"pctChg": "pct_chg"})
    candidate = {"symbol": "000001.SZSE", "source_day": "2026-10-13"}
    analysis = {"day": "2026-10-13", "frozen_rule_spec": {"unchanged": True}, "data_availability": {"turnover_rate": False},
        "archetypes": [{"id": key, "name": label, "status": "matched" if i == 0 else "unknown",
            "candidate_count": 1 if i == 0 else 0, "candidates": [candidate] if i == 0 else [],
            "missing_evidence": ["intraday_evidence"], "unresolved_rebound_symbols": ["000002.SZSE"] if i == 4 else []}
            for i, (key, label) in enumerate(ARCHETYPES)]}
    provenance = {"features_match_current_active_raw": True, "source_folder": str(package),
        "raw_daily_sha256": metadata["receipt"]["raw_daily_sha256"]}
    return {"path": folder / "result.json", "features": features, "analysis": analysis,
        "source": metadata, "provenance": provenance, "out_parent": target.parent, "calendar": calendar}


def call(value, **changes):
    args = dict(current_source=value["source"], current_provenance=value["provenance"],
                observed_at=datetime.now().astimezone().isoformat())
    args.update(changes)
    return turnover_context(value["path"], value["features"], value["analysis"], **args)


def test_complete_scope_quantiles_unknown_groups_and_frozen_analysis_are_preserved(context):
    before = deepcopy(context["analysis"])
    file_before = bulk.digest(context["path"].parent / "dated_extras.parquet")
    result = call(context)
    assert result["active_non_ST_rows"] == 2
    assert result["ordinary_inventory_rows"] == 3 and result["suspended_inventory_rows"] == 1
    assert result["overall"]["coverage_fraction"] == 1
    assert result["overall"]["median_percent"] == 2
    assert result["overall"]["quantiles_percent"]["p25"] == 1.5
    assert result["archetypes"][0]["description"]["median_percent"] == 1
    unknown = result["archetypes"][-1]
    assert unknown["existing_status"] == "unknown" and unknown["known_proxy_candidate_count"] == 0
    assert unknown["description"]["median_percent"] is None
    assert unknown["description"]["coverage_fraction"] is None
    assert unknown["leader_identity_confirmed_by_turnover"] is False
    assert context["analysis"] == before
    rows = pd.read_parquet(context["path"].parent / "dated_extras.parquet")
    assert rows.loc[rows.code.eq("sz.000001"), "peTTM_numeric"].item() == -5.75
    assert rows.loc[rows.tradestatus.eq("0"), "turn_numeric"].isna().all()
    assert file_before == bulk.digest(context["path"].parent / "dated_extras.parquet")


@pytest.mark.parametrize("kind", ["day", "scope", "raw", "roster", "candidate"])
def test_wrong_day_partial_scope_or_mixed_source_cannot_be_described(context, kind):
    if kind == "day":
        context["analysis"]["day"] = "2026-10-14"
    elif kind == "scope":
        context["features"] = context["features"].iloc[:1]
    elif kind == "raw":
        context["provenance"]["raw_daily_sha256"] = "different raw source"
    elif kind == "roster":
        path = context["path"]
        result = json.loads(path.read_text(encoding="utf-8"))
        result["roster_raw_sha256"] = "different own-day roster"
        path.write_text(json.dumps(result), encoding="utf-8")
    else:
        context["analysis"]["archetypes"][0]["candidates"][0]["source_day"] = "2026-10-12"
    with pytest.raises(ValueError):
        call(context)


def test_halt_null_cannot_be_replaced_by_numeric_zero_even_with_updated_file_hash(context):
    path = context["path"]
    record = json.loads(path.read_text(encoding="utf-8"))
    data = pd.read_parquet(record["data_file"])
    data.loc[data.tradestatus.eq("0"), "turn_numeric"] = 0
    data.to_parquet(record["data_file"], index=False)
    record["data_sha256"] = bulk.digest(record["data_file"])
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="signs, blanks or values"):
        call(context)


def test_future_derived_clock_is_not_a_usable_dated_turnover_context(context):
    record = json.loads(context["path"].read_text(encoding="utf-8"))
    record["actual_derived_at"] = record["available_at"] = "2099-10-15T12:00:00+08:00"
    context["path"].write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="clock"):
        call(context)


def test_runner_opt_in_keeps_frozen_analysis_and_rejects_wrong_day_before_mkdir(context, monkeypatch):
    from trading.research import leader_environment
    envelope = {"day": "2026-10-13", "source": context["source"]}
    monkeypatch.setattr(runner, "read_preview", lambda *a, **k: (envelope, context["features"], context["provenance"]))
    monkeypatch.setattr(leader_environment, "build_environment", lambda *a, **k: deepcopy(context["analysis"]))
    root = context["out_parent"]
    default = runner.analyze([root], context["calendar"], root / "default")
    result = runner.analyze([root], context["calendar"], root / "context", bulk_extras=context["path"])
    assert "turnover_context" not in default and "data_availability" not in default
    assert result["analysis"] == default["analysis"]
    assert result["analysis"]["data_availability"]["turnover_rate"] is False
    assert result["data_availability"]["observed_turnover_context"] is True
    context["analysis"]["day"] = "2026-10-14"
    with pytest.raises(ValueError, match="same-day"):
        runner.analyze([root], context["calendar"], root / "rejected", bulk_extras=context["path"])
    assert not (root / "rejected").exists()
