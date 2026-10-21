"""Runner/source supervision fixtures; no historical market or real Engine run."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_type_router_replay as m


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    m._save(path, value)
    return m._pin(path)


def fixture(tmp_path):
    studies = []
    for year in (2023, 2024, 2025):
        days = pd.bdate_range(f"{year}-04-06", periods=7).strftime("%Y-%m-%d").tolist()
        root = tmp_path / "sources" / str(year)
        root.mkdir(parents=True)
        payloads = {}
        for key in ("raw", "context_raw", "panels"):
            path = root / (key + ".parquet")
            path.write_bytes(b"saved synthetic payload, register must not load")
            payloads[key + "_file"] = str(path)
            payloads[key + "_sha256"] = m.digest(path)
        calendar = root / "calendar.json"
        write(calendar, {"fixture_calendar": True})
        manifest = {"schema": m.INPUT_SCHEMA, "method_id": m.INPUT_METHOD,
            "fixed_protocol": deepcopy(m.PROTOCOL), "start": days[0], "end": days[0],
            "through": days[-1], "source_days": days, "scope_members": ["600001.SSE"],
            "calendar": str(calendar), "calendar_sha256": m.digest(calendar), **payloads, **m.durable.FLAGS}
        source = write(root / "manifest.json", manifest)
        event = write(root / "events/result.json", {"status": "research_action_events_projected", "execution_admitted": False})
        reference = write(root / "references/result.json", {"status": "research_reference_presence_diagnostic",
            "event_view": str(Path(event["path"]).parent), "execution_admitted": False})
        studies.append({"label": str(year), "inputs": source, "action_event_view": event, "action_reference_view": reference})
    return studies


def test_register_only_pins_three_saved_periods_and_changed_source_is_rejected(tmp_path, monkeypatch):
    studies = fixture(tmp_path)
    monkeypatch.setattr(m, "load_inputs", lambda *a: pytest.fail("Registration cannot load raw or panels"))
    monkeypatch.setattr(m, "ExactSourceRawFeed", lambda *a, **k: pytest.fail("Registration cannot build a raw feed"))
    monkeypatch.setattr(m.policy, "LeaderTypeRouter", lambda *a, **k: pytest.fail("Registration cannot construct a router"))
    out = tmp_path / "registration"
    record = m.register(studies, out)
    assert record["raw_or_panels_loaded"] is False and record["engine_advanced"] is False
    assert [study["label"] for study in record["studies"]] == ["2023", "2024", "2025"]
    assert record["spec"]["case_ids"] == list(m.CASE_IDS)
    assert m._registration(out / "registration.json") == record
    with Path(studies[0]["inputs"]["path"]).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="source or implementation changed"):
        m._registration(out / "registration.json")


def test_period_loads_once_four_new_cases_keep_failure_residual_and_no_fill_null(tmp_path, monkeypatch):
    studies = fixture(tmp_path)
    record = m.register(studies, tmp_path / "registration")
    study = record["studies"][0]
    manifest = m._read(study["inputs"]["path"])
    days = manifest["source_days"]
    context_day = (pd.Timestamp(days[0]) - pd.offsets.BDay()).strftime("%Y-%m-%d")
    raw = pd.DataFrame({"date": days, "code": ["sh.600001"] * len(days)})
    context = pd.DataFrame({"date": [context_day], "code": ["sh.600001"]})
    loaded = {"raw": raw, "panel_by_day": {}, "source_days": days,
        "calendar_payload": {}, "scope_members": ["600001.SSE"], "qualification": object(),
        "context_anchor": {"day": context_day}}
    calls = {"load": 0, "context": 0, "base": 0, "accounting": 0, "execution": 0, "router": []}

    def load(path):
        calls["load"] += 1
        return loaded, manifest

    def read_context(path):
        calls["context"] += 1
        assert str(path) == manifest["context_raw_file"]
        return context

    class Base:
        def __init__(self, combined, calendar, members, *, context_anchor):
            calls["base"] += 1
            assert len(combined) == 8 and calendar == [context_day] + days

    def accounting(*a, **k):
        calls["accounting"] += 1
        return SimpleNamespace(checks=[], provenance={"fixture": True})

    class Factory:
        def __init__(self, shared):
            self.shared = shared

        def __call__(self, base, source_days):
            calls["execution"] += 1
            return SimpleNamespace(provenance={"fixture": True}, accounting=deepcopy(self.shared), checks=[])

    class Router:
        def __init__(self, base, config, context, **options):
            self.guard, self.capacity, self.config = options["guard"], options["capacity"], config
            self.modes = []
            calls["router"].append(self)

        def step(self, state, day, number, *, tail_mode):
            assert state["cash"] == 10000000
            self.modes.append(tail_mode)
            result = deepcopy(state)
            result["day"] = day
            no_fill = self.guard == "unguarded" and self.capacity == "uncapped"
            fail = self.guard == "two_close_envguard" and self.capacity == "causal_first_seen_cap20"
            if fail and number == 1:
                state["cash"] = 1  # An uncommitted attempted state must not replace the saved account.
                raise ValueError("fixture_exact_held_action_unknown")
            if not no_fill:
                result["fills"] = {"one_callback": {"qty": 100}}
                result["orders"] = {"one_parent": {"qty": 100}}
                result["positions"] = {"600001.SSE": [{"qty": 100, "available": day}]} if number < 5 else {}
            return result, {"date": day}, {"day": day, "tail_mode": tail_mode}

    monkeypatch.setattr(m, "load_inputs", load)
    monkeypatch.setattr(m.pd, "read_parquet", read_context)
    monkeypatch.setattr(m, "_validate_panel_data", lambda *a: ({"600001.SSE"}, [context_day] + days, {}, "fixture_asof"))
    monkeypatch.setattr(m, "_rank_path", lambda *a: ({}, {}))
    monkeypatch.setattr(m, "_environment", lambda *a: {})
    monkeypatch.setattr(m, "load_action_accounting", accounting)
    monkeypatch.setattr(m, "HistoricalExecutionFactory", Factory)
    monkeypatch.setattr(m, "ExactSourceRawFeed", Base)
    monkeypatch.setattr(m.policy, "LeaderTypeRouter", Router)
    monkeypatch.setattr(m, "metrics", lambda daily, capital: {"net_return": -.01, "max_drawdown": -.02})
    out = tmp_path / "results"
    out.mkdir()
    results = m._period(study, {"path": "fixture_registration", "sha256": "fixture"}, out)
    assert {key: calls[key] for key in ("load", "context", "base", "accounting", "execution")} == {
        "load": 1, "context": 1, "base": 1, "accounting": 1, "execution": 4}
    assert [result["case_id"] for result in results] == list(m.CASE_IDS)
    assert results[0]["complete_PnL_available"] is True
    assert results[1]["complete_PnL_available"] is False and results[1]["metrics"] is None
    assert results[2]["engine_sessions_committed"] == 1 and results[2]["metrics"] is None
    assert results[2]["residual_positions"] == {"600001.SSE": 100}
    failed_account = m._read(out / "2023" / m.CASE_IDS[2] / "account.json")
    assert failed_account["cash"] == 10000000
    assert results[3]["complete_PnL_available"] is True
    for router in (calls["router"][0], calls["router"][1], calls["router"][3]):
        assert router.modes == ["NO_CHANGE"] * 5 + ["EXIT_ALL", "NO_CHANGE"]


def test_run_keeps_all_three_periods_separate_and_never_reuses_existing_output(tmp_path, monkeypatch):
    record = m.register(fixture(tmp_path), tmp_path / "registration")
    seen = []

    def period(study, ref, out):
        seen.append(study["label"])
        if study["label"] == "2024":
            raise ValueError("fixture_saved_source_failure")
        return [{"period": study["label"], "case_id": case_id, "complete_PnL_available": True,
            "engine_sessions_committed": 7, "metrics": {"net_return": -.01}} for case_id in m.CASE_IDS]

    monkeypatch.setattr(m, "_period", period)
    registration = tmp_path / "registration/registration.json"
    out = tmp_path / "new_run"
    result = m.run(registration, out)
    assert seen == ["2023", "2024", "2025"] and len(result["cases"]) == 12
    assert result["all_cases_complete_PnL"] is False and result["period_returns_concatenated"] is False
    assert all(row["metrics"] is None for row in result["cases"] if row["period"] == "2024")
    with pytest.raises(FileExistsError, match="exclusive"):
        m.run(registration, out)
