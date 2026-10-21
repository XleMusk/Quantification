"""Official-list boundaries with synthetic CSV/PDF bytes, never source acquisition."""
from copy import deepcopy

import pandas as pd
import pytest

from trading.research import leader_official_industry as official
from trading.research.leader_posthoc_industry import load as load_industry


A, B, C = "000001.SZSE", "000002.SZSE", "000003.SZSE"
RETRIEVED = "2026-10-17T05:00:00+08:00"
OBSERVED = "2026-10-19T00:00:00+08:00"


def row(symbol, code, name, division="26", title="化学原料和化学制品制造业"):
    return dict(zip(official.COLUMNS, (symbol, code, name, "C", "制造业", "", "", division, title, "1")))


def batches(tmp_path):
    values = []
    specs = [
        ("old_2024h1", "2024-06-30", "2024-09-20",
            [row(A, "000001", "旧甲"), row(B, "000002", "旧乙", "35", "专用设备制造业")]),
        ("new_2024year", "2024-12-31", "2025-03-25",
            [row(A, "000001", "新甲", "27", "医药制造业"), row(C, "000003", "未知分类", "", ""),
             row("", "900901", "未映射交易所代码")]),
    ]
    for identity, period, publication, rows in specs:
        folder = tmp_path / identity
        folder.mkdir()
        pdf, article, table = folder / "list.pdf", folder / "article.html", folder / "table.csv"
        pdf.write_bytes(b"%PDF-synthetic-reference-only\n" + identity.encode())
        article.write_text("<html>Synthetic official-list source</html>", encoding="utf-8")
        pd.DataFrame(rows, columns=official.COLUMNS).to_csv(table, index=False)
        values.append({"batch_id": identity, "reference_period_end": period,
            "reference_period_basis": "synthetic_declared_period",
            "publication_date": publication, "retrieved_at": RETRIEVED,
            "source_pdf": str(pdf), "source_article": str(article),
            "table_file": str(table), "rows": len(rows)})
    return values


def test_complete_official_batches_switch_after_publication_without_missing_issuer_backfill(tmp_path):
    source = batches(tmp_path)
    # PDF line wrapping may change the display label, but never the group key.
    from pathlib import Path
    first_table = Path(source[0]["table_file"])
    table = pd.read_csv(first_table, dtype=str, keep_default_na=False)
    table.loc[1, "division_code"] = "26"
    table.loc[1, "division_name"] = "化学原料和化学制品\n制造业"
    table.to_csv(first_table, index=False)
    original = deepcopy(source)
    root = tmp_path / "saved"
    saved = official.build(source, root, start="2025-01-01", end="2025-12-31")
    provider = load_industry(root, OBSERVED)
    january = provider("2025-01-02")
    assert january["reference_period_end"] == "2024-06-30"
    assert january["membership"] == {A: "C26", B: "C26"}
    assert set(january["industry_labels"]["C26"]) == {"化学原料和化学制品制造业", "化学原料和化学制品\n制造业"}
    # The newer list's reference period ending in December does not make it
    # usable in January, or on a publication date with no exact release clock.
    assert provider("2025-03-25")["membership"] == january["membership"]
    newest = provider("2025-03-26")
    assert newest["snapshot_date"] == "2025-03-26"
    assert newest["reference_period_end"] == "2024-12-31"
    assert newest["membership"] == {A: "C27", C: None}
    assert B not in newest["membership"]  # New complete list has no old issuer.
    assert not any(symbol.startswith("900901") for symbol in newest["membership"])
    assert newest["unmapped_code_count"] == 1
    assert newest["source_retrieved_at"] == RETRIEVED
    assert newest["actual_available_at"] == saved["created_at"]
    assert newest["publication_time_known"] is False
    assert newest["revision_safe_PIT_verified"] is False
    assert provider("2024-12-31") is None and provider("2026-01-02") is None
    newest["membership"][A] = "caller mutation"
    assert provider("2025-03-26")["membership"][A] == "C27"
    assert source == original


def test_official_source_hash_clock_and_invalid_identity_or_partial_label_are_rejected(tmp_path):
    source = batches(tmp_path)
    root = tmp_path / "saved"
    official.build(source, root, start="2025-01-01", end="2025-12-31")
    for key in ("source_pdf", "table_file"):
        from pathlib import Path
        path = Path(source[1][key])
        original = path.read_bytes()
        if key == "source_pdf":
            path.write_bytes(original + b"changed")
        else:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            frame.loc[0, "division_name"] = "changed valid division label"
            frame.to_csv(path, index=False)
        with pytest.raises(ValueError):
            official.load(root, OBSERVED)
        path.write_bytes(original)
    with pytest.raises(ValueError):
        official.load(root, "2026-10-16T00:00:00+08:00")
    with pytest.raises(ValueError):
        official.build([source[0], deepcopy(source[0])], tmp_path / "duplicate", start="2025-01-01", end="2025-12-31")
    table = source[1]["table_file"]
    original_frame = pd.read_csv(table, dtype=str, keep_default_na=False)
    invalid_code = original_frame.copy()
    invalid_code.loc[0, "code"] = "ABCDEF"
    invalid_code.to_csv(table, index=False)
    with pytest.raises(ValueError):
        official.build(source, tmp_path / "invalid_code", start="2025-01-01", end="2025-12-31")
    partial = original_frame.copy()
    partial.loc[0, "division_code"] = ""  # A known name alone is not a full classification.
    partial.to_csv(table, index=False)
    with pytest.raises(ValueError):
        official.build(source, tmp_path / "partial_label", start="2025-01-01", end="2025-12-31")
