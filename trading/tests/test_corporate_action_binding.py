from trading.architecture import audit
from trading.corporate_action_binding import ACTION_VIEW_BINDING_ID
from trading.research.research_corporate_action_view import ResearchCorporateActionView


def test_engine_consumers_depend_on_core_action_binding_contract():
    dependencies = audit()["dependencies"]
    for module in ("trading.engine", "trading.intraday_session"):
        assert "trading.corporate_action_binding" in dependencies[module]
        assert "trading.research.research_corporate_action_view" not in dependencies[module]


def test_research_view_exposes_the_core_binding_marker():
    assert ResearchCorporateActionView.binding_contract == ACTION_VIEW_BINDING_ID
