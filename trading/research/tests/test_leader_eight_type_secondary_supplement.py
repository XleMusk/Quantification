"""Two cached source completions and tiny real-ledger durable replay only."""
from copy import deepcopy
from pathlib import Path
import json

import pandas as pd
import pytest

from trading.market_dataset import write_json
from trading.research import leader_eight_type_panel_replay as original
from trading.research import leader_eight_type_secondary_supplement as m
from trading.research.action_events import amend_secondary
from trading.research.action_terms import CNINFO, HEADERS, SINA, cninfo_rows, match_terms, sina_rows
from trading.research.leader_historical_execution_policy import HistoricalExecutionFactory
from trading.research.tests.test_leader_eight_type_action_supplement import Fixture, file_hash
from trading.research.tests.test_leader_eight_type_historical_native import A, B, OBSERVED


class SecondaryFixture:
    def __init__(self, root, monkeypatch):
        self.root, self.basic = root, Fixture(root, monkeypatch)
        self.source, self.ex = self.basic.source, self.basic.ex
        self.days = self.source.days
        actions = self.basic.actions.copy()
        actions["dividCashPsBeforeTax"] = "0.1"
        actions["dividCashPsAfterTax"] = "0.09"
        actions["dividCashStock"] = "10派1元"
        actions["reserve_term_basis"] = ""
        self.old = self.basic.pair("secondary_parent", actions)
        metadata = m._read(self.old[0] / "result.json")
        metadata["limitations"] = ["Synthetic source; no market data"]
        write_json(self.old[0] / "result.json", metadata)
        self._sync_event(self.old)
        source = root / "cached_histories"
        source.mkdir()
        records = []
        for symbol in (A, B):
            code = symbol[:6]
            folder = source / symbol
            folder.mkdir()
            labels = "".join("<strong>" + HEADERS[i] + "</strong>" for i in (0,1,5,6,7,8,9,2,3,4))
            cells = "".join("<td>" + text + "</td>" for text in
                (self.days[0], "0", "0", "1", "实施", self.ex, self.days[2], "--", "查看"))
            secondary = (f"<title>fixture({code})分红配股</title><table id='sharebonus_1'>"
                f"<thead>{labels}</thead><tbody><tr>{cells}</tr></tbody></table>").encode("gb18030")
            primary = json.dumps({"resultcode":200,"count":1,"total":1,"records":[{
                "F006D":self.days[0],"F012N":1,"F018D":self.days[2],"F020D":self.ex,
                "F023D":self.ex,"F010N":None,"F011N":None,"F025D":None}]}).encode()
            (folder / "secondary.html").write_bytes(secondary)
            (folder / "primary.json").write_bytes(primary)
            action = actions.loc[actions.symbol.eq(symbol)].iloc[0].to_dict()
            candidate = match_terms(action, sina_rows(secondary,code), cninfo_rows(primary))
            records.append({"symbol":symbol,"status":"source_histories_compared","observed_at":OBSERVED,
                "secondary_url":SINA.format(code=code),"primary_url":CNINFO,"primary_params":{"scode":code},
                "secondary_file":f"{symbol}/secondary.html","secondary_sha256":file_hash(folder/"secondary.html"),
                "primary_file":f"{symbol}/primary.json","primary_sha256":file_hash(folder/"primary.json"),
                "candidates":[candidate],"pending":[]})
        write_json(source / "result.json", {"status":"completed_secondary_action_terms",
            "event_view":str(self.old[0]),"execution_admitted":False,"records":records})
        self.keys = ((A,self.ex),(B,self.ex))
        monkeypatch.setattr(m,"EVENTS",self.keys)
        event, reference, _, _ = amend_secondary(*self.old,source,self.keys,
            root/"secondary_event",root/"secondary_ref")
        self.new = (event,reference)
        self.parent_record = {"action_event_view":m._view_pin(self.old[0]),
            "action_reference_view":m._view_pin(self.old[1]),"inputs":m._pin(self.source.path)}

    @staticmethod
    def _sync_event(pair):
        event, reference = pair
        (reference/"input_events.json").write_bytes((event/"result.json").read_bytes())
        metadata = m._read(reference/"result.json")
        metadata["files"]["input_events.json"] = file_hash(reference/"input_events.json")
        write_json(reference/"result.json",metadata)

    @staticmethod
    def rehash(folder, filename):
        metadata = m._read(folder/"result.json")
        metadata["files"][filename] = file_hash(folder/filename)
        write_json(folder/"result.json",metadata)

    def parent(self, monkeypatch):
        """Only two original accounts run; the other thirty are retained fixtures."""
        source = self.source
        raw = source.loaded["raw"]
        raw.loc[raw.date.ge(self.ex),["open","high","low","close","preclose"]] = ["9.9","10","9.8","9.9","9.9"]
        source.manifest["end"] = self.days[2]
        raw.to_parquet(source.manifest["raw_file"],index=False)
        source.manifest["raw_sha256"] = file_hash(source.manifest["raw_file"])
        write_json(source.path,source.manifest)
        self.parent_record["inputs"] = m._pin(source.path)
        members,sessions,previous,now = original._validate_panel_data(source.panels,raw,
            self.days,source.loaded["calendar_payload"],source.loaded["scope_members"],source.loaded["qualification"])
        paths,ranks = original._rank_path(source.panels,members,source.manifest["start"],source.manifest["end"])
        environment = original._environment(source.panels,sessions,now)
        cases = []
        context = pd.read_parquet(source.manifest["context_raw_file"])
        for case_id in m.CASE_IDS[:2]:
            kind,guard,cap = case_id.split("__")
            case = original._case(raw,self.days,sessions,previous,source.panels,paths[kind][cap],
                environment,members,source.loaded["qualification"],source.anchor,
                source.manifest["start"],source.manifest["end"],kind,cap,guard,"fixture:"+case_id,
                execution_policy_factory=HistoricalExecutionFactory(self.basic.accounting(self.old)),context_raw=context)
            assert case["engine_sessions_committed"] == 3 and case["metrics"] is None
            assert case["account"]["pending_targets"]
            case["source_origin"] = {"mode":"reused","action_pair":{
                "action_event_view":str(self.old[0]),"action_reference_view":str(self.old[1])}}
            cases.append(case)
        for case_id in m.CASE_IDS[2:]:
            retained = deepcopy(cases[0])
            kind,guard,cap = case_id.split("__")
            retained.update(case_id=case_id,type=kind,guard=guard,capacity=cap,blocking_evidence=None)
            cases.append(retained)
        self.saved = {"schema":m.parent_method.METHOD+"_result","method_id":m.parent_method.METHOD,
            "start":source.manifest["start"],"end":source.manifest["end"],"source_days":self.days,
            "scope_members":sorted(members),"config":cases[0]["config"],"cases":cases,"case_count":32,
            "capacity_paths":paths,"raw_top5_paths":ranks,"environment_decisions":environment,**m.FLAGS}
        parent_dir = self.root/"parent"
        parent_dir.mkdir()
        write_json(parent_dir/"registration.json",{"fixture":True})
        write_json(parent_dir/"progress.json",{"status":"completed"})
        write_json(parent_dir/"result.json",self.saved)
        self.parent_path = parent_dir/"result.json"
        # Existing parent's full validation has separate coverage. Retain real
        # file pins here and isolate only this new source/runner contract.
        def parent(path):
            assert Path(path).resolve() == self.parent_path
            pins = {key:m._pin(parent_dir/(key+".json")) for key in ("result","progress","registration")}
            return deepcopy(self.parent_record),deepcopy(source.manifest),m._read(path),pins
        monkeypatch.setattr(m,"_parent",parent)
        self.loads = []
        def load(path):
            self.loads.append(str(path))
            return source.load(path)
        monkeypatch.setattr(m,"load_inputs",load)
        monkeypatch.setattr(m,"_execution_inputs",lambda record,loaded,manifest:(context,
            HistoricalExecutionFactory(self.basic.accounting(self.new))))
        folder = self.root/"registration"
        self.record = m.register(self.parent_path,*self.new,folder)
        self.registration = folder/"registration.json"
        return self.saved


