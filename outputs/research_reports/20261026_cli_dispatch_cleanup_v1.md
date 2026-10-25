# CLI dispatch cleanup

Removed the unreachable duplicate `joint-portfolio-paper-package` route from `run_committee.py`. The remaining route still exposes the existing help contract with `--execution-day`, `--settlement-day`, and `--output-dir`.

Focused compatibility tests: 2 passed. No research specification, account path, broker adapter, or execution permission changed.
