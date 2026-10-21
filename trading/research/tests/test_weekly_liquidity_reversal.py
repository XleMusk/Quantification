from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.contracts import SignalUnavailable
from trading.research import weekly_liquidity_reversal as method


def tiny_loaded():
    before = pd.bdate_range("2023-01-03", "2023-12-29").strftime("%Y-%m-%d").tolist()
    calendar = before[:241] + [before[-1]]
    calendar += pd.bdate_range("2024-01-02", periods=40).strftime("%Y-%m-%d").tolist()
    days = method.inputs.weekly.decision_days(calendar)
    symbols = [f"{600000 + i:06}.SSE" for i in range(120)]
    turn = pd.DataFrame({s: (.2 + (i // 2) * .01) for i, s in enumerate(symbols)}, index=calendar)
    pools = {}
    for day in days:
        if calendar.index(day) < 252:
            pools[day] = SignalUnavailable("low_risk_252_session_warmup")
        else:
            pools[day] = [{"symbol": s, "reversal": (i - 60) / 1000,
                           "momentum": (i - 80) / 100, "volatility": .2 + i / 1000,
                           "median_amount": 40000000 + i} for i, s in enumerate(symbols)]
    return {"base": SimpleNamespace(calendar=calendar), "decision_days": days,
            "turnover_active": turn, "pools": pools,
            "load_observation": {"canonical_data_reads": 1, "raw_data_reads": 0}}


def test_exact_63_window_min42_and_same_pool_ties_before_future_labels():
    loaded = tiny_loaded()
    day = loaded["decision_days"][-1]
    i = loaded["base"].calendar.index(day)
    window = loaded["base"].calendar[i - 62:i + 1]
    turn = loaded["turnover_active"]
    turn.loc[window[:22], "600000.SSE"] = np.nan  # Only 41 of the exact 63.
    turn.loc[window[:21], "600001.SSE"] = np.nan  # Exactly 42, retained.
    full, frame, coverage = method.formation(loaded)
    assert len(full.loc[day]) == 120
    assert len(frame.loc[day]) == 119
    assert not full.at[(day, "600000.SSE"), "turnover_eligible"]
    assert full.at[(day, "600001.SSE"), "turn63_known_observations"] == 42
    assert frame.at[(day, "600002.SSE"), "turn_group"] == frame.at[(day, "600003.SSE"), "turn_group"]
    assert frame.at[(day, "600002.SSE"), "turn63_percent"] == pytest.approx(.21)
    assert frame.loc[day, "momentum"].lt(0).any()
    assert frame.loc[day, "reversal_score"].lt(0).any()
    assert coverage[0]["original_pool_members"] is None
    assert coverage[-1]["turnunknown_members"] == 1
    # Future formation data cannot alter any earlier D feature or membership.
    earlier = loaded["decision_days"][-2]
    altered = deepcopy(loaded)
    altered["turnover_active"].loc[altered["turnover_active"].index > earlier] = 100000
    changed = method.formation(altered)[1]
    pd.testing.assert_frame_equal(frame.loc[earlier], changed.loc[earlier])


def test_exact_open_endpoints_late_is_ratio_and_calendar_overflow_unknown():
    calendar = pd.bdate_range("2024-01-02", periods=30).strftime("%Y-%m-%d").tolist()
    days = [calendar[0], calendar[20], calendar[-1]]
    plans = method.endpoint_plan(days, calendar)
    index = pd.MultiIndex.from_product([days, ["600000.SSE"]], names=["date", "symbol"])
    frame = pd.DataFrame({"reversal_score": 1.0}, index=index)
    opening = pd.DataFrame(10.0, index=calendar, columns=["600000.SSE"])
    opening.at[calendar[6], "600000.SSE"] = 12
    opening.at[calendar[22], "600000.SSE"] = 18
    observations = {(d, "600000.SSE"): {"reason": None} for d in calendar}
    labels, rows = method._labels(frame, plans, opening, observations)
    assert plans[days[0]] == {"source_day": days[0], "entry_day": calendar[1],
                              "h5_exit_day": calendar[6], "h21_exit_day": calendar[22]}
    assert labels["h5"].iloc[0] == pytest.approx(.2)
    assert labels["h21"].iloc[0] == pytest.approx(.8)
    assert labels["late5_21"].iloc[0] == pytest.approx(.5)
    assert labels["late5_21"].iloc[0] != pytest.approx(.8 - .2)
    assert np.isnan(labels["h21"].iloc[1])
    assert all(np.isnan(labels[name].iloc[-1]) for name in method.diagnostics.ENDPOINTS)
    assert rows[-1]["entry_reason"] == "calendar_endpoint_unknown"
    # Missing exact E cannot be replaced by a later positive price.
    opening.at[calendar[1], "600000.SSE"] = np.nan
    missing, _ = method._labels(frame, plans, opening, observations)
    assert np.isnan(missing["h5"].iloc[0])
    assert missing["late5_21"].iloc[0] == pytest.approx(.5)


def test_register_is_metadata_only_then_formation_seal_precedes_endpoint_read(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    runfile = source_dir / "run.json"
    runfile.write_text("{}", encoding="utf-8")
    panel = source_dir / "canonical_daily.parquet"
    panel.write_bytes(b"synthetic-source-declaration")
    code = tmp_path / "implementation.py"
    code.write_text("# frozen synthetic implementation", encoding="utf-8")
    loaded = tiny_loaded()
    source = {"metadata_refs": {"saved_run": method.inputs._ref(runfile)},
              "canonical_panel": method.inputs._ref(panel), "calendar": loaded["base"].calendar,
              "source_clock": {"basis": "synthetic actual retrieval clock not historical publication"}}
    loaded["source"] = source
    calls = {"load": 0, "endpoints": 0}
    monkeypatch.setattr(method, "_code_files", lambda: [code])
    monkeypatch.setattr(method.inputs, "inspect_saved_weekly_inputs", lambda _: source)

    def load(_):
        calls["load"] += 1
        return deepcopy(loaded)

    monkeypatch.setattr(method.inputs, "load_inputs_from_saved_weekly", load)
    registration = tmp_path / "registration"
    output = tmp_path / "result"
    note = tmp_path / "literature" / "reference_note.json"
    note.parent.mkdir()
    note.write_text('{"primary_source":"synthetic reviewed primary reference"}', encoding="utf-8")

    def endpoint_reader(saved_source, plans, symbols):
        calls["endpoints"] += 1
        seal = json.loads((output / "formation_seal.json").read_text(encoding="utf-8"))
        assert seal["future_endpoint_data_read"] is False
        assert not (output / "labels.parquet").exists()
        for ref in seal["files"].values():
            assert method.inputs._ref(ref["path"]) == ref
        assert saved_source["panel"] == source["canonical_panel"]
        needed = sorted({p[key] for p in plans.values() for key in ("entry_day", "exit_day") if p[key] is not None})
        opening = pd.DataFrame({s: np.arange(len(source["calendar"])) + 10 + i / 10
                                for i, s in enumerate(symbols)}, index=source["calendar"])
        observations = {(d, s): {"reason": None} for d in needed for s in symbols}
        return opening, observations, needed

    monkeypatch.setattr(method, "endpoint_panel", endpoint_reader)
    record = method.register(runfile, registration, reference_note=note)
    assert calls == {"load": 0, "endpoints": 0}
    assert record["future_endpoint_data_read"] is False
    assert record["reference_note"] == method.inputs._ref(note)
    assert record["spec"]["independent_holdout_claimed"] is False
    result = method.run(registration / "registration.json", output)
    assert calls == {"load": 1, "endpoints": 1}
    assert result["formation_sealed"] is True
    assert result["engine_advanced"] is False
    assert result["returns_reportable"] is False
    assert result["registered_days"] == len(loaded["decision_days"])
    assert len(json.loads((output / "summaries.json").read_text(encoding="utf-8"))) == 72
    assert len(json.loads((output / "paired_late_daily.json").read_text(encoding="utf-8"))) == len(loaded["decision_days"])
    with pytest.raises(FileExistsError):
        method.run(registration / "registration.json", output)
    assert calls == {"load": 1, "endpoints": 1}
    with pytest.raises(ValueError, match="overlap"):
        method.register(runfile, source_dir / "illegal")
    with pytest.raises(ValueError, match="overlap"):
        method.register(runfile, note.parent / "illegal", reference_note=note)
    with pytest.raises(FileNotFoundError):
        method.register(runfile, tmp_path / "missing_note", reference_note=note.parent / "missing.json")
    assert not (tmp_path / "missing_note").exists()
    code.write_text("# changed", encoding="utf-8")
    with pytest.raises(ValueError, match="implementation changed"):
        method.run(registration / "registration.json", tmp_path / "changed")
    assert not (tmp_path / "changed").exists()
