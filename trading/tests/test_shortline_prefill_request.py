import json

import pandas as pd

from trading.research.shortline_prefill_request import run


def test_shortline_prefill_request_stays_blank_and_non_admitting(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "status": "completed_research_target_weight_preview",
        "execution_admitted": False,
        "decision_day": "2026-10-13",
        "weights": {"000668.SZSE": 0.2, "003037.SZSE": 0.2},
    }), encoding="utf-8")

    out, result = run(target, output=tmp_path / "out")

    assert result["account_observed"] is False
    assert result["quotes_observed"] is False
    assert result["submitted"] is False
    prefill = pd.read_csv(result["ths_prefill_reference_template"])
    assert prefill["price_field"].isna().all()
    assert json.loads(out.read_text(encoding="utf-8"))["execution_admitted"] is False
