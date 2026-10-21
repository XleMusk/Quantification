"""Narrow native32 source subset, real case-prefix replay and retained failures."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import shutil

import pandas as pd
import pytest

from trading.market_dataset import write_json
from trading.research import leader_eight_type_action_supplement as m
from trading.research import leader_eight_type_panel_replay as original
from trading.research.action_events import reference_concordance
from trading.research.action_reviewed_amendment import patch_reference_rows
from trading.research.leader_historical_execution_policy import HistoricalExecutionFactory
from trading.research.tests.test_leader_eight_type_case_replay import SavedInputs
from trading.research.tests.test_leader_eight_type_historical_native import A, B, accounting


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.source = SavedInputs(tmp_path, monkeypatch)
        self.days, self.ex = self.source.days, self.source.days[3]
        self.actions = pd.DataFrame([{
            "symbol": symbol, "code": "sz." + symbol[:6], "dividRegistDate": self.days[2],
            "dividOperateDate": self.ex, "dividPayDate": self.ex, "dividStockMarketDate": "",
            "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": ""}
            for symbol in (A, B)])
        self.gaps = pd.DataFrame([{
            "symbol": row.symbol, "code": row.code, "date": self.ex, "previous_day": self.days[2],
            "previous_observed_close": "10", "preclose": "9.9", "has_reported_distribution": True,
            "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": ""}
            for row in self.actions.itertuples()])
        self.old = self.pair("old", self.actions)
        known = self.actions.copy()
        known.loc[known.symbol.eq(A), "dividReserveToStockPs"] = "0"
        self.new = self.pair("new", known, self.old)
        self.original_pair = {"action_event_view": m._view_pin(self.old[0]),
                              "action_reference_view": m._view_pin(self.old[1])}

    def pair(self, name, actions, parents=None):
        event, ref = self.root / (name + "_event"), self.root / (name + "_ref")
        event.mkdir(); ref.mkdir()
        actions.to_parquet(event / "canonical_actions.parquet", index=False)
        terms = self.gaps.copy()
        for field in m.AMOUNTS[:3]:
            terms[field] = actions[field]
        rows = reference_concordance(terms, [], [])
        if parents:
            old = pd.read_parquet(parents[1] / "raw_reference_changes.parquet")
            rows = patch_reference_rows(old, old.symbol.eq(A), rows.loc[rows.symbol.eq(A)])
        rows.to_parquet(ref / "raw_reference_changes.parquet", index=False)
        for name in ("reviewed_special_references.json", "cash_reference_evidence.json", "distribution_reference_evidence.json"):
            write_json(ref / name, [])
        event_meta = {"status": "research_action_events_projected", "execution_admitted": False,
            "start": self.days[0], "end": self.days[-1], "canonical_events": 2,
            "files": {"canonical_actions.parquet": file_hash(event / "canonical_actions.parquet")}}
        if parents:
            event_meta["reviewed_cash_amendment"] = {"parent_event_view": str(parents[0]),
                "parent_reference_view": str(parents[1]), "events": [[A, self.ex]],
                "raw_prices_rescanned": False, "other_events_changed": False}
        write_json(event / "result.json", event_meta)
        (ref / "input_events.json").write_bytes((event / "result.json").read_bytes())
        ref_meta = {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
            "event_view": str(event), "files": {path.name:file_hash(path) for path in ref.iterdir()}}
        if parents:
            ref_meta["reviewed_cash_amendment"] = deepcopy(event_meta["reviewed_cash_amendment"])
        write_json(ref / "result.json", ref_meta)
        return event, ref

    def formula(self, name, parents, *, symbol=A, raw_change=False):
        event, before_ref = parents
        ref = self.root / name
        shutil.copytree(before_ref, ref)
        metadata = m._read(before_ref / "result.json")
        added = {"code":"sz."+symbol[:6], "dividOperateDate":self.ex,
            "dividCashPsBeforeTax":".1", "dividStocksPs":"0", "dividReserveToStockPs":"0",
            "reference_adjustment_cash_ps":".1"}
        write_json(ref / "distribution_reference_evidence.json", [added])
        rows = pd.read_parquet(before_ref / "raw_reference_changes.parquet")
        at = rows.symbol.eq(symbol)
        rows.loc[at, "reference_formula_status"] = "reference_match"
        rows.loc[at, "reference_formula_basis"] = "reviewed_cash_reference"
        rows.loc[at, "expected_reference_rmb"] = "9.9"
        rows.loc[at, "reference_error_rmb"] = 0.
        if raw_change:
            rows.loc[at, "preclose"] = "9.8"
        rows.to_parquet(ref / "raw_reference_changes.parquet", index=False)
        for filename in ("distribution_reference_evidence.json", "raw_reference_changes.parquet"):
            metadata["files"][filename] = file_hash(ref / filename)
        metadata["distribution_amendment"] = {"parent_reference_view":str(before_ref),
            "events":[[added["code"],self.ex]],"entitlements_changed":False,"raw_prices_rescanned":False}
        write_json(ref / "result.json", metadata)
        return event, ref

    def baseline(self, monkeypatch):
        source = self.source
        raw = source.loaded["raw"]
        after = raw.date.ge(self.ex)
        raw.loc[after, ["open","high","low","close","preclose"]] = ["9.9","10","9.8","9.9","9.9"]
        raw.loc[raw.date.eq(self.ex), "preclose"] = "9.9"
        for panel in source.panels.values():
            for index, item in enumerate(panel["types"]):
                item.update(status="matched" if index < 2 else "unknown",
                    candidates=[{"symbol": A if index == 0 else B,"amount_cny":100000000.}] if index < 2 else [],
                    unresolved_symbols=[] if index < 2 else [A,B])
        source.loaded["panel_by_day"] = source.panels
        raw.to_parquet(source.manifest["raw_file"], index=False)
        write_json(source.manifest["panels_file"], source.panels)
        source.manifest.update(IPO_qualification={"fixture_actual_clock":"2026-10-17T00:00:00+08:00"},
            raw_sha256=file_hash(source.manifest["raw_file"]),
            panels_sha256=file_hash(source.manifest["panels_file"]))
        write_json(source.path, source.manifest)
        source.loaded["provenance"] = {"manifest":str(source.path.resolve()),
            "manifest_sha256":file_hash(source.path),"input_record":deepcopy(source.manifest)}
        old_accounting = self.accounting(self.old)
        old_dir = self.root / "original"
        saved = original.replay_eight_types(**source.loaded, start=source.manifest["start"],
            end=source.manifest["end"], out=old_dir,
            execution_policy_factory=HistoricalExecutionFactory(old_accounting),
            context_raw=pd.read_parquet(source.manifest["context_raw_file"]))
        write_json(old_dir / "historical_method.json", {
            "schema":"leader_eight_type_historical_native_result_v1","method_id":m.INPUT_METHOD,
            "shared_engine_result":str(old_dir / "result.json"),"original_32_protocol_unchanged":True,
            "cases":32,"IPO_qualification":source.manifest["IPO_qualification"],
            "action_accounting":old_accounting.provenance})
        self.saved, self.result = saved, old_dir / "result.json"
        self.loads, self.execution_calls = [], []
        def load(path):
            self.loads.append(str(path))
            return source.load(path)
        def execute(record, loaded, manifest):
            self.execution_calls.append(True)
            assert record["action_event_view"]["directory"] == str(self.new[0])
            return pd.read_parquet(manifest["context_raw_file"]), HistoricalExecutionFactory(self.accounting(self.new))
        monkeypatch.setattr(m,"load_inputs",load)
        monkeypatch.setattr(m,"_execution_inputs",execute)
        return saved

    def accounting(self, pair):
        actions = pd.read_parquet(pair[0]/"canonical_actions.parquet").to_dict("records")
        overlay = accounting([A,B],[self.source.before]+self.days,self.days,actions)
        overlay.provenance.update(event_view=str(pair[0]),reference_view=str(pair[1]))
        return overlay

    def register(self):
        folder = self.root / "registration"
        record = m.register(self.source.path,self.result,*self.new,folder)
        return record, folder/"registration.json"


def test_terms_and_single_formula_subset_preserve_known_economics_and_exclude_other_blocks(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path,monkeypatch)
    # Original observed block identities are separate from return values.
    from trading.research.tests.test_leader_environment_action_supplement import source_fixture
    _, old = source_fixture()
    saved = {"source_days":fixture.days,"cases":[]}
    for index, case_id in enumerate(m.CASE_IDS):
        case = deepcopy(old["cases"][0])
        kind,guard,cap = case_id.split("__")
        case.update(case_id=case_id,type=kind,guard=guard,capacity=cap,
            daily=[{"date":fixture.days[0]}],engine_sessions_committed=1,
            account={"day":fixture.days[0]})
        symbol = A if index < 2 else B
        gap = fixture.gaps.loc[fixture.gaps.symbol.eq(symbol)].iloc[0].to_dict()
        gap.update(date=fixture.days[1],previous_day=fixture.days[0])
        case["blocking_evidence"] = {"day":fixture.days[1],
            "reason":"held_or_pending_raw_or_action_reference_unknown","session_not_committed":True,
            "raw_issues":[],"IPO_evidence":[],"reference_evidence":[{"symbol":symbol,
                "day":fixture.days[1],"status":"unresolved_blank_terms","reference_evidence":gap}]}
        saved["cases"].append(case)
    # Use the actual ex date for the narrowly saved original prefix.
    saved["source_days"] = [fixture.days[2],fixture.ex]
    for case in saved["cases"]:
        case["daily"][0]["date"] = fixture.days[2]
        case["account"]["day"] = fixture.days[2]
        block = case["blocking_evidence"]
        block["day"] = fixture.ex
        block["reference_evidence"][0].update(day=fixture.ex)
        block["reference_evidence"][0]["reference_evidence"].update(date=fixture.ex,previous_day=fixture.days[2])
    # A current capacity stop, a raw gap and an IPO gap are not source candidates.
    saved["cases"][3]["blocking_evidence"]["reason"] = "shared_engine_session_blocked"
    saved["cases"][4]["blocking_evidence"]["raw_issues"] = [{"reason":"missing_quote"}]
    saved["cases"][5]["blocking_evidence"]["IPO_evidence"] = [{"engine_block_reason":"unknown"}]
    final = fixture.formula("final_ref",fixture.new)
    manifest = {"context_anchor":{"day":fixture.source.before}}
    binding = m._source_binding(fixture.original_pair,saved,manifest,*final)
    assert binding["replayed_case_ids"] == list(m.CASE_IDS[:2])
    assert len(binding["reused_case_ids"]) == 30
    assert binding["formula_append"]["added_events"] == [["sz."+A[:6],fixture.ex]]
    assert all(case_id not in binding["source_blocked_case_ids"] for case_id in m.CASE_IDS[3:6])
    changed = fixture.actions.copy()
    changed.loc[changed.symbol.eq(A),["dividReserveToStockPs","dividCashPsBeforeTax"]] = ["0",".2"]
    bad = fixture.pair("known_change",changed,fixture.old)
    with pytest.raises(ValueError,match="Known economic"):
        m._source_binding(fixture.original_pair,saved,manifest,*bad)
    only_a = deepcopy(saved)
    for case in only_a["cases"][2:]:
        case["blocking_evidence"] = None
    outside = fixture.formula("unrelated_ref",fixture.new,symbol=B)
    with pytest.raises(ValueError,match="original blocked event set"):
        m._source_binding(fixture.original_pair,only_a,manifest,*outside)
    changed_raw = fixture.formula("raw_changed_ref",fixture.new,raw_change=True)
    with pytest.raises(ValueError,match="raw/non-formula"):
        m._source_binding(fixture.original_pair,saved,manifest,*changed_raw)


def test_real_case_replays_only_resolved_subset_and_resume_reuses_saved_accounts(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path,monkeypatch)
    saved = fixture.baseline(monkeypatch)
    before = deepcopy(saved)
    record, registration = fixture.register()
    selected = record["binding"]["replayed_case_ids"]
    assert selected == list(m.CASE_IDS[:4])
    assert len(record["binding"]["unresolved_source_case_ids"]) == 4
    def forbidden(*args,**kwargs):
        raise AssertionError("Supplement must use saved rankings and environment")
    monkeypatch.setattr(original,"_rank_path",forbidden)
    monkeypatch.setattr(original,"_environment",forbidden)
    calls = []
    def replay(*args,**kwargs):
        case_id = f"{args[12]}__{args[14]}__{args[13]}"
        calls.append(case_id)
        assert args[5] == saved["capacity_paths"][args[12]][args[13]]
        assert args[6] == saved["environment_decisions"]
        assert kwargs["snapshot_day"] == saved["cases"][m.CASE_IDS.index(case_id)]["daily"][-1]["date"]
        if case_id == selected[1] and calls.count(case_id) == 1:
            raise KeyboardInterrupt("explicit unfinished source case")
        return original._case(*args,**kwargs)
    monkeypatch.setattr(m,"_case",replay)
    folder = tmp_path/"supplement"
    with pytest.raises(KeyboardInterrupt):
        m.run(registration,folder)
    progress = m._read(folder/"progress.json")
    first = progress["completed_cases"][0]
    first_bytes = {pin["path"]:Path(pin["path"]).read_bytes()
                   for pin in [first["case_file"],*first["files"].values()]}
    with pytest.raises(ValueError,match="retry-active"):
        m.run(registration,folder,resume=True)
    result = m.run(registration,folder,resume=True,retry_active=True)
    assert result["replayed_case_count"] == 4 and result["reused_case_count"] == 28
    assert len(fixture.loads) == 2 and len(fixture.execution_calls) == 2
    assert calls.count(selected[0]) == 1 and calls.count(selected[1]) == 2
    assert set(calls) == set(selected)
    assert all(Path(path).read_bytes() == data for path,data in first_bytes.items())
    for old,case in zip(saved["cases"],result["cases"]):
        origin = case["source_origin"]
        if case["case_id"] in selected:
            assert origin["mode"] == "replayed"
            assert origin["committed_prefix_check"]["complete_account_snapshot_compared"] is True
            assert case["engine_sessions_committed"] == len(fixture.days)
            assert case["account"]["cash"] != 10000000 and case["fills"]
            assert origin["action_pair"]["action_event_view"] == str(fixture.new[0])
        else:
            assert {k:v for k,v in case.items() if k != "source_origin"} == old
            assert origin["mode"] == "reused" and origin["action_pair"]["action_event_view"] == str(fixture.old[0])
    assert saved == before
    assert m._read(fixture.result) == saved
    assert result["new_action_pair_scope"] == "replayed_case_ids_only"


def test_full_account_prefix_drift_is_preserved_as_failed_attempt_not_adopted(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path,monkeypatch)
    fixture.baseline(monkeypatch)
    _, registration = fixture.register()
    calls = []
    def drift(*args,**kwargs):
        calls.append(True)
        result = original._case(*args,**kwargs)
        result["committed_prefix_snapshot"]["cash"] += 1
        return result
    monkeypatch.setattr(m,"_case",drift)
    folder = tmp_path/"failed_supplement"
    with pytest.raises(ValueError,match="full account prefix changed"):
        m.run(registration,folder)
    attempt = folder/m.CASE_IDS[0]/"attempt-0001"
    assert m._read(attempt/"attempt.json")["status"] == "failed"
    assert m._read(attempt/"prefix_check.json")["status"] == "failed_original_prefix_changed"
    assert (attempt/"account.json").is_file() and (attempt/"committed_prefix_snapshot.json").is_file()
    assert m._read(folder/"progress.json")["completed_cases"] == []
    assert not (folder/"result.json").exists()
    with pytest.raises(ValueError,match="retry-active"):
        m.run(registration,folder,resume=True)
    assert calls == [True]
