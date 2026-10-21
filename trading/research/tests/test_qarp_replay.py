from copy import deepcopy

import pytest

from trading.research.profitability_inputs import QARP_ID, QARP_CASES, QARP_FORMULAS
from trading.research.qarp_replay import validate_resume
from trading.research import qarp_replay


def test_resume_binds_same_candidates_features_formula_and_configuration():
    record = {"research_family": QARP_ID, "status": "failed_qarp_diagnostics",
        "source_run": "source", "inputs_run": "inputs", "rankings": "frozen_ranks",
        "start": "2025-01-02", "end": "2026-09-30", "sessions": 424, "symbols": 5368,
        "scope": "research_only", "dataset": "prices", "results": [{"signal": case} for case in QARP_CASES],
        "protocol": {"formulas": QARP_FORMULAS, "signal_portfolio_configs": {case: {"holdings": 20} for case in QARP_CASES},
                     "financial_view": "financials", "financial_receipt": {"symbols": 3568}, "cohort_policy": "calendar"}}
    validate_resume(record, deepcopy(record))
    changed = deepcopy(record)
    changed["rankings"] = "new_ranks"
    with pytest.raises(ValueError, match="original scope"):
        validate_resume(record, changed)
    changed = deepcopy(record)
    changed["protocol"]["signal_portfolio_configs"][QARP_CASES[0]]["holdings"] = 10
    with pytest.raises(ValueError, match="input changed"):
        validate_resume(record, changed)


    changed = deepcopy(record)
    changed["protocol"]["formulas"]["qarp_only"] = "refitted_weights"
    with pytest.raises(ValueError, match="input changed"):
        validate_resume(record, changed)


def test_cli_control_refresh_requires_explicit_replay_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(qarp_replay, "run", lambda *args, **kw: (
        calls.append((args, kw)), {"status": "completed_qarp_diagnostics"}))
    assert qarp_replay.main(["--inputs-run", "inputs", "--control-run", "controls", "--refresh-controls"]) == 0
    assert calls[0][1]["refresh_controls"] is True
    with pytest.raises(SystemExit):
        qarp_replay.main(["--prepare-actions", "prior", "--primary-view", "primary", "--refresh-controls"])
