import pandas as pd

from trading.research.shortline_split_experiment import summarize_touchability


def test_touchability_summary_is_post_replay_and_not_executable(tmp_path):
    proxy = tmp_path / "touchability.csv"
    pd.DataFrame([
        {"event_id": "a", "next_session_observed": True, "next_open_clearance": True,
         "next_high_touches_limit_up": True, "next_open_locked_limit_up": False,
         "touchability_label_status": "daily_ohlc_proxy_only"},
        {"event_id": "b", "next_session_observed": True, "next_open_clearance": False,
         "next_high_touches_limit_up": True, "next_open_locked_limit_up": True,
         "touchability_label_status": "daily_ohlc_proxy_only"},
        {"event_id": "ignored", "next_session_observed": True, "next_open_clearance": True,
         "next_high_touches_limit_up": True, "next_open_locked_limit_up": False,
         "touchability_label_status": "daily_ohlc_proxy_only"},
    ]).to_csv(proxy, index=False)

    summary = summarize_touchability(proxy, [{"event_id": "a"}, {"event_id": "b"}])

    assert summary["status"] == "completed_diagnostic"
    assert summary["matched_event_rows"] == 2
    assert summary["next_open_clearance"] == 1
    assert summary["next_open_clearance_rate_observed"] == 0.5
    assert summary["used_for_score_or_selection"] is False
    assert summary["executable_fill_label"] is False
