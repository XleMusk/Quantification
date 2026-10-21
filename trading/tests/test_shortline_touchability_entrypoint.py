from trading.research import shortline_touchability


def test_shortline_touchability_entrypoint_has_two_research_modes():
    assert shortline_touchability.PROXY_SCRIPT.name == "build_daily_touchability_proxy.py"
    assert shortline_touchability.ADJUSTED_SCRIPT.name == "run_touchability_adjusted_shortline.py"


def test_shortline_touchability_dispatches_without_side_effects(monkeypatch):
    called = []
    monkeypatch.setattr(shortline_touchability, "_run", lambda path: called.append(path) or 0)
    assert shortline_touchability.main(["proxy"]) == 0
    assert shortline_touchability.main(["adjusted-model"]) == 0
    assert called == [shortline_touchability.PROXY_SCRIPT, shortline_touchability.ADJUSTED_SCRIPT]