def test_exact_cached_secondary_match_rejects_known_terms_and_frozen_raw_drift(tmp_path,monkeypatch):
    f = SecondaryFixture(tmp_path,monkeypatch)
    proof,actions,_ = m._checked_secondary_pair(f.parent_record,*f.new)
    assert set(map(tuple,proof["events"])) == set(f.keys)
    assert proof["issuer_confirmed"] is False and proof["PIT_verified"] is False
    assert actions.dividReserveToStockPs.tolist() == ["0","0"]
    canonical = f.new[0]/"canonical_actions.parquet"
    originals = {path:path.read_bytes() for path in (canonical,f.new[0]/"result.json",
        f.new[1]/"input_events.json",f.new[1]/"result.json")}
    changed = pd.read_parquet(canonical)
    changed.loc[0,"dividCashPsBeforeTax"] = "0.2"
    changed.to_parquet(canonical,index=False)
    f.rehash(f.new[0],canonical.name)
    f._sync_event(f.new)
    with pytest.raises(ValueError,match="exactly the two matched"):
        m._checked_secondary_pair(f.parent_record,*f.new)
    for path,data in originals.items():
        path.write_bytes(data)
    raw = f.new[1]/"raw_reference_changes.parquet"
    changed = pd.read_parquet(raw)
    changed.loc[0,"preclose"] = "9.8"
    changed.to_parquet(raw,index=False)
    f.rehash(f.new[1],raw.name)
    with pytest.raises(ValueError,match="Raw price facts"):
        m._checked_secondary_pair(f.parent_record,*f.new)


