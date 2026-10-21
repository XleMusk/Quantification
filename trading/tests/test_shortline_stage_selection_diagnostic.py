from trading.research.shortline_stage_selection_diagnostic import assemble


def test_stage_selection_diagnostic_keeps_forward_fields_out_of_selection():
    result = assemble(
        {"rows": [{"stage": 1, "events": 10, "hit_4_rate": .2, "hit_5_rate": .1}]},
        {"by_stage": [{"stage": 1, "next_open_clearance_rate": .8,
                        "next_high_touches_limit_up_rate": .4,
                        "hit_5_conditional_open_clearance_rate": .5}]},
        {"features": ["amount_rank_pct"], "results": [{"fold": "2026_ytd", "stage": 1,
            "test_events": 5, "test_hit5_rate": .1, "model_auc": .6,
            "model_average_precision": .2, "top10pct_events": 1,
            "top10pct_hit5_rate": .2, "baseline_brier": .1, "model_brier": .09}]},
    )
    assert result["stages"][0]["stage"] == 1
    assert result["screening_hypothesis"]["post_event_fields_used_for_selection"] is False
    assert result["candidate_generated"] is False
    assert result["execution_admitted"] is False
