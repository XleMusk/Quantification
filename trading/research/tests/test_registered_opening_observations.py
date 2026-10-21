"""The next display refresh must retain the latest saved failure evidence."""
from copy import deepcopy

from trading.research import registered_research_saved_opening as adapter, system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import write


def test_new_operation_preserves_prior_failed_observations_and_original_routes(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    write(previous / "receipt.json", {"saved": True})
    entry_failed = write(previous / "forward_result.json", {"status": "failed", "execution_progress_unknown": True})
    seat_failed = write(tmp_path / "seat_failed.json", {"status": "failed", "execution_progress_unknown": True})
    registration = write(tmp_path / "method.json", {"existing": True})
    head = {"entry_comparison_profile": {"registration": registration, "publications": [],
            "run_directory": str(tmp_path / "entry_account"), "operations": [str(previous / "receipt.json")]},
        "public_seat_profile": {branch: {"registration": registration, "publications": [],
            "run_directory": str(tmp_path / branch), "consumer_result": seat_failed if branch == "old_seat" else None}
            for branch in ("old_seat", "append_seat")}}
    config = {"opening_run_directory": str(tmp_path / "coordinator"), "run_directory": str(tmp_path / "system")}
    routes = [{"id": kind, "source": str(tmp_path / "sources" / kind)} for kind in adapter.ROUTE_KINDS]
    monkeypatch.setattr(daily.opening_daily, "_load", lambda directory: ({}, {"routes": routes}))
    monkeypatch.setattr(daily.opening_daily, "extend", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No existing route registration changes")))
    seen = {}
    monkeypatch.setattr(adapter, "build_source", lambda kind, *args, consumer_observation=None:
        seen.update({kind: consumer_observation}))
    monkeypatch.setattr(adapter, "build_opening", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No unchanged product rebuild")))
    original = deepcopy(head)
    assert daily._registered_opening(head, config, tmp_path / "new_operation") == []
    assert seen == {"entry_comparison": daily.Path(entry_failed["file"]),
        "public_seat_static": daily.Path(seat_failed["file"]), "public_seat_append": None}
    assert head == original
