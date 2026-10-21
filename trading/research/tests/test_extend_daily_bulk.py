import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research.extend_daily_bulk import extend


def view(tmp_path, name, dates, *, kind="stock", code_prefix="sh"):
    root = tmp_path / name
    root.mkdir()
    rows = []
    for day_index, day in enumerate(dates):
        rows.append(pd.DataFrame({
            "date" if kind == "stock" else "dividOperateDate": [day],
            "code": [f"{code_prefix}.{day_index + 1:06d}"],
            "value": [str(day_index)],
        }))
    panel = "raw_daily.parquet" if kind == "stock" else "adjustment_events.parquet"
    with pq.ParquetWriter(root / panel, pa.Table.from_pandas(rows[0], preserve_index=False).schema) as writer:
        for frame in rows:
            writer.write_table(pa.Table.from_pandas(frame, preserve_index=False))
    (root / "result.json").write_text(json.dumps({
        "status": "source_cache_complete", "kind": kind, "expected_days": len(dates),
        "acquired_days": len(dates), "missing_days": [], "empty_days": [],
        "rows": len(dates), "symbols": len(dates), "execution_admitted": False,
    }), encoding="utf-8")
    (root / "plan.json").write_text(json.dumps({"days": dates, "kind": kind}), encoding="utf-8")
    return root


def test_extend_preserves_old_prefix_and_writes_receipts(tmp_path):
    base = view(tmp_path, "base", ["2026-10-08", "2026-10-09"])
    extra = view(tmp_path, "extra", ["2026-10-12", "2026-10-13"])
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]}).to_csv(calendar, index=False)

    folder, result = extend(base, extra, output=tmp_path / "out", calendar=calendar)

    assert result["old_prefix_preserved"] is True
    assert result["prefix_days"] == 2 and result["extension_days"] == 2
    assert result["execution_admitted"] is False
    assert result["promotion"] is False
    assert pd.read_parquet(folder / "raw_daily.parquet")["date"].tolist() == [
        "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"
    ]
    assert pq.ParquetFile(folder / "raw_daily.parquet").metadata.num_row_groups == 4
    assert json.loads((folder / "result.json").read_text(encoding="utf-8"))["cutoff"] == "2026-10-13"


def test_extend_rejects_overlap_and_schema_change(tmp_path):
    base = view(tmp_path, "base", ["2026-10-08", "2026-10-09"])
    overlap = view(tmp_path, "overlap", ["2026-10-09", "2026-10-12"])
    with pytest.raises(ValueError, match="new dates"):
        extend(base, overlap, output=tmp_path / "overlap_out")

    changed = view(tmp_path, "changed", ["2026-10-12"])
    with pq.ParquetWriter(changed / "raw_daily.parquet", pa.schema([
        ("date", pa.string()), ("code", pa.string()), ("other", pa.string())
    ])) as writer:
        writer.write_table(pa.Table.from_pydict({"date": ["2026-10-12"], "code": ["sh.000001"], "other": ["x"]}))
    with pytest.raises(ValueError, match="schemas differ"):
        extend(base, changed, output=tmp_path / "changed_out")
