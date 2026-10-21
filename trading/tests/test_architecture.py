from pathlib import Path
import subprocess
import sys

import pytest

from trading.architecture import CORE, audit
from trading.symbols import normalize_symbol


def test_real_checkout_dependency_rules():
    result = audit()
    assert result["status"] == "passed", result["violations"]
    assert result["modules_scanned"] > len(CORE)


@pytest.mark.parametrize("module,content,rule", [
    ("trading.contracts", "def hidden():\n    import xtquant\n", "core_requires_stdlib_or_core"),
    ("trading.engine", "from .feed import HistoricalFeed\n", "core_requires_stdlib_or_core"),
    ("trading.research.strategy", "from ..ths import adapter\n", "research_cannot_import_execution_or_gui"),
    ("trading.research.strategy", "import pywinauto\n", "research_cannot_import_execution_or_gui"),
    ("trading.signals", "from a_stock_committee.market_data import normalize_symbol\n", "signals_cannot_load_committee_provider_or_execution"),
    ("trading.contracts", "import importlib\nimportlib.import_module(name)\n", "boundary_dynamic_import_requires_review"),
])
def test_actual_forbidden_imports_are_rejected(tmp_path, module, content, rule):
    for core in CORE:
        path = tmp_path.joinpath(*core.split(".")).with_suffix(".py") if core != "trading" else tmp_path / "trading" / "__init__.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    target = tmp_path.joinpath(*module.split(".")).with_suffix(".py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result = audit(tmp_path)
    assert result["status"] == "failed"
    assert any(v["module"] == module and v["rule"] == rule for v in result["violations"])


def test_signals_import_does_not_initialize_provider_committee():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", "import sys; import trading.signals; "
                             "assert 'a_stock_committee' not in sys.modules; "
                             "assert 'baostock' not in sys.modules; assert 'xtquant' not in sys.modules"],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("alias", ["600000", "SH600000", "sh.600000", "600000.SH", "600000.SSE"])
def test_normalizer_old_entry_is_same_implementation(alias):
    from a_stock_committee.market_data import normalize_symbol as legacy
    assert legacy is normalize_symbol
    assert legacy(alias).vt_symbol == "600000.SSE"
