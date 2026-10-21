import json
from pathlib import Path


SNAPSHOT = Path(
    r"D:\Quantification\outputs\trader_style_research\20261014_public_accounts\duan_profile_snapshot_20261014.json"
)


def test_xueqiu_owner_profile_is_identity_evidence_only():
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert snapshot["schema"] == "xueqiu_public_profile_snapshot_v1"
    assert snapshot["identity_status"] == "direct_public_profile_title_and_alias_consistent"
    assert snapshot["canonical_user_id"] == "1247347556"
    assert snapshot["display_name"] == "大道无形我有型"
    assert snapshot["execution_admitted"] is False
    assert "does not prove full holdings" in snapshot["style_boundary"]

    primary = next(
        row for row in snapshot["profiles"]
        if row["url"] == snapshot["canonical_profile_url"]
    )
    assert primary["display_name"] == snapshot["display_name"]
    assert primary["follower_count"] > 1_000_000
    assert primary["post_count"] > 10_000
    assert primary["portfolio_count"] == 0

