import json

import pandas as pd

from trading.research import stockdb_research_prefill_request as module


def test_prefill_request_is_blank_and_non_admitting(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target_csv = target_dir / "weights.csv"
    pd.DataFrame([{"rank": 1, "symbol": "000001.SZSE", "score": 1.0, "target_weight": 1.0}]).to_csv(target_csv, index=False)
    target = target_dir / "result.json"
    target.write_text(json.dumps({
        "status": "completed_stockdb_research_target_preview", "execution_admitted": False,
        "date": "2026-10-13", "target_path": str(target_csv),
    }), encoding="utf-8")
    out, result = module.run(target, output=tmp_path / "out")
    assert result["account_observed"] is False
    assert result["quotes_observed"] is False
    prefill = pd.read_csv(result["ths_prefill_reference_template"])
    assert prefill.loc[0, "price_field"] != prefill.loc[0, "price_field"]  # blank NaN
    assert json.loads(out.read_text(encoding="utf-8"))["submitted"] is False

