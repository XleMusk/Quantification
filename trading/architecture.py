"""Static import constraints for the existing V2 module boundaries."""

import argparse
import ast
from importlib.util import resolve_name
import json
from pathlib import Path
import sys


CORE = frozenset({"trading", "trading.config", "trading.contracts", "trading.engine",
                  "trading.ledger", "trading.rules", "trading.symbols", "trading.broker",
                  "trading.simulated_broker"})
EXECUTION = ("trading.ths", "trading.xtquant_adapter", "bridge", "webapp")
GUI = ("pywinauto", "win32gui", "win32api", "win32con", "uiautomation", "pyautogui")


def _matches(module, prefixes):
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _imports(path, module):
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            target = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
            if node.module is None:
                for alias in node.names:
                    yield target + "." + alias.name, node.lineno
            else:
                yield target, node.lineno
        elif isinstance(node, ast.Call):
            dynamic = ((isinstance(node.func, ast.Name) and node.func.id == "__import__")
                       or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"))
            if dynamic:
                literal = node.args[0] if node.args else None
                yield (literal.value if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
                       else "<dynamic-import-unresolved>"), node.lineno


def audit(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    dependencies, violations = {}, []
    for path in sorted((root / "trading").rglob("*.py")):
        if any(part in {"tests", "__pycache__", ".venv", "env", "venv", "site-packages"} for part in path.relative_to(root).parts):
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        imports = list(_imports(path, module))
        dependencies[module] = sorted({target for target, _ in imports})
        for target, line in imports:
            rule = None
            if module in CORE and target not in CORE and target.split(".")[0] not in sys.stdlib_module_names:
                rule = "core_requires_stdlib_or_core"
            if module.startswith("trading.research.") and _matches(target, EXECUTION + GUI):
                rule = "research_cannot_import_execution_or_gui"
            if module == "trading.signals" and _matches(target, ("a_stock_committee",) + EXECUTION + GUI):
                rule = "signals_cannot_load_committee_provider_or_execution"
            if (module in CORE or module == "trading.signals" or module.startswith("trading.research.")) and target == "<dynamic-import-unresolved>":
                rule = "boundary_dynamic_import_requires_review"
            if rule:
                violations.append({"module": module, "path": path.relative_to(root).as_posix(),
                                   "line": line, "import": target, "rule": rule})
    missing = sorted(CORE - dependencies.keys())
    for module in missing:
        violations.append({"module": module, "rule": "required_core_module_missing"})
    return {"work_package": "A04", "status": "passed" if not violations else "failed",
            "scope": "static_python_imports_not_runtime_authorization", "modules_scanned": len(dependencies),
            "core_modules": sorted(CORE), "dependencies": dependencies, "violations": violations,
            "limits": ["Not a sandbox: exec, plugin code, runtime sys.path changes and native APIs require separate review.",
                       "Data/research coupling is listed for phased migration, not automatically rewritten."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = audit()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "modules_scanned", "violations")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
