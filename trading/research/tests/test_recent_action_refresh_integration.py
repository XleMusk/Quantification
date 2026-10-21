"""Three tiny cached-source/bundle/accounting contracts; no SDK, network or Engine."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading import engine
from trading.research import recent_action_bundle as bundle_builder
from trading.research import recent_action_sources as sources
from trading.research import recent_action_refresh as refresh
from trading.research.recent_action_extension import load_extension
from trading.research.tests.test_recent_action_extension import fixture_extension, pin, write, START, END
from trading.research.tests.test_stockdb_action_accounting import SYMBOL, DAYS


def prepared_cached_source(tmp_path, monkeypatch, *, unknown_ex=False, defer_to_refresh=False):
    """Reuse original full provider rows, clocks and PDF through the real preparer."""
    parts = fixture_extension(tmp_path / "inputs")
    event, reference, prior, value = parts
    query = value["queries"][0]
    payload = Path(query["payload"]["path"])
    if unknown_ex:
        frame = pd.read_csv(payload, dtype=str, keep_default_na=False)
        frame.loc[1, "dividOperateDate"] = ""
        frame.to_csv(payload, index=False)
        query["payload"] = pin(payload)
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    task = receipt["tasks"]["actions_2026"]
    task["sha256"] = query["payload"]["sha256"]
    task["source_sdk"] = {"distribution_version": "0.9.4", "sha256": sources.WHEEL_SHA256}
    query["receipt"] = write(receipt_path, receipt)
    write(prior, value)
    calendar = Path(value["calendar"]["path"])
    package = tmp_path / "inputs" / "complete_close"
    package.mkdir()
    publication = tmp_path / "inputs" / "scope" / "publication.json"
    publication.parent.mkdir()
    write(publication, {"source_day": END, "published_at": "2026-06-30T10:02:00+08:00",
        "calendar_ref": {"file": str(calendar), "sha256": pin(calendar)["sha256"]},
        "weights": {SYMBOL: .04}})
    monkeypatch.setattr(sources, "_now", lambda: datetime.fromisoformat("2026-06-30T11:00:00+08:00"))
    monkeypatch.setattr(sources, "_source", lambda *args: {"proof": {
        "day": END, "source_folder": str(package), "synthetic_exact_complete_close": True}})
    monkeypatch.setattr(sources, "load_publication", lambda path, **kwargs: json.loads(Path(path).read_text()))
    monkeypatch.setattr(sources, "select_sdk", lambda *args, **kwargs: pytest.fail("All queries are cached; no SDK selection"))
    monkeypatch.setattr(sources, "collect", lambda *args, **kwargs: pytest.fail("Cached source must not collect"))
    monkeypatch.setattr(engine, "advance", lambda *args, **kwargs: pytest.fail("Source and bundle work must not advance Engine"))
    old_bytes = {path: path.read_bytes() for path in (event / "result.json", reference / "result.json", prior,
        receipt_path, payload, Path(value["actions"][0]["source_file"]))}
    if defer_to_refresh:
        return parts, {"package": package, "publication": publication}, old_bytes
    prepared = sources.prepare(END, package, calendar, tmp_path / "prepared",
        publications=[publication], reuse_roots=[value["provider_root"]])
    collected = sources.collect_prepared(tmp_path / "prepared", None)
    assert prepared["pending_query_count"] == 0 and prepared["reused_query_count"] == 1
    assert collected["status"] == "scoped_action_sources_complete"
    assert collected["network_accessed"] is False and collected["new_query_count"] == 0
    return parts, prepared, old_bytes


def test_cached_source_builds_loadable_bundle_without_sdk_or_original_changes(tmp_path, monkeypatch):
    parts, scope, old_bytes = prepared_cached_source(tmp_path, monkeypatch, defer_to_refresh=True)
    event, reference, _, value = parts
    review = tmp_path / "inputs" / "review.json"
    write(review, {"action": value["actions"][0], "reference": value["reference_evidence"][0]})
    built = refresh.run(END, scope["package"], value["calendar"]["path"], tmp_path / "refresh",
        publications=[scope["publication"]], reuse_roots=[value["provider_root"]],
        base_event_view=event, base_reference_view=reference, reviews=[review])
    assert built["status"] == "ready", built
    assert built["preparation"]["reused_query_count"] == 1 and built["source"]["new_query_count"] == 0
    assert built["source"]["source_complete"] is True and built["source"]["network_accessed"] is False
    overlay = load_extension(event, reference, built["bundle"], symbols=[SYMBOL],
        raw_days=DAYS + [START, END], replay_days=DAYS[1:] + [START, END],
        price_source="independent_baostock_raw")
    assert len(overlay.actions) == 2 and overlay.actions[-1]["dividCashPsBeforeTax"] == "0.05"
    assert overlay.provenance["extension_scope_count"] == 1
    assert overlay.provenance["full_market_recent_coverage"] is False
    assert overlay.provenance["returns_reportable"] is False
    assert all(path.read_bytes() == before for path, before in old_bytes.items())


def test_unknown_new_provider_event_stays_pending_without_bundle_or_engine(tmp_path, monkeypatch):
    parts, prepared, old_bytes = prepared_cached_source(tmp_path, monkeypatch, unknown_ex=True)
    event, reference, _, value = parts
    output = tmp_path / "pending"
    built = bundle_builder.build(prepared["provider_root"], event, reference,
        value["calendar"]["path"], output, day=END)
    assert built["status"] == "pending_review" and built["bundle"] is None
    assert not (output / "bundle.json").exists()
    assert built["pending_review"][0]["reason"] == "relevant_provider_event_has_unknown_ex_date"
    assert built["engine_called"] is False and built["targets_generated"] is False
    assert built["pending_review"][0]["provider_row"]["dividOperateDate"] == ""
    assert all(path.read_bytes() == before for path, before in old_bytes.items())


def test_prior_review_and_primary_bytes_keep_their_actual_clocks_across_new_source(tmp_path, monkeypatch):
    parts, prepared, old_bytes = prepared_cached_source(tmp_path, monkeypatch)
    event, reference, prior, value = parts
    original_action = deepcopy(value["actions"][0])
    built = bundle_builder.build(prepared["provider_root"], event, reference,
        value["calendar"]["path"], tmp_path / "inherited", day=END, prior_bundle=prior)
    assert built["status"] == "ready", built
    saved = json.loads(Path(built["bundle"]).read_text())
    assert saved["actions"][0] == original_action
    assert saved["actions"][0]["reviewed_at"] == "2026-06-30T10:00:00+08:00"
    assert saved["actions"][0]["source_sha256"] == value["actions"][0]["source_sha256"]
    original_retrieved = json.loads(Path(value["queries"][0]["receipt"]["path"]).read_text())["tasks"]["actions_2026"]["retrieved_at"]
    new_retrieved = json.loads(Path(saved["queries"][0]["receipt"]["path"]).read_text())["tasks"]["actions_2026"]["retrieved_at"]
    assert new_retrieved == original_retrieved == "2026-06-30T09:00:00+08:00"
    assert saved["created_at"] != original_action["reviewed_at"]
    assert all(path.read_bytes() == before for path, before in old_bytes.items())
