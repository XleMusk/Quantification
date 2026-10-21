"""Durable fixed32 cases reuse real Engine accounts, never replay saved cash."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.market_dataset import write_json
from trading.research import leader_eight_type_case_replay as durable
from trading.research import leader_eight_type_historical_native as native
from trading.research import leader_eight_type_panel_replay as original
from trading.research.leader_historical_execution_policy import HistoricalExecutionFactory
from trading.research.tests.test_leader_eight_type_historical_native import (
    A, B, CALENDAR, OBSERVED, SESSIONS, accounting, bars, qualification,
)


class SavedInputs:
    """Only source loading is synthetic; _case, its factory and Ledger are real."""

    def __init__(self, root, monkeypatch, *, unavailable=False):
        self.root = root / "inputs"
        self.root.mkdir()
        at = SESSIONS.index("2026-01-12")
        self.days, self.before = SESSIONS[at:at + 10], SESSIONS[at - 1]
        raw = bars([self.before] + self.days)
        self.panels = {}
        for day in self.days:
            types = []
            for index, identity in enumerate(original.TYPE_IDS):
                unknown = unavailable and index % 2 == 0
                empty = unavailable and not unknown
                types.append({"id": identity,
                    "status": "unknown" if unknown else "not_matched" if empty else "matched",
                    "candidates": [] if empty else [
                        {"symbol": A, "amount_cny": 100000000.},
                        {"symbol": B, "amount_cny": 50000000.}],
                    "unresolved_symbols": [A, B] if unknown else []})
            self.panels[day] = {"day": day, "available_at": OBSERVED,
                "environment": {"source_complete": True, "breadth": .5, "median_return_pct": 1.},
                "types": types}
        paths = {name: self.root / filename for name, filename in (
            ("raw_file", "raw.parquet"), ("context_raw_file", "context.parquet"),
            ("panels_file", "panels.json"), ("calendar", "calendar.json"))}
        raw.loc[raw.date.isin(self.days)].to_parquet(paths["raw_file"], index=False)
        raw.loc[raw.date.eq(self.before)].to_parquet(paths["context_raw_file"], index=False)
        write_json(paths["panels_file"], self.panels)
        write_json(paths["calendar"], CALENDAR)
        self.anchor = {"day": self.before, "closes": {A: 10., B: 10.}}
        self.manifest = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID,
            "fixed_protocol": deepcopy(original.PROTOCOL), "created_at": OBSERVED,
            "start": self.days[0], "end": self.days[1], "through": self.days[-1],
            "source_days": self.days, "scope_members": [A, B], "context_anchor": self.anchor,
            **native.FLAGS}
        for key, path in paths.items():
            self.manifest[key] = str(path.resolve())
            self.manifest[{"raw_file": "raw_sha256", "context_raw_file": "context_raw_sha256",
                "panels_file": "panels_sha256", "calendar": "calendar_sha256"}[key]] = sha256(path.read_bytes()).hexdigest()
        self.path = self.root / "manifest.json"
        write_json(self.path, self.manifest)
        self.loaded = {"panel_by_day": self.panels,
            "raw": raw.loc[raw.date.isin(self.days)].copy(), "source_days": self.days,
            "calendar_payload": CALENDAR, "scope_members": [A, B],
            "qualification": qualification(), "context_anchor": self.anchor,
            "provenance": {"synthetic_saved_source": True}}
        self.events, self.references = root / "events", root / "references"
        self.events.mkdir()
        self.references.mkdir()
        write_json(self.events / "result.json", {
            "status": "research_action_events_projected", "execution_admitted": False})
        write_json(self.references / "result.json", {
            "status": "research_reference_presence_diagnostic", "execution_admitted": False,
            "event_view": str(self.events.resolve())})
        self.accounting_loads = []
        monkeypatch.setattr(durable, "load_inputs", self.load)
        monkeypatch.setattr(durable, "load_action_accounting", self.load_accounting)

    def load(self, path):
        assert Path(path).resolve() == self.path.resolve()
        return deepcopy(self.loaded), deepcopy(self.manifest)

    def load_accounting(self, event_view, reference_view, **kwargs):
        assert Path(event_view).resolve() == self.events.resolve()
        assert Path(reference_view).resolve() == self.references.resolve()
        assert kwargs == {"symbols": [A, B], "raw_days": [self.before] + self.days,
            "replay_days": self.days, "price_source": "independent_baostock_raw"}
        self.accounting_loads.append(deepcopy(kwargs))
        return accounting([A, B], [self.before] + self.days, self.days)

    def register(self, out, *, check_entry_readiness=False):
        durable.register(self.path, self.events, self.references, out,
            check_entry_readiness=check_entry_readiness)
        return out / "registration.json"

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))

    @classmethod
    def saved_case(cls, entry):
        case = cls.read(entry["case_file"]["path"])
        case.update({name: cls.read(pin["path"]) for name, pin in entry["files"].items()})
        return case


class CaseProbe:
    def __init__(self, *, interrupt=False):
        self.interrupt = interrupt
        self.calls = []
        self.results = {}
        self.forbidden = False

    def __call__(self, *args, **kwargs):
        if self.forbidden:
            raise AssertionError("A rejected resume or skipped case must not call _case")
        case_id = f"{args[12]}__{args[14]}__{args[13]}"
        self.calls.append(case_id)
        assert isinstance(kwargs["execution_policy_factory"], HistoricalExecutionFactory)
        if self.interrupt and case_id == durable.CASE_IDS[1]:
            self.interrupt = False
            raise KeyboardInterrupt("one unfinished second case")
        case = original._case(*args, **kwargs)
        self.results[case_id] = deepcopy(case)
        return case


def test_interrupted_second_case_requires_retry_and_reuses_exact_first_account(tmp_path, monkeypatch):
    source = SavedInputs(tmp_path, monkeypatch)
    registration = source.register(tmp_path / "registration")
    probe = CaseProbe(interrupt=True)
    monkeypatch.setattr(durable, "_case", probe)
    folder = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt, match="unfinished second case"):
        durable.run(registration, folder)

    progress = source.read(folder / "progress.json")
    assert [row["case_id"] for row in progress["completed_cases"]] == [durable.CASE_IDS[0]]
    assert progress["active_case"]["case_id"] == durable.CASE_IDS[1]
    completed = progress["completed_cases"][0]
    first = source.saved_case(completed)
    assert first == probe.results[durable.CASE_IDS[0]]
    assert first["orders"] and first["fills"] and first["complete_PnL_available"]
    assert first["engine_sessions_committed"] == len(source.days)
    assert first["account"]["cash"] != 10000000
    assert first["action_reference_checks"] and first["candidate_reference_checks"]
    assert not any(
        sum(lot["qty"] for lot in lots) for lots in first["account"]["positions"].values())
    assert all(fill["available_day"] == SESSIONS[SESSIONS.index(fill["day"]) + 1]
        for fill in first["fills"] if fill["side"] == "buy")
    assert set(completed["files"]) == {"decisions", "daily", "orders", "fills", "account"}
    assert not set(completed["files"]).intersection(source.read(completed["case_file"]["path"]))
    saved_bytes = {pin["path"]: Path(pin["path"]).read_bytes()
        for pin in [completed["case_file"], *completed["files"].values()]}
    failed_attempt = folder / durable.CASE_IDS[1] / "attempt-0001" / "attempt.json"
    failed_bytes = failed_attempt.read_bytes()
    assert source.read(failed_attempt)["status"] == "failed"
    prepared_bytes = (folder / "preparation.json").read_bytes()
    calls_before = list(probe.calls)
    with pytest.raises(ValueError, match="retry-active"):
        durable.run(registration, folder, resume=True)
    assert probe.calls == calls_before

    progress_bytes = (folder / "progress.json").read_bytes()
    with durable._process_lock(durable._writer_lock_path(folder)):
        with pytest.raises(RuntimeError, match="writer busy"):
            durable.run(registration, folder, resume=True, retry_active=True)
    assert probe.calls == calls_before
    assert (folder / "progress.json").read_bytes() == progress_bytes

    real_replace, injected_failures = Path.replace, []

    def fail_second_case_progress_replace(path, target):
        if Path(target).resolve() == (folder / "progress.json").resolve():
            candidate = source.read(path)
            if (not injected_failures and len(candidate["completed_cases"]) == 2
                    and candidate.get("active_case") is None):
                injected_failures.append(True)
                raise OSError("simulated progress replace failure")
        return real_replace(path, target)

    with monkeypatch.context() as replacement:
        replacement.setattr(Path, "replace", fail_second_case_progress_replace)
        with pytest.raises(OSError, match="simulated progress replace failure"):
            durable.run(registration, folder, resume=True, retry_active=True)
    assert injected_failures == [True]
    after_failed_replace = source.read(folder / "progress.json")
    assert [row["case_id"] for row in after_failed_replace["completed_cases"]] == [durable.CASE_IDS[0]]
    assert after_failed_replace["active_case"]["case_id"] == durable.CASE_IDS[1]
    adopted_attempt = folder / durable.CASE_IDS[1] / "attempt-0002" / "attempt.json"
    assert Path(after_failed_replace["active_case"]["attempt_directory"]) == adopted_attempt.parent
    adopted_bytes = adopted_attempt.read_bytes()
    assert source.read(adopted_attempt)["status"] == "completed"
    assert probe.calls == calls_before + [durable.CASE_IDS[1]]

    # A complete attempt beyond the last durable progress is adopted without
    # retry authorization or another Engine run of the already-finished case.
    resumed = durable.run(registration, folder, resume=True)
    assert probe.calls.count(durable.CASE_IDS[0]) == 1
    assert probe.calls.count(durable.CASE_IDS[1]) == 2
    assert probe.calls[2:] == list(durable.CASE_IDS[1:])
    assert all(Path(path).read_bytes() == content for path, content in saved_bytes.items())
    assert failed_attempt.read_bytes() == failed_bytes
    assert adopted_attempt.read_bytes() == adopted_bytes
    assert sorted(path.name for path in (folder / durable.CASE_IDS[0]).iterdir()) == ["attempt-0001"]
    assert (folder / "preparation.json").read_bytes() == prepared_bytes
    assert source.read(folder / "progress.json")["status"] == "completed"

    # The same registration fixes account IDs across independent output folders.
    monkeypatch.setattr(durable, "_case", original._case)
    uninterrupted = durable.run(registration, tmp_path / "uninterrupted")
    assert resumed["protocol"] == uninterrupted["protocol"] == original.PROTOCOL
    assert resumed["case_count"] == uninterrupted["case_count"] == 32
    assert [case["case_id"] for case in resumed["cases"]] == list(durable.CASE_IDS)
    # Compare all case contents, including cents, FIFO/T+1 lots, IDs, decisions,
    # daily events and accounting checks; outer preparation clocks may differ.
    assert resumed["cases"] == uninterrupted["cases"]


def test_resume_rejects_one_changed_source_or_saved_account_byte_before_any_case(tmp_path, monkeypatch):
    source = SavedInputs(tmp_path, monkeypatch)
    registration = source.register(tmp_path / "registration")
    probe = CaseProbe(interrupt=True)
    monkeypatch.setattr(durable, "_case", probe)
    folder = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        durable.run(registration, folder)
    completed = source.read(folder / "progress.json")["completed_cases"][0]
    assert source.saved_case(completed)["fills"]
    calls_before = list(probe.calls)
    probe.forbidden = True
    for path in (source.events / "result.json", Path(completed["files"]["account"]["path"])):
        saved = path.read_bytes()
        try:
            # Whitespace keeps the JSON value identical but changes one byte.
            path.write_bytes(saved + b" ")
            with pytest.raises(ValueError, match="changed"):
                durable.run(registration, folder, resume=True, retry_active=True)
            assert probe.calls == calls_before
        finally:
            path.write_bytes(saved)


def test_registered_readiness_skips_all_unknown_or_empty_types_without_case_or_account(tmp_path, monkeypatch):
    source = SavedInputs(tmp_path, monkeypatch, unavailable=True)
    registration = source.register(tmp_path / "registration", check_entry_readiness=True)
    probe = CaseProbe()
    probe.forbidden = True
    monkeypatch.setattr(durable, "_case", probe)
    folder = tmp_path / "skipped"
    result = durable.run(registration, folder)
    assert result["case_count"] == result["skipped_case_count"] == 32
    assert result["protocol"] == original.PROTOCOL
    assert [case["case_id"] for case in result["cases"]] == list(durable.CASE_IDS)
    assert probe.calls == [] and source.accounting_loads == []
    for case in result["cases"]:
        assert case["status"] == "skipped_no_possible_entry_from_saved_panel"
        assert case["metrics"] is None and case["account"] is None
        assert case["engine_sessions_committed"] == 0
        assert case["complete_PnL_available"] is False
        assert case["decisions"] == case["daily"] == case["orders"] == case["fills"] == []
    progress = source.read(folder / "progress.json")
    assert progress["status"] == "completed" and len(progress["completed_cases"]) == 32
    for entry in progress["completed_cases"]:
        assert source.read(entry["files"]["account"]["path"]) is None
        assert source.saved_case(entry) == result["cases"][durable.CASE_IDS.index(entry["case_id"])]
