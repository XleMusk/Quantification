"""Small saved-artifact adoption and fixed producer wiring; synthetic only."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from trading.research import opening_daily_artifacts as factory
from trading.research import next_session_preview
from trading.research.tests.test_saved_intent_bridge import bundle, D, E, SIGNAL, write


@pytest.fixture
def fixed(bundle):
    b = bundle
    root = b["root"]
    ref = factory._reference
    source = {"run": ref(b["paths"]["run"]), "account": ref(b["paths"]["account"]),
        "protocol": ref(b["paths"]["protocol"]), "replay": ref(b["paths"]["receipt"])}
    head = {"schema": "stockdb_fixed_persistent_daily_research_v1", "source": source,
        "last_completed_day": D, **factory.FLAGS}
    write(root/"head.json", head)
    preview = root/"preview.json"
    pack = root/"pack.json"
    intent = root/"intent.json"
    write(intent, b["payload"])
    write(preview, {"status": "paper_preview_only", "task_method": "saved_fixed_account_next_session_v1",
        "execution_admitted": False, "strategy_id": SIGNAL, "source_cutoff": D,
        "source_run": str(b["paths"]["run"]), "execution_day": E})
    write(pack, {"schema": "a_share_opening_pack_v1", "status": "paper_candidates_only",
        "execution_admitted": False, "automatic_strategy_selection": False,
        "source_cutoff": D, "execution_day": E, "candidate_order_count": 0,
        "strategies": [{"strategy_id": SIGNAL, "preview": str(preview)}]})
    route = {"id": "fixed", "family": "fixed", "source": str(root), "signal": SIGNAL}
    artifacts = {"preview": str(preview), "opening_pack": str(pack), "intent": str(intent)}
    return b, route, ref(root/"head.json"), artifacts


def test_adoption_binds_existing_fixed_artifacts_without_producing_or_changing_them(fixed, monkeypatch):
    b, route, source, artifacts = fixed
    def forbidden(*a, **kw):
        raise AssertionError("Adoption attempted a producer")
    monkeypatch.setattr(next_session_preview, "build", forbidden)
    before = {path: Path(path).read_bytes() for path in artifacts.values()}
    product = factory.adopt(route, source, artifacts)
    assert product["source_head"] == source and product["source_day"] == D and product["execution_day"] == E
    assert product["candidate_order_count"] == 0 and product["case_count"] == 1
    assert product["prefill_allowed"] is False and product["engine_advanced"] is False
    factory.validate_product(route, source, product)
    assert before == {path: Path(path).read_bytes() for path in artifacts.values()}


@pytest.mark.parametrize("field", ["execution_day", "source_run_ref"])
def test_adoption_rejects_a_different_date_or_saved_source(fixed, field):
    b, route, source, artifacts = fixed
    if field == "execution_day":
        pack = json.loads(Path(artifacts["opening_pack"]).read_text(encoding="utf-8"))
        pack["execution_day"] = "2026-10-20"
        write(Path(artifacts["opening_pack"]), pack)
    else:
        payload = deepcopy(b["payload"])
        payload["refs"]["source_run"]["sha256"] = "changed"
        write(Path(artifacts["intent"]), payload)
    with pytest.raises(ValueError, match="different|does not match"):
        factory.adopt(route, source, artifacts)


def test_fixed_producer_passes_saved_intent_to_existing_preview_instead_of_reranking(fixed, monkeypatch):
    from trading.research import saved_account_intent, opening_pack
    b, route, source, artifacts = fixed
    calls = []
    def export(run, signal, execution):
        calls.append("export")
        assert signal == SIGNAL and execution == E
        return b["payload"]
    def preview(run, signal, day, execution, *, saved_intent):
        calls.append("preview")
        assert saved_intent == b["payload"] and day == D and execution == E
        # Existing producer contract is result.json inside its returned folder.
        directory = b["root"]/"produced_preview"
        directory.mkdir()
        value = json.loads(Path(artifacts["preview"]).read_text(encoding="utf-8"))
        write(directory/"result.json", value)
        return directory, value
    def opening(previews, *, execution_day, strategy_id):
        calls.append("opening")
        assert execution_day == E and strategy_id == SIGNAL
        directory = b["root"]/"produced_pack"
        directory.mkdir()
        value = json.loads(Path(artifacts["opening_pack"]).read_text(encoding="utf-8"))
        value["strategies"][0]["preview"] = str(previews[0])
        write(directory/"opening_pack.json", value)
        return directory, value
    monkeypatch.setattr(saved_account_intent, "export_request", export)
    monkeypatch.setattr(next_session_preview, "build", preview)
    monkeypatch.setattr(opening_pack, "build", opening)
    product = factory.produce(route, source, b["root"]/"new_output")
    assert calls == ["export", "preview", "opening"]
    assert product["source_head"] == source and product["execution_admitted"] is False
    assert Path(product["refs"]["intent"]["path"]).is_file()


def test_preview_rejects_a_mismatched_saved_intent_before_loading_market_sources(bundle):
    with pytest.raises(ValueError, match="differs from the requested fixed preview"):
        next_session_preview.build(bundle["paths"]["run"], SIGNAL, "2026-10-15", E,
            saved_intent=bundle["payload"])