def test_binding_uses_current_own_reference_not_returns_and_leaves_unresolved(tmp_path,monkeypatch):
    f = SecondaryFixture(tmp_path,monkeypatch)
    saved = f.parent(monkeypatch)
    changed = deepcopy(saved)
    # One exact own-native reference fails independently despite completed terms.
    changed["cases"][1]["blocking_evidence"]["reference_evidence"][0]["reference_evidence"]["preclose"] = "9.7"
    other = deepcopy(changed["cases"][0])
    other.update(case_id=m.CASE_IDS[2],guard=m.CASE_IDS[2].split("__")[1])
    other["historical_execution_policy"]["accounting"]["event_view"] = str(f.basic.new[0])
    changed["cases"][2] = other
    before = deepcopy(changed)
    binding = m._binding(f.parent_record,changed,*f.new)
    assert binding["replayed_case_ids"] == [m.CASE_IDS[0]]
    assert binding["unresolved_source_case_ids"] == [m.CASE_IDS[1]]
    assert m.CASE_IDS[2] not in binding["eligible_source_case_ids"]
    assert len(binding["reused_case_ids"]) == 31 and changed == before
    with pytest.raises(ValueError,match="No current own-pair source block"):
        m._binding(f.parent_record,{**changed,"cases":changed["cases"][1:]},*f.new)


def test_real_prefix_and_durable_adoption_retry_never_replay_completed_or_other_thirty(tmp_path,monkeypatch):
    f = SecondaryFixture(tmp_path,monkeypatch)
    saved = f.parent(monkeypatch)
    selected = list(m.CASE_IDS[:2])
    assert f.record["binding"]["replayed_case_ids"] == selected
    calls = []
    def forbidden(*args,**kwargs):
        raise AssertionError("Saved ranks/environment/completed cases must not be recomputed")
    monkeypatch.setattr(original,"_rank_path",forbidden)
    monkeypatch.setattr(original,"_environment",forbidden)
    def replay(*args,**kwargs):
        case_id = f"{args[12]}__{args[14]}__{args[13]}"
        calls.append(case_id)
        if case_id == selected[1] and calls.count(case_id) == 1:
            raise KeyboardInterrupt("retain failed second attempt")
        return original._case(*args,**kwargs)
    monkeypatch.setattr(m,"_case",replay)
    out = tmp_path/"replay"
    replace,failures = Path.replace,[]
    def fail_after_first_completed(path,target):
        if Path(target).resolve() == out/"progress.json":
            candidate = m._read(path)
            if not failures and len(candidate["completed_cases"]) == 1 and candidate.get("active_case") is None:
                failures.append(True)
                raise OSError("durable completed marker then failed progress replace")
        return replace(path,target)
    monkeypatch.setattr(Path,"replace",fail_after_first_completed)
    with pytest.raises(OSError,match="failed progress replace"):
        m.run(f.registration,out)
    first_attempt = out/selected[0]/"attempt-0001"
    marker = m._read(first_attempt/"attempt.json")
    assert marker["status"] == "completed"
    first_bytes = {path:path.read_bytes() for path in first_attempt.iterdir() if path.is_file()}
    with pytest.raises(KeyboardInterrupt,match="failed second attempt"):
        m.run(f.registration,out,resume=True)
    progress = m._read(out/"progress.json")
    assert [row["case_id"] for row in progress["completed_cases"]] == selected[:1]
    before = list(calls)
    with pytest.raises(ValueError,match="retry-active"):
        m.run(f.registration,out,resume=True)
    assert calls == before
    with m._process_lock(m._writer_lock_path(out)):
        with pytest.raises(RuntimeError,match="writer busy"):
            m.run(f.registration,out,resume=True,retry_active=True)
    result = m.run(f.registration,out,resume=True,retry_active=True)
    assert result["replayed_case_count"] == 2 and result["reused_case_count"] == 30
    assert calls == [selected[0],selected[1],selected[1]]
    assert all(path.read_bytes() == data for path,data in first_bytes.items())
    for old,new in zip(saved["cases"],result["cases"]):
        assert new["source_origin"] == old["source_origin"]
        if new["case_id"] in selected:
            proof = new["source_supplement_origin"]["committed_prefix_check"]
            assert proof["complete_account_snapshot_compared"] and proof["pending_orders_included"]
            assert new["complete_PnL_available"] and new["fills"] and new["residual_positions"] == []
            bad = deepcopy(new)
            bad["committed_prefix_snapshot"]["cash"] += 1
            with pytest.raises(ValueError,match="full account prefix"):
                m._committed_prefix_check(old,bad,saved["source_days"])
        else:
            assert all(new[key] == old[key] for key in m.PAYLOADS)
            assert new["historical_execution_policy"] == old["historical_execution_policy"]
            assert new["metrics"] is None
    monkeypatch.setattr(m,"load_inputs",forbidden)
    monkeypatch.setattr(m,"_case",forbidden)
    assert m.run(f.registration,out,resume=True) == result
    pin_path = Path(f.record["parent"]["progress"]["path"])
    write_json(pin_path,{"status":"changed"})
    with pytest.raises(ValueError,match="changed"):
        m.run(f.registration,out,resume=True)
