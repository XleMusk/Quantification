"""Launcher for the A-share committee learning system."""

import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["persistent-task-decision-receipt"]:
        from trading.research.persistent_task_decision_receipt import main as persistent_task_decision_main
        return persistent_task_decision_main(argv[1:])
    if argv[:1] == ["runtime-recipe"]:
        from trading.research.runtime_recipe import main as runtime_recipe_main
        return runtime_recipe_main(argv[1:])
    if argv[:1] == ["monthly-max-vs-sigma-supplement"]:
        from trading.research.monthly_max_vs_sigma_supplement import main as monthly_max_sigma_supplement_main
        return monthly_max_sigma_supplement_main(argv[1:])
    if argv[:1] == ["monthly-max-vs-sigma"]:
        from trading.research.monthly_max_vs_sigma import main as monthly_max_sigma_main
        return monthly_max_sigma_main(argv[1:])
    if argv[:1] == ["leader-eight-type-secondary-supplement"]:
        from trading.research.leader_eight_type_secondary_supplement import main as secondary_supplement_main
        return secondary_supplement_main(argv[1:])
    if argv[:1] == ["leader-official-industry"]:
        from trading.research.leader_official_industry import main as official_industry_main
        return official_industry_main(argv[1:])
    if argv[:1] == ["leader-industry-composite"]:
        from trading.research.leader_industry_composite import main as industry_composite_main
        return industry_composite_main(argv[1:])
    if argv[:1] == ["leader-historical-listing-classification"]:
        from trading.research.leader_historical_listing_classification import main as leader_listing_classification_main
        return leader_listing_classification_main(argv[1:])
    if argv[:1] == ["leader-eight-type-historical-native"]:
        from trading.research.leader_eight_type_historical_native import main as historical_native_eight_main
        return historical_native_eight_main(argv[1:])
    if argv[:1] == ["leader-eight-type-case-replay"]:
        from trading.research.leader_eight_type_case_replay import main as durable_eight_main
        return durable_eight_main(argv[1:])
    if argv[:1] == ["leader-eight-type-action-supplement"]:
        from trading.research.leader_eight_type_action_supplement import main as eight_action_supplement_main
        return eight_action_supplement_main(argv[1:])
    if argv[:1] == ["leader-panel-readiness"]:
        from trading.research.leader_panel_readiness import main as leader_panel_readiness_main
        return leader_panel_readiness_main(argv[1:])
    if argv[:1] == ["leader-environment-exit-ablation"]:
        from trading.research.leader_environment_exit_ablation import main as environment_exit_ablation_main
        return environment_exit_ablation_main(argv[1:])
    if argv[:1] == ["leader-environment-attribution"]:
        from trading.research.leader_environment_attribution import main as environment_attribution_main
        return environment_attribution_main(argv[1:])
    if argv[:1] == ["leader-trend-eligible-scope"]:
        from trading.research.leader_trend_eligible_scope import main as trend_eligible_scope_main
        return trend_eligible_scope_main(argv[1:])
    if argv[:1] == ["leader-historical-month-summary"]:
        from trading.research.leader_historical_month_summary import main as leader_month_summary_main
        return leader_month_summary_main(argv[1:])
    if argv[:1] == ["leader-eight-type-history-view"]:
        from trading.research.leader_eight_type_history_view import main as leader_history_view_main
        return leader_history_view_main(argv[1:])
    if argv[:1] == ["recent-action-refresh"]:
        from trading.research.recent_action_refresh import main as recent_action_refresh_main
        return recent_action_refresh_main(argv[1:])
    if argv[:1] == ["native-lowvol-replay-summary"]:
        from trading.research.native_lowvol_replay_summary import main as native_lowvol_summary_main
        return native_lowvol_summary_main(argv[1:])
    if argv[:1] == ["ths-opening-review"]:
        from trading.ths.opening_review_cli import main as ths_opening_review_main
        return ths_opening_review_main(argv[1:])
    if argv[:1] == ["system-daily-refresh"]:
        from trading.research.system_daily_refresh import main as system_daily_refresh_main
        return system_daily_refresh_main(argv[1:])
    if argv[:1] == ["native-lowvol-forward"]:
        from trading.research.native_lowvol_forward import main as native_lowvol_forward_main
        return native_lowvol_forward_main(argv[1:])
    if argv[:1] == ["native-lowvol-publish"]:
        from trading.research.native_lowvol_publication import main as native_lowvol_publish_main
        return native_lowvol_publish_main(argv[1:])
    if argv[:1] == ["leader-historical-identity"]:
        from trading.research.leader_historical_identity_inputs import main as historical_identity_main
        return historical_identity_main(argv[1:])
    if argv[:1] == ["leader-known-identity-scope"]:
        from trading.research.leader_known_identity_scope import main as known_identity_scope_main
        return known_identity_scope_main(argv[1:])
    if argv[:1] == ["leader-known-identity-continue"]:
        from trading.research.leader_known_identity_continue import main as known_identity_continue_main
        return known_identity_continue_main(argv[1:])
    if argv[:1] == ["baostock-native-history"]:
        from trading.research.baostock_native_history import main as baostock_native_history_main
        return baostock_native_history_main(argv[1:])
    if argv[:1] == ["leader-action-component-diagnostic"]:
        from trading.research.leader_action_component_diagnostic import main as leader_action_diagnostic_main
        return leader_action_diagnostic_main(argv[1:])
    if argv[:1] == ["leader-case-study"]:
        from trading.research.leader_case_study import main as leader_case_study_main
        return leader_case_study_main(argv[1:])
    if argv[:1] == ["leader-type-case-study"]:
        from trading.research.leader_type_case_study import main as leader_type_case_study_main
        return leader_type_case_study_main(argv[1:])
    if argv[:1] == ["leader-type-case-context"]:
        from trading.research.leader_type_case_context import main as leader_type_case_context_main
        return leader_type_case_context_main(argv[1:])
    if argv[:1] == ["leader-historical-environment"]:
        from trading.research.leader_historical_environment import main as leader_historical_environment_main
        return leader_historical_environment_main(argv[1:])
    if argv[:1] == ["leader-historical-case-annotations"]:
        from trading.research.leader_historical_case_annotations import main as leader_historical_case_annotations_main
        return leader_historical_case_annotations_main(argv[1:])
    if argv[:1] == ["leader-monthly-engine-cases"]:
        from trading.research.leader_monthly_engine_cases import main as leader_monthly_engine_cases_main
        return leader_monthly_engine_cases_main(argv[1:])
    if argv[:1] == ["leader-case-structure-observations"]:
        from trading.research.leader_case_structure_observations import main as leader_case_structure_main
        return leader_case_structure_main(argv[1:])
    if argv[:1] == ["leader-expiry-hook-diagnostic"]:
        from trading.research.leader_expiry_hook_diagnostic import main as leader_expiry_hook_main
        return leader_expiry_hook_main(argv[1:])
    if argv[:1] == ["leader-reported-turnover-confirmation"]:
        from trading.research.leader_reported_turnover_confirmation import main as leader_turnover_confirmation_main
        return leader_turnover_confirmation_main(argv[1:])
    if argv[:1] == ["leader-entry-comparison"]:
        from trading.research.leader_entry_comparison_publication import main as leader_entry_comparison_main
        return leader_entry_comparison_main(argv[1:])
    if argv[:1] == ["leader-entry-comparison-forward"]:
        from trading.research.leader_entry_comparison_forward import main as leader_entry_forward_main
        return leader_entry_forward_main(argv[1:])
    if argv[:1] == ["leader-entry-confirmation-inputs"]:
        from trading.research.leader_entry_confirmation_inputs import main as leader_entry_inputs_main
        return leader_entry_inputs_main(argv[1:])
    if argv[:1] == ["seat-case-study"]:
        from trading.research.seat_case_study import main as seat_case_study_main
        return seat_case_study_main(argv[1:])
    if argv[:1] == ["trade-casebook"]:
        from trading.research.trade_casebook import main as trade_casebook_main
        return trade_casebook_main(argv[1:])
    if argv[:1] == ["trade-case-learning"]:
        from trading.research.trade_case_learning_index import main as trade_case_learning_main
        return trade_case_learning_main(argv[1:])
    if argv[:1] == ["seat-disclosure-catalog"]:
        from trading.research.seat_disclosure_catalog import main as seat_catalog_main
        return seat_catalog_main(argv[1:])
    if argv[:1] == ["seat-append-register"]:
        from trading.research.seat_append_registration import main as seat_append_register_main
        return seat_append_register_main(argv[1:])
    if argv[:1] == ["seat-append-source-audit"]:
        from trading.research.seat_append_source_audit import main as seat_append_audit_main
        return seat_append_audit_main(argv[1:])
    if argv[:1] == ["seat-daily"]:
        from trading.research.seat_daily import main as seat_daily_main
        return seat_daily_main(argv[1:])
    if argv[:1] == ["seat-append-dated-inputs"]:
        from trading.research.seat_append_dated_inputs import main as seat_append_inputs_main
        return seat_append_inputs_main(argv[1:])
    if argv[:1] == ["seat-append-forward"]:
        from trading.research.seat_append_forward import main as seat_append_forward_main
        return seat_append_forward_main(argv[1:])
    if argv[:1] == ["seat-hypothesis-dated-inputs"]:
        from trading.research.seat_hypothesis_dated_inputs import main as seat_dated_main
        return seat_dated_main(["dated-inputs", *argv[1:]])
    if argv[:1] == ["seat-hypothesis-source-audit"]:
        from trading.research.seat_hypothesis_dated_inputs import main as seat_dated_main
        return seat_dated_main(["exact-source-audit", *argv[1:]])
    if argv[:1] == ["seat-hypothesis-dated-forward"]:
        from trading.research.seat_hypothesis_dated_forward import main as seat_forward_main
        return seat_forward_main(argv[1:])
    if argv[:1] == ["seat-hypothesis-forward-register"]:
        from trading.research.seat_hypothesis_forward_registration import main as seat_forward_register_main
        return seat_forward_register_main(argv[1:])
    if argv[:1] == ["shortline-native-bootstrap-state"]:
        from trading.research.shortline_native_incremental import main as native_incremental_main
        return native_incremental_main(["bootstrap", *argv[1:]])
    if argv[:1] == ["shortline-native-daily-input"]:
        from trading.research.shortline_native_incremental import main as native_incremental_main
        return native_incremental_main(["advance", *argv[1:]])
    if argv[:1] == ["shortline-native-history-repair"]:
        from trading.research.shortline_native_history_repair import main as native_history_repair_main
        return native_history_repair_main(argv[1:])
    if argv[:1] == ["lowvol-supplement-append"]:
        from trading.research.lowvol_supplement_append import main as lowvol_append_main
        return lowvol_append_main(argv[1:])
    if argv[:1] == ["leader-cohort-observe"]:
        from trading.research.leader_cohort_observation import main as leader_cohort_observe_main
        return leader_cohort_observe_main(argv[1:])
    if argv[:1] == ["market-roster-observe"]:
        from trading.research.prior_dated_market_quote_snapshot import main as market_roster_observe_main
        return market_roster_observe_main(argv[1:])
    if argv[:1] == ["market-roster-watch"]:
        from trading.research.prior_dated_market_quote_snapshot import watch_main as market_roster_watch_main
        return market_roster_watch_main(argv[1:])
    if argv[:1] == ["leader-intraday-panel"]:
        from trading.research.leader_intraday_path_panel import main as leader_intraday_panel_main
        return leader_intraday_panel_main(argv[1:])
    if argv[:1] == ["index-observe"]:
        from trading.research.index_observation import main as index_observe_main
        return index_observe_main(argv[1:])
    if argv[:1] == ["architecture-check"]:
        from trading.architecture import main as architecture_main
        return architecture_main(argv[1:])
    if argv[:1] == ["production-receipt-audit"]:
        from trading.research.production_receipt_audit import main as production_receipt_audit_main
        return production_receipt_audit_main(argv[1:])
    if argv[:1] == ["ths"]:
        from trading.ths.cli import main as ths_main
        return ths_main(argv[1:])
    if argv[:1] == ["ths-visual-operator-card"]:
        from trading.ths.visual_operator import main as visual_operator_main
        return visual_operator_main(argv[1:])
    if argv[:1] == ["ths-simulation-funds-preview"]:
        from trading.ths.sky_readonly_funds import main as simulation_funds_main
        return simulation_funds_main(argv[1:])
    if argv[:1] == ["galaxy-api-readonly-probe"]:
        from trading.research.galaxy_api_probe import main as galaxy_api_probe_main
        return galaxy_api_probe_main(argv[1:])
    if argv[:1] == ["opening-cycle"]:
        from trading.research.opening_cycle import main as opening_cycle_main
        return opening_cycle_main(argv[1:])
    if argv[:1] == ["stockdb-rolling-low-volatility"]:
        from trading.research.stockdb_rolling_low_volatility import main as rolling_low_volatility_main
        return rolling_low_volatility_main(argv[1:])
    if argv[:1] == ["stockdb-fixed-daily"]:
        from trading.research.stockdb_fixed_daily import main as fixed_daily_main
        return fixed_daily_main(argv[1:])
    if argv[:1] == ["stockdb-close-package"]:
        from trading.research.stockdb_close_package import main as stockdb_close_package_main
        return stockdb_close_package_main(argv[1:])
    if argv[:1] == ["continuous-paper-acceptance"]:
        from trading.research.continuous_paper_acceptance import main as continuous_paper_acceptance_main
        return continuous_paper_acceptance_main(argv[1:])
    if argv[:1] == ["paper-account-recovery"]:
        from trading.research.paper_account_recovery import main as paper_account_recovery_main
        return paper_account_recovery_main(argv[1:])
    if argv[:1] == ["paper-target-reconciliation"]:
        from trading.research.paper_target_reconciliation import main as paper_target_reconciliation_main
        return paper_target_reconciliation_main(argv[1:])
    if argv[:1] == ["paper-target-native-reference"]:
        from trading.research.paper_target_reconciliation import native_reference_main
        return native_reference_main(argv[1:])
    if argv[:1] == ["paper-plan-reconciliation-audit"]:
        from trading.research.paper_plan_reconciliation_audit import main as paper_plan_reconciliation_audit_main
        return paper_plan_reconciliation_audit_main(argv[1:])
    if argv[:1] == ["paper-exposure-stress"]:
        from trading.research.paper_exposure_stress import main as paper_exposure_stress_main
        return paper_exposure_stress_main(argv[1:])
    if argv[:1] == ["ledger-recovery-scenarios"]:
        from trading.research.ledger_recovery_scenarios import main as ledger_recovery_scenarios_main
        return ledger_recovery_scenarios_main(argv[1:])
    if argv[:1] == ["snapshot-publication-recovery"]:
        from trading.research.snapshot_publication_recovery import main as snapshot_publication_recovery_main
        return snapshot_publication_recovery_main(argv[1:])
    if argv[:1] == ["joint-portfolio-acceptance"]:
        from trading.research.joint_portfolio_acceptance import main as joint_portfolio_acceptance_main
        return joint_portfolio_acceptance_main(argv[1:])
    if argv[:1] == ["joint-portfolio-paper-package"]:
        from trading.research.joint_portfolio_paper_package import main as joint_portfolio_paper_package_main
        return joint_portfolio_paper_package_main(argv[1:])
    if argv[:1] == ["ths-target-readback"]:
        from trading.ths.target_readback import main as ths_target_readback_main
        return ths_target_readback_main(argv[1:])
    if argv[:1] == ["f2-alternate-financials"]:
        from trading.research.f2_alternate_financials import main as f2_alternate_financials_main
        return f2_alternate_financials_main(argv[1:])
    if argv[:1] == ["long-window-strategy-comparison"]:
        from trading.research.long_window_strategy_comparison import main as long_window_strategy_comparison_main
        return long_window_strategy_comparison_main(argv[1:])
    if argv[:1] == ["common-window-strategy-comparison"]:
        from trading.research.common_window_strategy_comparison import main as common_window_strategy_comparison_main
        return common_window_strategy_comparison_main(argv[1:])
    if argv[:1] == ["current-window-strategy-comparison"]:
        from trading.research.current_window_strategy_comparison import main as current_window_strategy_comparison_main
        return current_window_strategy_comparison_main(argv[1:])
    if argv[:1] == ["candidate-adoption-decision"]:
        from trading.research.candidate_adoption_decision import main as candidate_adoption_decision_main
        return candidate_adoption_decision_main(argv[1:])
    if argv[:1] == ["research-adoption-rollup"]:
        from trading.research.research_adoption_rollup import main as research_adoption_rollup_main
        return research_adoption_rollup_main(argv[1:])
    if argv[:1] == ["shortline-adoption-gate"]:
        from trading.research.shortline_adoption_gate import main as shortline_adoption_gate_main
        return shortline_adoption_gate_main(argv[1:])
    if argv[:1] == ["research-decision-matrix"]:
        from trading.research.research_decision_matrix import main as research_decision_matrix_main
        return research_decision_matrix_main(argv[1:])
    if argv[:1] == ["copied-account-replay-proof"]:
        from trading.research.copied_account_replay_proof import main as copied_account_replay_proof_main
        return copied_account_replay_proof_main(argv[1:])
    if argv[:1] == ["strategy-robustness-matrix"]:
        from trading.research.strategy_robustness_matrix import main as strategy_robustness_matrix_main
        return strategy_robustness_matrix_main(argv[1:])
    if argv[:1] == ["candidate-research-selection"]:
        from trading.research.candidate_research_selection import main as candidate_research_selection_main
        return candidate_research_selection_main(argv[1:])
    if argv[:1] == ["f01-local-vertical-slice"]:
        from trading.research.f01_local_vertical_slice import main as f01_local_vertical_slice_main
        return f01_local_vertical_slice_main(argv[1:])
    if argv[:1] == ["f01-source-retrieval-receipt"]:
        from trading.research.f01_source_retrieval_receipt import main as f01_source_retrieval_receipt_main
        return f01_source_retrieval_receipt_main(argv[1:])
    if argv[:1] == ["candidate-catalog-audit"]:
        from trading.research.candidate_catalog_audit import main as candidate_catalog_audit_main
        return candidate_catalog_audit_main(argv[1:])
    if argv[:1] == ["candidate-spec-registry"]:
        from trading.research.candidate_spec_registry import main as candidate_spec_registry_main
        return candidate_spec_registry_main(argv[1:])
    if argv[:1] == ["paper-account-provenance"]:
        from trading.research.paper_account_provenance import main as paper_account_provenance_main
        return paper_account_provenance_main(argv[1:])
    if argv[:1] == ["stockdb-replay-audit"]:
        from trading.research.stockdb_replay_audit import main as stockdb_replay_audit_main
        return stockdb_replay_audit_main(argv[1:])
    if argv[:1] == ["fixed-close-to-opening"]:
        from trading.research.fixed_close_to_opening import main as fixed_close_to_opening_main
        return fixed_close_to_opening_main(argv[1:])
    if argv[:1] == ["stockdb-saved-intent"]:
        from trading.research.saved_account_intent import main as saved_account_intent_main
        return saved_account_intent_main(argv[1:])
    if argv[:1] == ["opening-daily"]:
        from trading.research.opening_daily import main as opening_daily_main
        return opening_daily_main(argv[1:])
    if argv[:1] == ["native-saved-opening"]:
        from trading.research.native_saved_opening import main as native_saved_opening_main
        return native_saved_opening_main(argv[1:])
    if argv[:1] == ["leader-daily-replay"]:
        from trading.research.leader_daily_replay import main as leader_daily_replay_main
        return leader_daily_replay_main(argv[1:])
    if argv[:1] == ["leader-environment"]:
        from trading.research.leader_environment_run import main as leader_environment_main
        return leader_environment_main(argv[1:])
    if argv[:1] == ["leader-identity"]:
        from trading.research.leader_identity_registry import main as leader_identity_main
        return leader_identity_main(argv[1:])
    if argv[:1] == ["leader-identity-episodes"]:
        from trading.research.leader_identity_episode_history import main as leader_identity_episodes_main
        return leader_identity_episodes_main(argv[1:])
    if argv[:1] == ["leader-dated-identity"]:
        from trading.research.leader_dated_identity_catalog import main as leader_dated_identity_main
        return leader_dated_identity_main(argv[1:])
    if argv[:1] == ["leader-dated-catchup-inputs"]:
        from trading.research.leader_dated_catchup_inputs import main as leader_dated_catchup_main
        return leader_dated_catchup_main(argv[1:])
    if argv[:1] == ["leader-dated-catchup-paper"]:
        from trading.research.leader_dated_catchup_paper import main as leader_dated_catchup_paper_main
        return leader_dated_catchup_paper_main(argv[1:])
    if argv[:1] == ["leader-dated-catchup-forward"]:
        from trading.research.leader_dated_catchup_forward import main as leader_dated_catchup_forward_main
        return leader_dated_catchup_forward_main(argv[1:])
    if argv[:1] == ["shortline-native-event-panel"]:
        from trading.research.shortline_native_event_panel import main as shortline_native_panel_main
        return shortline_native_panel_main(argv[1:])
    if argv[:1] == ["shortline-native-event-panel-chain"]:
        from trading.research.shortline_native_event_panel import chain_main as shortline_native_panel_chain_main
        return shortline_native_panel_chain_main(argv[1:])
    if argv[:1] == ["shortline-native-model-generation"]:
        from trading.research.shortline_native_training import main as shortline_native_training_main
        return shortline_native_training_main(argv[1:])
    if argv[:1] == ["shortline-native-frozen-score"]:
        from trading.research.shortline_native_scoring import main as shortline_native_scoring_main
        return shortline_native_scoring_main(argv[1:])
    if argv[:1] == ["shortline-native-evaluation"]:
        from trading.research.shortline_native_evaluation import main as shortline_native_evaluation_main
        return shortline_native_evaluation_main(argv[1:])
    if argv[:1] == ["shortline-native-selection-replay"]:
        from trading.research.shortline_native_selection_replay import main as shortline_native_selection_replay_main
        return shortline_native_selection_replay_main(argv[1:])
    if argv[:1] == ["shortline-native-remaining-edge"]:
        from trading.research.shortline_native_remaining_edge import main as remaining_edge_main
        return remaining_edge_main(argv[1:])
    if argv[:1] == ["official-share-capital"]:
        from trading.research.official_share_capital import main as official_share_capital_main
        return official_share_capital_main(argv[1:])
    if argv[:1] == ["shortline-native-score-publication"]:
        from trading.research.shortline_native_scoring import publication_main
        return publication_main(argv[1:])
    if argv[:1] == ["shortline-score-gate-audit"]:
        from trading.research.shortline_score_gate_audit import main as shortline_score_gate_audit_main
        return shortline_score_gate_audit_main(argv[1:])
    if argv[:1] == ["shortline-native-preopen-plan"]:
        from trading.research.shortline_native_preopen import main as shortline_native_preopen_main
        return shortline_native_preopen_main(argv[1:])
    if argv[:1] == ["shortline-native-candidate-handoff"]:
        from trading.research.shortline_candidate_handoff import main as shortline_candidate_handoff_main
        return shortline_candidate_handoff_main(argv[1:])
    if argv[:1] == ["shortline-baostock-raw-snapshot"]:
        from trading.research.shortline_baostock_raw_snapshot import main as shortline_baostock_raw_snapshot_main
        return shortline_baostock_raw_snapshot_main(argv[1:])
    if argv[:1] == ["shortline-native-preopen-forward"]:
        from trading.research.shortline_native_preopen_forward import main as shortline_native_preopen_forward_main
        return shortline_native_preopen_forward_main(argv[1:])
    if argv[:1] == ["native-intraday-rehearsal"]:
        from trading.research.intraday_native_rehearsal import main as native_intraday_rehearsal_main
        return native_intraday_rehearsal_main(argv[1:])
    if argv[:1] == ["native-intraday-paper"]:
        from trading.native_intraday_paper import main as native_intraday_paper_main
        return native_intraday_paper_main(argv[1:])
    if argv[:1] == ["native-intraday-input"]:
        from trading.native_intraday_feed import main as native_intraday_input_main
        return native_intraday_input_main(argv[1:])
    if argv[:1] == ["leader-candidate-intraday"]:
        from trading.research.leader_candidate_intraday_view import main as leader_candidate_intraday_main
        return leader_candidate_intraday_main(argv[1:])
    if argv[:1] == ["leader-candidate-observe"]:
        from trading.research.leader_candidate_observation import main as leader_candidate_observation_main
        return leader_candidate_observation_main(argv[1:])
    if argv[:1] == ["market-regime-scoreboard"]:
        from trading.research.regime_scoreboard import main as regime_scoreboard_main
        return regime_scoreboard_main(argv[1:])
    if argv[:1] == ["baostock-all-stock-status"]:
        from trading.research.baostock_all_stock_probe import main as all_stock_status_main
        return all_stock_status_main(argv[1:])
    if argv[:1] == ["baostock-daily-slice"]:
        from trading.research.baostock_daily_slice import main as daily_slice_main
        return daily_slice_main(argv[1:])
    if argv[:1] == ["baostock-full-close"]:
        from trading.research.baostock_full_close import main as full_close_main
        return full_close_main(argv[1:])
    if argv[:1] == ["baostock-bulk-close-import"]:
        from trading.research.baostock_bulk_close import main as bulk_close_main
        return bulk_close_main(argv[1:])
    if argv[:1] == ["bulk-close-cycle"]:
        from trading.research.bulk_close_cycle import main as bulk_close_cycle_main
        return bulk_close_cycle_main(argv[1:])
    if argv[:1] == ["baostock-bulk-extras"]:
        from trading.research.baostock_bulk_extras import main as bulk_extras_main
        return bulk_extras_main(argv[1:])
    if argv[:1] == ["leader-close-preview"]:
        from trading.research.leader_close_preview import main as close_preview_main
        return close_preview_main(argv[1:])
    if argv[:1] == ["leader-history-repair-preview"]:
        from trading.research.leader_history_repair_preview import main as history_repair_preview_main
        return history_repair_preview_main(argv[1:])
    if argv[:1] == ["leader-recent-source-preview"]:
        from trading.research.leader_recent_source_preview import main as recent_source_preview_main
        return recent_source_preview_main(argv[1:])
    if argv[:1] == ["leader-forward-run"]:
        from trading.research.leader_forward_run import main as leader_forward_main
        return leader_forward_main(argv[1:])
    if argv[:1] == ["leader-published-inputs"]:
        from trading.research.leader_published_inputs import main as leader_published_inputs_main
        return leader_published_inputs_main(argv[1:])
    if argv[:1] == ["leader-historical-turnover-inputs"]:
        from trading.research.leader_historical_turnover_inputs import main as leader_historical_turnover_inputs_main
        return leader_historical_turnover_inputs_main(argv[1:])
    if argv[:1] == ["leader-published-forward"]:
        from trading.research.leader_published_forward_run import main as leader_published_forward_main
        return leader_published_forward_main(argv[1:])
    if argv[:1] == ["leader-close-publication"]:
        from trading.research.leader_close_publication import main as leader_close_publication_main
        return leader_close_publication_main(argv[1:])
    if argv[:1] == ["opening-pack"]:
        from trading.research.opening_pack import main as opening_pack_main
        return opening_pack_main(argv[1:])
    if argv[:1] == ["next-session-preview"]:
        from trading.research.next_session_preview import main as next_session_preview_main
        return next_session_preview_main(argv[1:])
    if argv[:1] == ["factor-current-score"]:
        from trading.research.factor_current_score import main as factor_current_score_main
        return factor_current_score_main(argv[1:])
    if argv[:1] == ["trader-desk-snapshot"]:
        from trading.research.trader_desk_snapshot import main as trader_desk_snapshot_main
        return trader_desk_snapshot_main(argv[1:])
    if argv[:1] == ["offline-strategy-plan"]:
        from trading.research.offline_strategy_plan import main as offline_strategy_plan_main
        return offline_strategy_plan_main(argv[1:])
    if argv[:1] == ["paper-lifecycle-rehearsal"]:
        from trading.research.paper_lifecycle_rehearsal import main as paper_lifecycle_main
        return paper_lifecycle_main(argv[1:])
    if argv[:1] == ["paper-execution-attribution"]:
        from trading.research.paper_execution_attribution import main as paper_execution_attribution_main
        return paper_execution_attribution_main(argv[1:])
    if argv[:1] == ["offline-portfolio-risk"]:
        from trading.research.offline_portfolio_risk import main as offline_portfolio_risk_main
        return offline_portfolio_risk_main(argv[1:])
    if argv[:1] == ["strategy-capacity-cost"]:
        from trading.research.strategy_capacity_cost_comparison import main as strategy_capacity_cost_main
        return strategy_capacity_cost_main(argv[1:])
    if argv[:1] == ["paper-release-sequence"]:
        from trading.research.paper_release_sequence import main as paper_release_sequence_main
        return paper_release_sequence_main(argv[1:])
    if argv[:1] == ["paper-recovery-handoff"]:
        from trading.research.paper_recovery_handoff import main as paper_recovery_handoff_main
        return paper_recovery_handoff_main(argv[1:])
    if argv[:1] == ["paper-strategy-selection"]:
        from trading.research.paper_strategy_selection import main as paper_strategy_selection_main
        return paper_strategy_selection_main(argv[1:])
    if argv[:1] == ["research-comparison"]:
        from trading.research_comparison import main as research_comparison_main
        return research_comparison_main(argv[1:])
    if argv[:1] == ["research-robustness"]:
        from trading.research_robustness import main as research_robustness_main
        return research_robustness_main(argv[1:])
    if argv[:1] == ["alpha158-turnover-replay"]:
        from trading.research.alpha158_turnover_replay import main as alpha158_turnover_main
        return alpha158_turnover_main(argv[1:])
    if argv[:1] == ["factor-mine"]:
        from trading.research.experiment import main as research_main
        return research_main(argv[1:])
    if argv[:1] == ["research-auto-cycle"]:
        from trading.research.auto_idea_cycle import main as research_auto_cycle_main
        return research_auto_cycle_main(argv[1:])
    if argv[:1] == ["idea-frontier"]:
        from trading.research.idea_frontier import main as idea_frontier_main
        return idea_frontier_main(argv[1:])
    if argv[:1] == ["capability-gap-scan"]:
        from trading.research.capability_gap_scan import main as capability_gap_scan_main
        return capability_gap_scan_main(argv[1:])
    if argv[:1] == ["seat-collect"]:
        from trading.research.seats import main as seats_main
        return seats_main(argv[1:])
    if argv[:1] == ["seat-trend"]:
        from trading.research.seat_trend import main as trend_main
        return trend_main(argv[1:])
    if argv[:1] == ["seat-bulk-replay"]:
        from trading.research.bulk_seat_trend import main as seat_bulk_main
        return seat_bulk_main(argv[1:])
    if argv[:1] == ["seat-dataset"]:
        from trading.research.seat_dataset import main as dataset_main
        return dataset_main(argv[1:])
    if argv[:1] == ["market-dataset"]:
        from trading.market_dataset import main as market_dataset_main
        return market_dataset_main(argv[1:])
    if argv[:1] == ["stockdb-baostock-extension"]:
        from trading.research.stockdb_baostock_extension import main as stockdb_baostock_extension_main
        return stockdb_baostock_extension_main(argv[1:])
    if argv[:1] == ["stockdb-alias-projection"]:
        from trading.research.stockdb_alias_projection import main as stockdb_alias_projection_main
        return stockdb_alias_projection_main(argv[1:])
    if argv[:1] == ["stockdb-2023-reconcile"]:
        from trading.research.stockdb_2023_reconciliation import main as stockdb_2023_reconciliation_main
        return stockdb_2023_reconciliation_main(argv[1:])
    if argv[:1] == ["stockdb-semantic-policy"]:
        from trading.research.stockdb_semantic_policy import main as stockdb_semantic_policy_main
        return stockdb_semantic_policy_main(argv[1:])
    if argv[:1] == ["stockdb-paper-target-package"]:
        from trading.research.stockdb_paper_target_package import main as stockdb_paper_target_package_main
        return stockdb_paper_target_package_main(argv[1:])
    if argv[:1] == ["candidate-shadow-paper"]:
        from trading.research.candidate_shadow_paper import main as candidate_shadow_paper_main
        return candidate_shadow_paper_main(argv[1:])
    if argv[:1] == ["daily-candidate-paper-batch"]:
        from trading.research.daily_candidate_paper_batch import main as daily_candidate_paper_batch_main
        return daily_candidate_paper_batch_main(argv[1:])
    if argv[:1] == ["candidate-daily-input-bundle"]:
        from trading.research.candidate_daily_input_bundle import main as candidate_daily_input_bundle_main
        return candidate_daily_input_bundle_main(argv[1:])
    if argv[:1] == ["candidate-daily-paper-run"]:
        from trading.research.candidate_daily_paper_run import main as candidate_daily_paper_run_main
        return candidate_daily_paper_run_main(argv[1:])
    if argv[:1] == ["next-close-d0-d1"]:
        from trading.research.next_close_d0_d1 import main as next_close_d0_d1_main
        return next_close_d0_d1_main(argv[1:])
    if argv[:1] == ["post-close-d0-d1"]:
        from trading.research.post_close_d0_d1 import main as post_close_d0_d1_main
        return post_close_d0_d1_main(argv[1:])
    if argv[:1] == ["internal-release-candidate"]:
        from trading.research.internal_release_candidate import main as internal_release_candidate_main
        return internal_release_candidate_main(argv[1:])
    if argv[:1] == ["virtual-strategy-sleeves"]:
        from trading.research.virtual_strategy_sleeves import main as virtual_strategy_sleeves_main
        return virtual_strategy_sleeves_main(argv[1:])
    if argv[:1] == ["stockdb-baostock-splice-inventory"]:
        from trading.research.stockdb_baostock_splice_inventory import main as stockdb_baostock_splice_inventory_main
        return stockdb_baostock_splice_inventory_main(argv[1:])
    if argv[:1] == ["market-snapshot"]:
        from trading.market_snapshot import main as market_snapshot_main
        return market_snapshot_main(argv[1:])
    if argv[:1] == ["market-indices"]:
        from trading.research.market_indices import main as market_indices_main
        return market_indices_main(argv[1:])
    if argv[:1] == ["instrument-master"]:
        from trading.research.instrument_master import main as instrument_master_main
        return instrument_master_main(argv[1:])
    if argv[:1] == ["instrument-master-coverage"]:
        from trading.research.instrument_master import coverage_main as instrument_master_coverage_main
        return instrument_master_coverage_main(argv[1:])
    if argv[:1] == ["research-scope"]:
        from trading.research.instrument_master import scope_main as research_scope_main
        return research_scope_main(argv[1:])
    if argv[:1] == ["master-replay"]:
        from trading.research.master import main as master_main
        return master_main(argv[1:])
    if argv[:1] == ["etf-quote-audit"]:
        from trading.research.quote_precision import main as quote_main
        return quote_main(argv[1:])
    if argv[:1] == ["etf-reconcile"]:
        from trading.research.etf_reconciliation import main as etf_reconcile_main
        return etf_reconcile_main(argv[1:])
    if argv[:1] == ["fund-disclosures"]:
        from trading.research.fund_disclosures import main as fund_disclosures_main
        return fund_disclosures_main(argv[1:])
    if argv[:1] == ["fund-issuer-history"]:
        from trading.research.gt_fund_history import main as issuer_history_main
        return issuer_history_main(argv[1:])
    if argv[:1] == ["fund-chinaamc-history"]:
        from trading.research.chinaamc_fund_history import main as chinaamc_history_main
        return chinaamc_history_main(argv[1:])
    if argv[:1] == ["fund-huaan-history"]:
        from trading.research.huaan_fund_history import main as huaan_history_main
        return huaan_history_main(argv[1:])
    if argv[:1] == ["fund-statistics"]:
        from trading.research.fund_statistics import main as fund_statistics_main
        return fund_statistics_main(argv[1:])
    if argv[:1] == ["fund-dayk"]:
        from trading.research.fund_dayk import main as fund_dayk_main
        return fund_dayk_main(argv[1:])
    if argv[:1] == ["fund-replay-snapshot"]:
        from trading.research.fund_replay_snapshot import main as fund_snapshot_main
        return fund_snapshot_main(argv[1:])
    if argv[:1] == ["fund-ledger-probe"]:
        from trading.research.fund_replay_snapshot import probe_main
        return probe_main(argv[1:])
    if argv[:1] == ["fund-trend"]:
        from trading.research.fund_trend import main as fund_trend_main
        return fund_trend_main(argv[1:])
    if argv[:1] == ["daily-source-probe"]:
        from trading.research.daily_source_probe import main as daily_source_main
        return daily_source_main(argv[1:])
    if argv[:1] == ["adjusted-price-check"]:
        from trading.research.adjusted_prices import main as adjusted_price_main
        return adjusted_price_main(argv[1:])
    if argv[:1] == ["bulk-adjusted-prices"]:
        from trading.research.bulk_adjusted_prices import main as bulk_adjusted_main
        return bulk_adjusted_main(argv[1:])
    if argv[:1] == ["research-price-boundaries"]:
        from trading.research.price_boundaries import main as price_boundaries_main
        return price_boundaries_main(argv[1:])
    if argv[:1] == ["extend-derived-factor-view"]:
        from trading.research.extend_derived_factor_view import main as extend_derived_factor_main
        return extend_derived_factor_main(argv[1:])
    if argv[:1] == ["qlib-research-provider"]:
        from trading.research.qlib_research_provider import main as qlib_provider_main
        return qlib_provider_main(argv[1:])
    if argv[:1] == ["qlib-native-features"]:
        from trading.research.qlib_native_features import main as qlib_native_main
        return qlib_native_main(argv[1:])
    if argv[:1] == ["stockdb-qlib-provider"]:
        from trading.research.stockdb_qlib_provider import main as stockdb_qlib_provider_main
        return stockdb_qlib_provider_main(argv[1:])
    if argv[:1] == ["recent-source-qlib-provider"]:
        from trading.research.recent_source_qlib_provider import main as recent_qlib_provider_main
        return recent_qlib_provider_main(argv[1:])
    if argv[:1] == ["recent-source-alpha158"]:
        from trading.research.recent_source_alpha158 import main as recent_alpha158_main
        return recent_alpha158_main(argv[1:])
    if argv[:1] == ["recent-source-alpha158-panel-replay"]:
        from trading.research.recent_alpha158_panel_replay import main as recent_alpha158_replay_main
        return recent_alpha158_replay_main(argv[1:])
    if argv[:1] == ["recent-source-alpha158-target-publication"]:
        from trading.research.recent_alpha158_target_publication import main as recent_alpha158_publication_main
        return recent_alpha158_publication_main(argv[1:])
    if argv[:1] == ["recent-source-alpha158-forward-run"]:
        from trading.research.recent_alpha158_forward_run import main as recent_alpha158_forward_main
        return recent_alpha158_forward_main(argv[1:])
    if argv[:1] == ["recent-source-alpha158-split-forward"]:
        from trading.research.recent_alpha158_split_forward import main as recent_alpha158_split_main
        return recent_alpha158_split_main(argv[1:])
    if argv[:1] == ["recent-source-shortline-panel"]:
        from trading.research.recent_shortline_panel import main as recent_shortline_panel_main
        return recent_shortline_panel_main(argv[1:])
    if argv[:1] == ["recent-shortline-panel-replay"]:
        from trading.research.recent_shortline_panel_replay import main as recent_shortline_replay_main
        return recent_shortline_replay_main(argv[1:])
    if argv[:1] == ["leader-eight-type-panel-replay"]:
        from trading.research.leader_eight_type_panel_replay import main as leader_eight_type_replay_main
        return leader_eight_type_replay_main(argv[1:])
    if argv[:1] == ["leader-eight-type-inputs"]:
        from trading.research.leader_eight_type_inputs import main as leader_eight_type_inputs_main
        return leader_eight_type_inputs_main(argv[1:])
    if argv[:1] == ["leader-eight-type-paper"]:
        from trading.research.leader_eight_type_paper import main as leader_eight_type_paper_main
        return leader_eight_type_paper_main(argv[1:])
    if argv[:1] == ["leader-eight-type-forward-register"]:
        from trading.research.leader_eight_type_forward_registration import main as leader_eight_type_register_main
        return leader_eight_type_register_main(argv[1:])
    if argv[:1] == ["leader-eight-type-split-forward"]:
        from trading.research.leader_eight_type_split_forward import main as eight_type_split_main
        return eight_type_split_main(argv[1:])
    if argv[:1] == ["leader-eight-type-forward"]:
        from trading.research.leader_eight_type_forward_run import main as leader_eight_type_forward_main
        return leader_eight_type_forward_main(argv[1:])
    if argv[:1] in (["leader-eight-type-opening-pack"], ["published-account-opening-pack"]):
        from trading.research.leader_eight_type_opening import main as eight_type_opening_main
        return eight_type_opening_main(argv[1:])
    if argv[:1] == ["stockdb-factor-features"]:
        from trading.research.stockdb_factor_features import main as stockdb_factor_features_main
        return stockdb_factor_features_main(argv[1:])
    if argv[:1] == ["stockdb-factor-score-diagnostic"]:
        from trading.research.stockdb_factor_score_diagnostic import main as stockdb_factor_score_diagnostic_main
        return stockdb_factor_score_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-factor-monthly-ridge-diagnostic"]:
        from trading.research.stockdb_factor_monthly_ridge_diagnostic import main as stockdb_factor_monthly_ridge_diagnostic_main
        return stockdb_factor_monthly_ridge_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-factor-monthly-ridge-replay"]:
        from trading.research.stockdb_factor_monthly_ridge_replay import main as stockdb_factor_monthly_ridge_replay_main
        return stockdb_factor_monthly_ridge_replay_main(argv[1:])
    if argv[:1] == ["stockdb-factor-monthly-ridge-horizon-variant"]:
        from trading.research.stockdb_factor_monthly_ridge_horizon_variant import main as stockdb_factor_monthly_ridge_horizon_variant_main
        return stockdb_factor_monthly_ridge_horizon_variant_main(argv[1:])
    if argv[:1] == ["stockdb-dated-eligibility-view"]:
        from trading.research.stockdb_eligibility_view import main as stockdb_dated_eligibility_view_main
        return stockdb_dated_eligibility_view_main(argv[1:])
    if argv[:1] == ["stockdb-untouched-forward-holdout"]:
        from trading.research.stockdb_untouched_forward_holdout import main as stockdb_untouched_forward_holdout_main
        return stockdb_untouched_forward_holdout_main(argv[1:])
    if argv[:1] == ["stockdb-dated-action-context-view"]:
        from trading.research.stockdb_action_context_view import main as stockdb_dated_action_context_view_main
        return stockdb_dated_action_context_view_main(argv[1:])
    if argv[:1] == ["action-publication-sidecar"]:
        from trading.research.action_publication_sidecar import main as action_publication_sidecar_main
        return action_publication_sidecar_main(argv[1:])
    if argv[:1] == ["f01-publication-sidecar"]:
        from trading.research.f01_publication_sidecar import main as f01_publication_sidecar_main
        return f01_publication_sidecar_main(argv[1:])
    if argv[:1] == ["public-seat-replay-audit"]:
        from trading.research.public_seat_replay_audit import main as public_seat_replay_audit_main
        return public_seat_replay_audit_main(argv[1:])
    if argv[:1] == ["stockdb-alpha158-lgbm-diagnostic"]:
        from trading.research.stockdb_alpha158_lgbm_diagnostic import main as stockdb_alpha158_lgbm_diagnostic_main
        return stockdb_alpha158_lgbm_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-alpha158-lgbm-batch-diagnostic"]:
        from trading.research.stockdb_alpha158_lgbm_batch_diagnostic import main as stockdb_alpha158_lgbm_batch_diagnostic_main
        return stockdb_alpha158_lgbm_batch_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-alpha158-lgbm-batch-merge"]:
        from trading.research.stockdb_score_batch_merge import main as stockdb_score_batch_merge_main
        return stockdb_score_batch_merge_main(argv[1:])
    if argv[:1] == ["stockdb-alpha158-lgbm-panel-replay"]:
        from trading.research.stockdb_alpha158_lgbm_panel_replay import main as stockdb_alpha158_lgbm_panel_replay_main
        return stockdb_alpha158_lgbm_panel_replay_main(argv[1:])
    if argv[:1] == ["leader-52week-monthly"]:
        from trading.research.leader_52week_monthly import main as leader_high52_main
        return leader_high52_main(argv[1:])
    if argv[:1] == ["saved-monthly-rank-diagnostic"]:
        from trading.research.saved_monthly_rank_diagnostic import main as saved_rank_main
        return saved_rank_main(argv[1:])
    if argv[:1] == ["leader-momentum-benchmark"]:
        from trading.research.leader_momentum_benchmark import main
        return main(argv[1:])
    if argv[:1] == ["current-risk-warning-source"]:
        from trading.research.current_risk_warning_source import main
        return main(argv[1:])
    if argv[:1] == ["leader-trend-holding-comparison"]:
        from trading.research.leader_trend_holding_comparison import main
        return main(argv[1:])
    if argv[:1] == ["weekly-liquidity-reversal"]:
        from trading.research.weekly_liquidity_reversal import cli
        return cli(argv[1:])
    if argv[:1] == ["high-volume-attention"]:
        from trading.research.high_volume_attention import main
        return main(argv[1:])
    if argv[:1] == ["shortline-objective-comparison"]:
        from trading.research.shortline_objective_comparison import main
        return main(argv[1:])
    if argv[:1] == ["market-residual-momentum"]:
        from trading.research.market_residual_replay import main
        return main(argv[1:])
    if argv[:1] == ["market-residual-outcomes"]:
        from trading.research.market_residual_outcomes import main
        return main(argv[1:])
    if argv[:1] == ["annual-quality-value-replay"]:
        from trading.research.annual_quality_value_replay import main
        return main(argv[1:])
    if argv[:1] == ["leader-type-router-replay"]:
        from trading.research.leader_type_router_replay import main
        return main(argv[1:])
    if argv[:1] == ["leader-type-opportunity-diagnostic"]:
        from trading.research.leader_type_opportunity_diagnostic import main
        return main(argv[1:])
    if argv[:1] == ["opening-plan-execution-reconciliation"]:
        from trading.research.opening_plan_execution_reconciliation import main
        return main(argv[1:])
    if argv[:1] == ["trend-atr-stop-replay"]:
        from trading.research.trend_atr_stop_replay import main
        return main(argv[1:])
    if argv[:1] == ["leader-type-router-paper"]:
        from trading.research.leader_type_router_paper import main
        return main(argv[1:])
    if argv[:1] == ["leader-type-router-forward"]:
        from trading.research.leader_type_router_forward import main
        return main(argv[1:])
    if argv[:1] == ["leader-known-identity-paper"]:
        from trading.research.leader_known_identity_paper import main
        return main(argv[1:])
    if argv[:1] == ["leader-selection-control"]:
        from trading.research.leader_selection_control import main
        return main(argv[1:])
    if argv[:1] == ["leader-momentum-year"]:
        from trading.research.leader_momentum_year import main
        return main(argv[1:])
    if argv[:1] == ["leader-momentum-capacity"]:
        from trading.research.leader_momentum_capacity import main
        return main(argv[1:])
    if argv[:1] == ["leader-momentum-capacity-continue"]:
        from trading.research.leader_momentum_capacity_continue import main
        return main(argv[1:])
    if argv[:1] == ["leader-momentum-source-continue"]:
        from trading.research.leader_momentum_source_continue import main
        return main(argv[1:])
    if argv[:1] == ["native-lowvol-replay"]:
        from trading.research.native_lowvol_replay import main as native_lowvol_replay_main
        return native_lowvol_replay_main(argv[1:])
    if argv[:1] == ["stockdb-controlled-signal-comparison"]:
        from trading.research.stockdb_controlled_signal_comparison import main as controlled_signal_comparison_main
        return controlled_signal_comparison_main(argv[1:])
    if argv[:1] == ["stockdb-alpha158-lgbm-signal-frame"]:
        from trading.research.stockdb_alpha158_lgbm_signal_frame import main as stockdb_alpha158_lgbm_signal_frame_main
        return stockdb_alpha158_lgbm_signal_frame_main(argv[1:])
    if argv[:1] == ["stockdb-factor-lineage-overlap"]:
        from trading.research.stockdb_factor_lineage_overlap import main as stockdb_factor_lineage_overlap_main
        return stockdb_factor_lineage_overlap_main(argv[1:])
    if argv[:1] == ["stockdb-factor-lineage-compare"]:
        from trading.research.stockdb_factor_lineage_compare import main as stockdb_factor_lineage_compare_main
        return stockdb_factor_lineage_compare_main(argv[1:])
    if argv[:1] == ["stockdb-eligibility-compat"]:
        from trading.research.stockdb_eligibility_compat import main as stockdb_eligibility_compat_main
        return stockdb_eligibility_compat_main(argv[1:])
    if argv[:1] == ["stockdb-tradability-compare"]:
        from trading.research.stockdb_tradability_compare import main as stockdb_tradability_compare_main
        return stockdb_tradability_compare_main(argv[1:])
    if argv[:1] == ["stockdb-factor-rank-diagnostic"]:
        from trading.research.stockdb_factor_rank_diagnostic import main as stockdb_factor_rank_diagnostic_main
        return stockdb_factor_rank_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-factor-signal-frame"]:
        from trading.research.stockdb_factor_signal_frame import main as stockdb_factor_signal_frame_main
        return stockdb_factor_signal_frame_main(argv[1:])
    if argv[:1] == ["stockdb-action-gate-diagnostic"]:
        from trading.research.stockdb_action_gate_diagnostic import main as stockdb_action_gate_diagnostic_main
        return stockdb_action_gate_diagnostic_main(argv[1:])
    if argv[:1] == ["stockdb-research-target-preview"]:
        from trading.research.stockdb_research_target_preview import main as stockdb_research_target_preview_main
        return stockdb_research_target_preview_main(argv[1:])
    if argv[:1] == ["stockdb-research-prefill-request"]:
        from trading.research.stockdb_research_prefill_request import main as stockdb_research_prefill_request_main
        return stockdb_research_prefill_request_main(argv[1:])
    if argv[:1] == ["shortline-public-style-ablation"]:
        from trading.research.shortline_public_style_ablation import main as shortline_public_style_ablation_main
        return shortline_public_style_ablation_main(argv[1:])
    if argv[:1] == ["stock-action-scope"]:
        from trading.research.action_scope import main as action_scope_main
        return action_scope_main(argv[1:])
    if argv[:1] == ["stock-action-view"]:
        from trading.research.action_scope import pin_main as action_view_main
        return action_view_main(argv[1:])
    if argv[:1] == ["stock-action-events"]:
        from trading.research.action_events import main as action_events_main
        return action_events_main(argv[1:])
    if argv[:1] == ["stock-action-references"]:
        from trading.research.action_events import references_main as action_references_main
        return action_references_main(argv[1:])
    if argv[:1] == ["stock-action-disclosures"]:
        from trading.research.action_disclosures import main as action_disclosures_main
        return action_disclosures_main(argv[1:])
    if argv[:1] == ["stock-action-cash-confirmation"]:
        from trading.research.action_confirmations import main as action_confirmation_main
        return action_confirmation_main(argv[1:])
    if argv[:1] == ["stock-distribution-references"]:
        from trading.research.distribution_references import main as distribution_references_main
        return distribution_references_main(argv[1:])
    if argv[:1] == ["stock-action-terms"]:
        from trading.research.action_terms import main as action_terms_main
        return action_terms_main(argv[1:])
    if argv[:1] == ["stock-action-reviewed-amend"]:
        from trading.research.action_reviewed_amendment import main as reviewed_amendment_main
        return reviewed_amendment_main(argv[1:])
    if argv[:1] == ["profitability-period-diagnostics"]:
        from trading.research.profitability_periods import main as profitability_periods_main
        return profitability_periods_main(argv[1:])
    if argv[:1] == ["profitability-inputs"]:
        from trading.research.profitability_inputs import main as profitability_inputs_main
        return profitability_inputs_main(argv[1:])
    if argv[:1] == ["factor-baseline-backfill"]:
        from trading.research.factor_baselines import main as factor_baseline_main
        return factor_baseline_main(argv[1:])
    if argv[:1] == ["factor-baseline-merge"]:
        from trading.research.factor_baselines import merge_main as factor_baseline_merge_main
        return factor_baseline_merge_main(argv[1:])
    if argv[:1] == ["factor-baseline-extend"]:
        from trading.research.factor_baselines import extend_main as factor_baseline_extend_main
        return factor_baseline_extend_main(argv[1:])
    if argv[:1] in (["daily-etf-backfill"], ["daily-bulk-backfill"]):
        from trading.research.daily_source_probe import backfill_main
        return backfill_main(argv[1:])
    if argv[:1] == ["extend-daily-bulk"]:
        from trading.research.extend_daily_bulk import main as extend_daily_bulk_main
        return extend_daily_bulk_main(argv[1:])
    if argv[:1] == ["financials"]:
        from trading.research.financials import main as financials_main
        return financials_main(argv[1:])
    if argv[:1] == ["f01-scoped-field-coverage"]:
        from trading.research.f01_scoped_field_coverage import main as f01_scoped_field_coverage_main
        return f01_scoped_field_coverage_main(argv[1:])
    if argv[:1] == ["f01-coverage-funnel"]:
        from trading.research.f01_coverage_funnel import main as f01_coverage_funnel_main
        return f01_coverage_funnel_main(argv[1:])
    if argv[:1] == ["f2-fact-bridge"]:
        from trading.research.f2_fact_bridge import main as f2_fact_bridge_main
        return f2_fact_bridge_main(argv[1:])
    if argv[:1] == ["f2-source-snapshot"]:
        from trading.research.f2_source_snapshot import main as f2_source_snapshot_main
        return f2_source_snapshot_main(argv[1:])
    if argv[:1] == ["f2-wayback-probe"]:
        from trading.research.f2_wayback_probe import main as f2_wayback_probe_main
        return f2_wayback_probe_main(argv[1:])
    if argv[:1] == ["f2-vertical-slice"]:
        from trading.research.f2_vertical_slice import main as f2_vertical_slice_main
        return f2_vertical_slice_main(argv[1:])
    if argv[:1] == ["f2-bridge-vertical-slice"]:
        from trading.research.f2_bridge_vertical_slice import main as f2_bridge_vertical_slice_main
        return f2_bridge_vertical_slice_main(argv[1:])
    if argv[:1] == ["alternate-stock-daily"]:
        from trading.research.alternate_daily import main as alternate_daily_main
        return alternate_daily_main(argv[1:])
    if argv[:1] == ["alternate-financial-statements"]:
        from trading.research.alternate_financials import main as alternate_financials_main
        return alternate_financials_main(argv[1:])
    if argv[:1] == ["issuer-annual-inputs"]:
        from trading.research.issuer_annual_inputs import main as issuer_annual_inputs_main
        return issuer_annual_inputs_main(argv[1:])
    if argv[:1] == ["bulk-annual-financials"]:
        from trading.research.bulk_annual_financials import main as bulk_annual_financials_main
        return bulk_annual_financials_main(argv[1:])
    if argv[:1] == ["annual-financial-inputs"]:
        from trading.research.annual_financial_inputs import main as annual_financial_inputs_main
        return annual_financial_inputs_main(argv[1:])
    if argv[:1] == ["annual-quality-features"]:
        from trading.research.annual_quality_features import main as annual_quality_features_main
        return annual_quality_features_main(argv[1:])
    if argv[:1] == ["annual-quality-pit-audit"]:
        from trading.research.annual_quality_pit_audit import main as annual_quality_pit_audit_main
        return annual_quality_pit_audit_main(argv[1:])
    if argv[:1] == ["annual-quality-portfolios"]:
        from trading.research.annual_quality_portfolios import main as annual_quality_portfolios_main
        return annual_quality_portfolios_main(argv[1:])
    if argv[:1] == ["annual-quality-forward-inputs"]:
        from trading.research.annual_quality_forward_inputs import main as annual_quality_forward_inputs_main
        return annual_quality_forward_inputs_main(argv[1:])
    if argv[:1] == ["annual-quality-action-repair"]:
        from trading.research.annual_quality_action_repair import main as annual_quality_action_repair_main
        return annual_quality_action_repair_main(argv[1:])
    if argv[:1] == ["leader-eight-type-residual"]:
        from trading.research.leader_eight_type_residual_paper import main as leader_eight_type_residual_main
        return leader_eight_type_residual_main(argv[1:])
    if argv[:1] == ["market-replay"]:
        from trading.research.market_replay import main as market_replay_main
        return market_replay_main(argv[1:])
    if argv[:1] == ["monthly-timing"]:
        from trading.research.monthly_timing import main as monthly_timing_main
        return monthly_timing_main(argv[1:])
    if argv[:1] == ["volatility-timing"]:
        from trading.research.volatility_timing import main as volatility_timing_main
        return volatility_timing_main(argv[1:])
    if argv[:1] == ["low-risk-stocks"]:
        from trading.research.low_risk import main as low_risk_main
        return low_risk_main(argv[1:])
    if argv[:1] == ["momentum-gate"]:
        from trading.research.momentum_gate import main as momentum_gate_main
        return momentum_gate_main(argv[1:])
    if argv[:1] == ["continuous-momentum"]:
        from trading.research.continuous_momentum import main as continuous_momentum_main
        return continuous_momentum_main(argv[1:])
    if argv[:1] == ["weekly-reversal"]:
        from trading.research.weekly_reversal import main as weekly_reversal_main
        return weekly_reversal_main(argv[1:])
    if argv[:1] == ["weekly-reversal-sources"]:
        from trading.research.weekly_reversal import sources_main as weekly_sources_main
        return weekly_sources_main(argv[1:])
    if argv[:1] == ["stock-risk-notices"]:
        from trading.research.risk_notices import main as risk_notices_main
        return risk_notices_main(argv[1:])
    if argv[:1] == ["research-compare"]:
        from trading.research_comparison import main as research_compare_main
        return research_compare_main(argv[1:])
    if argv[:1] == ["loss-attribution"]:
        from trading.research.loss_attribution import main as attribution_main
        return attribution_main(argv[1:])
    if argv[:1] == ["factor-candidate-attribution"]:
        from trading.research.factor_candidate_attribution import main as factor_attribution_main
        return factor_attribution_main(argv[1:])
    if argv[:1] == ["factor-candidate-cadence-stability"]:
        from trading.research.factor_candidate_cadence_stability import main as factor_cadence_stability_main
        return factor_cadence_stability_main(argv[1:])
    if argv[:1] == ["factor-candidate-cost-replay"]:
        from trading.research.factor_candidate_cost_replay import main as factor_cost_main
        return factor_cost_main(argv[1:])
    if argv[:1] == ["factor-candidate-family-replay"]:
        from trading.research.factor_candidate_family_replay import main as factor_family_main
        return factor_family_main(argv[1:])
    if argv[:1] == ["factor-candidate-library-ablation"]:
        from trading.research.factor_candidate_library_ablation import main as factor_library_ablation_main
        return factor_library_ablation_main(argv[1:])
    if argv[:1] == ["risk-concentration"]:
        from trading.research.risk_concentration import main as risk_concentration_main
        return risk_concentration_main(argv[1:])
    if argv[:1] == ["stress-exposure"]:
        from trading.research.stress_exposure import main as stress_exposure_main
        return stress_exposure_main(argv[1:])
    if argv[:1] == ["factor-regime-overlay"]:
        from trading.research.factor_regime_overlay import main as factor_regime_main
        return factor_regime_main(argv[1:])
    if argv[:1] == ["candidate-admission"]:
        from trading.research.admission_gate import main as admission_main
        return admission_main(argv[1:])
    if argv[:1] == ["turnover-attribution"]:
        from trading.research.turnover_attribution import main as turnover_main
        return turnover_main(argv[1:])
    if argv[:1] == ["holding-path-diagnostics"]:
        from trading.research.holding_paths import main as holding_paths_main
        return holding_paths_main(argv[1:])
    if argv[:1] == ["valuation-gaps"]:
        from trading.research.valuation_gaps import main as valuation_gaps_main
        return valuation_gaps_main(argv[1:])
    if argv[:1] == ["portfolio-style-exposure"]:
        from trading.research.portfolio_styles import main as portfolio_styles_main
        return portfolio_styles_main(argv[1:])
    if argv[:1] == ["momentum-exit-replay"]:
        from trading.research.momentum_exit import main as momentum_exit_main
        return momentum_exit_main(argv[1:])
    if argv[:1] == ["qarp-replay"]:
        from trading.research.qarp_replay import main as qarp_replay_main
        return qarp_replay_main(argv[1:])
    if argv[:1] == ["cost-aware-replay"]:
        from trading.research.cost_aware_replay import main as cost_replay_main
        return cost_replay_main(argv[1:])
    if argv[:1] == ["factor-family-diagnostics"]:
        from trading.research.factor_families import main as factor_family_main
        return factor_family_main(argv[1:])
    if argv[:1] == ["execution-delay-diagnostics"]:
        from trading.research.execution_delay import main as execution_delay_main
        return execution_delay_main(argv[1:])
    if argv[:1] == ["factor-library-diagnostics"]:
        from trading.research.factor_library_extension import main as factor_library_main
        return factor_library_main(argv[1:])
    if argv[:1] == ["factor-candidate-pack"]:
        from trading.research.factor_candidate_pack import main as factor_candidate_main
        return factor_candidate_main(argv[1:])
    if argv[:1] == ["factor-candidate-replay"]:
        from trading.research.factor_candidate_replay import main as factor_candidate_replay_main
        return factor_candidate_replay_main(argv[1:])
    if argv[:1] == ["factor-replay-comparison"]:
        from trading.research.factor_replay_comparison import main as factor_replay_comparison_main
        return factor_replay_comparison_main(argv[1:])
    if argv[:1] == ["factor-execution-projection"]:
        from trading.research.factor_execution_projection import main as factor_execution_projection_main
        return factor_execution_projection_main(argv[1:])
    if argv[:1] == ["factor-score-export"]:
        from trading.research.factor_score_export import main as factor_score_export_main
        return factor_score_export_main(argv[1:])
    if argv[:1] == ["family-portfolio-replay"]:
        from trading.research.family_portfolios import main as family_portfolio_main
        return family_portfolio_main(argv[1:])
    if argv[:1] == ["capital-allocation-replay"]:
        from trading.research.capital_allocation_replay import main as capital_allocation_main
        return capital_allocation_main(argv[1:])
    if argv[:1] == ["family-ridge-replay"]:
        from trading.research.family_ridge import main as family_ridge_main
        return family_ridge_main(argv[1:])
    if argv[:1] == ["return-cost-replay"]:
        from trading.research.return_cost_replay import main as return_cost_main
        return return_cost_main(argv[1:])
    if argv[:1] == ["return-forecast-diagnostics"]:
        from trading.research.return_forecast_diagnostics import main as return_forecast_main
        return return_forecast_main(argv[1:])
    if argv[:1] == ["family-pipeline"]:
        from trading.research.family_pipeline import main as family_pipeline_main
        return family_pipeline_main(argv[1:])
    if argv[:1] == ["family-holdout"]:
        from trading.research.family_holdout import main as family_holdout_main
        return family_holdout_main(argv[1:])
    if argv[:1] == ["ledger-stress"]:
        from trading.research.ledger_stress import main as ledger_stress_main
        return ledger_stress_main(argv[1:])
    if argv[:1] == ["alpha158-linear-replay"]:
        from trading.research.alpha158_linear import main as alpha158_linear_main
        return alpha158_linear_main(argv[1:])
    if argv[:1] == ["prediction-funnel"]:
        from trading.research.prediction_funnel import main as prediction_funnel_main
        return prediction_funnel_main(argv[1:])
    if argv[:1] == ["head-stability"]:
        from trading.research.head_stability import main as head_stability_main
        return head_stability_main(argv[1:])
    if argv[:1] == ["prospective-research"]:
        from trading.research.prospective_research import main as prospective_research_main
        return prospective_research_main(argv[1:])
    if argv[:1] == ["style-exposure"]:
        from trading.research.style_exposure import main as style_exposure_main
        return style_exposure_main(argv[1:])
    if argv[:1] == ["value-portfolio-replay"]:
        from trading.research.value_portfolios import main as value_portfolio_main
        return value_portfolio_main(argv[1:])
    if argv[:1] == ["profitability-portfolio-replay"]:
        from trading.research.profitability_portfolios import main as profitability_portfolio_main
        return profitability_portfolio_main(argv[1:])
    if argv[:1] == ["family-action-needs"]:
        from trading.research.family_portfolios import actions_main
        return actions_main(argv[1:])
    if argv[:1] == ["family-action-terms"]:
        from trading.research.family_portfolios import terms_main
        return terms_main(argv[1:])
    if argv[:1] == ["stock-defensive"]:
        from trading.research.stock_defensive import main as stock_defensive_main
        return stock_defensive_main(argv[1:])
    if argv[:1] == ["institutional-mix"]:
        from trading.research.institutional_mix import main as institutional_mix_main
        return institutional_mix_main(argv[1:])
    if argv[:1] == ["reproduce-run"]:
        from trading.research.reproduce_run import main as reproduce_main
        return reproduce_main(argv[1:])
    if argv[:1] == ["stockdb-audit"]:
        from trading.stockdb_source import main as stockdb_main
        return stockdb_main(argv[1:])
    if argv[:1] == ["stockdb-freeze"]:
        from trading.stockdb_source import main as stockdb_main
        return stockdb_main([*argv[1:], "--freeze"])
    if argv[:1] == ["stockdb-forward-compare"]:
        from trading.research.stockdb_forward_compare import main as forward_compare_main
        return forward_compare_main(argv[1:])
    if argv[:1] == ["stockdb-independent-source-compare"]:
        from trading.research.stockdb_independent_source_compare import main as independent_source_compare_main
        return independent_source_compare_main(argv[1:])
    if argv[:1] == ["stockdb-independent-sample-compare"]:
        from trading.research.stockdb_independent_sample_compare import main as independent_sample_compare_main
        return independent_sample_compare_main(argv[1:])
    if argv[:1] == ["stockdb-ledger-replay"]:
        from trading.research.stockdb_ledger_replay import main as stockdb_ledger_replay_main
        return stockdb_ledger_replay_main(argv[1:])
    if argv[:1] == ["stockdb-history-inputs"]:
        from trading.research.stockdb_history_inputs import main as stockdb_inputs_main
        return stockdb_inputs_main(argv[1:])
    if argv[:1] == ["stockdb-factor-diagnostics"]:
        from trading.research.stockdb_factor_diagnostics import main as stockdb_diagnostics_main
        return stockdb_diagnostics_main(argv[1:])
    if argv[:1] == ["stockdb-factor-replay"]:
        from trading.research.stockdb_factor_replay import main as stockdb_replay_main
        return stockdb_replay_main(argv[1:])
    if argv[:1] == ["stockdb-source-coverage"]:
        from trading.research.stockdb_source_coverage import main as stockdb_coverage_main
        return stockdb_coverage_main(argv[1:])
    if argv[:1] == ["stockdb-live-coverage"]:
        from trading.research.stockdb_live_coverage import main as stockdb_live_coverage_main
        return stockdb_live_coverage_main(argv[1:])
    if argv[:1] == ["shortline-touchability"]:
        from trading.research.shortline_touchability import main as shortline_touchability_main
        return shortline_touchability_main(argv[1:])
    if argv[:1] == ["shortline-signal-frame"]:
        from trading.research.shortline_signal_frame import main as shortline_signal_frame_main
        return shortline_signal_frame_main(argv[1:])
    if argv[:1] == ["shortline-panel-replay"]:
        from trading.research.shortline_panel_replay import main as shortline_panel_replay_main
        return shortline_panel_replay_main(argv[1:])
    if argv[:1] == ["shortline-fixed-rule-replay"]:
        from trading.research.shortline_fixed_rule_replay import main as shortline_fixed_rule_replay_main
        return shortline_fixed_rule_replay_main(argv[1:])
    if argv[:1] == ["shortline-stage-selection-diagnostic"]:
        from trading.research.shortline_stage_selection_diagnostic import main as shortline_stage_selection_diagnostic_main
        return shortline_stage_selection_diagnostic_main(argv[1:])
    if argv[:1] == ["shortline-split-experiment"]:
        from trading.research.shortline_split_experiment import main as shortline_split_experiment_main
        return shortline_split_experiment_main(argv[1:])
    if argv[:1] == ["shortline-prefill-request"]:
        from trading.research.shortline_prefill_request import main as shortline_prefill_request_main
        return shortline_prefill_request_main(argv[1:])
    if argv[:1] == ["seat-hypothesis-signal-frame"]:
        from trading.research.seat_hypothesis_signal_frame import main as seat_hypothesis_signal_frame_main
        return seat_hypothesis_signal_frame_main(argv[1:])
    if argv[:1] == ["seat-hypothesis-panel-replay"]:
        from trading.research.seat_hypothesis_panel_replay import main as seat_hypothesis_panel_replay_main
        return seat_hypothesis_panel_replay_main(argv[1:])
    if argv[:1] == ["seat-hypothesis-snapshot"]:
        from trading.research.seat_hypothesis_snapshot import main as seat_hypothesis_snapshot_main
        return seat_hypothesis_snapshot_main(argv[1:])
    if argv[:1] == ["stockdb-size-diagnostics"]:
        from trading.research.stockdb_size_diagnostics import main as stockdb_size_main
        return stockdb_size_main(argv[1:])
    if argv[:1] == ["full-market-collect"]:
        from trading.research.full_market import main as full_market_main
        return full_market_main(argv[1:])
    if argv[:1] == ["full-market-audit"]:
        from trading.research.full_market_audit import main as full_market_audit_main
        return full_market_audit_main(argv[1:])
    if argv[:1] == ["full-market-screen"]:
        from trading.research.full_market_screen import main as full_market_screen_main
        return full_market_screen_main(argv[1:])
    if argv[:1] == ["full-market-mix"]:
        from trading.research.full_market_mix import main as full_market_mix_main
        return full_market_mix_main(argv[1:])
    if argv[:1] == ["full-market-alpha158"]:
        from trading.research.full_market_alpha158 import main as full_market_alpha158_main
        return full_market_alpha158_main(argv[1:])
    if argv[:1] == ["full-market-templates"]:
        from trading.research.full_market_templates import main as full_market_templates_main
        return full_market_templates_main(argv[1:])
    if argv[:1] == ["factor-risk-audit"]:
        from trading.research.factor_risk_audit import main as factor_risk_audit_main
        return factor_risk_audit_main(argv[1:])
    if argv[:1] == ["full-market-etf"]:
        from trading.research.full_market_etf import main as full_market_etf_main
        return full_market_etf_main(argv[1:])
    if argv[:1] == ["stockdb-2026-inventory"]:
        from trading.research.stockdb_2026_inventory import main as inventory_main
        return inventory_main(argv[1:])
    from a_stock_committee.cli import main as committee_main
    return committee_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
