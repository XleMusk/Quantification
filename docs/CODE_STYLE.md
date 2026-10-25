# Quantification Code Style

This is the project code standard for changes to the A-share research and paper-trading system. It is derived from `Quantification_code_craft_20261025.docx` and applies to humans and agents equally.

## Business meaning comes first

Use the project vocabulary consistently: `Signal`, `Target Portfolio`, `Order`, `BrokerAdapter`, `Fill`, `Cash`, and `Position` are different objects. Public interfaces must show the time view, units, scope, state changes, return type, and failure behavior. Do not hide a financial decision inside a generic manager or helper.

## Boundaries and dependencies

- Validate raw shape and business meaning once at the input boundary. Internal functions consume clear typed values.
- Keep calculation separate from I/O. Factors and allocation rules do not read environment variables, fetch data, or contact a broker.
- Parse configuration at the application or task entry point. Do not read the clock or environment inside reusable calculations.
- Keep external providers, storage, GUI, and account facts behind narrow interfaces. Add a `Protocol` only when a real replacement boundary exists.
- Preserve `execution_admitted=false`; no code-style change may enable login, prefill, submit, cancel, transfer, paid data, or persistent account writes.

## Modules and changes

Organize modules by the reason they change, not by every step in the execution sequence. Prefer ordinary functions and a small number of concrete objects with composition. Keep the existing Engine/Ledger, BrokerAdapter, snapshot, recovery, and comparison paths when they already satisfy the behavior.

Separate four kinds of changes:

1. Formatting changes only alter presentation.
2. Structural refactors preserve behavior.
3. Bug fixes change incorrect behavior and explain the result difference.
4. Research changes alter an economic question, input, rule, or selection and require a new fixed specification.

Do not mix these categories in a large patch. Do not rewrite the Ledger for appearance. Do not add a parallel account chain or a second task register. Compatibility aliases require a named user, an exit condition, and a removal point.

## Errors, states, and side effects

Expected business outcomes such as no eligible candidate, no rebalance, or a valid hold are explicit results. Source failure, unknown units, conflicting facts, corrupted input, and unknown external results are failures or blocked states; never return an empty collection that looks successful.

Record meaningful `run_id`, `snapshot_id`, `decision_id`, and state transitions at external boundaries. Do not log account secrets, cookies, tokens, or full sensitive payloads. A retry must have a business meaning; unknown external orders are reconciled, never blindly resent.

## Tests and delivery

Tests should let a maintainer understand the behavior from inputs, actions, and expected results. Each changed path needs a normal case, its important boundary, and its real failure behavior. Use fixed slices to compare signal, target, quantity, fees, equity, and account results; allow only documented run metadata to vary.

Use the existing project interpreter and command entry points. Run the smallest relevant tests first, then the affected integration slice. Do not expand test counts or add abstractions without a concrete user or failure path. Keep historical facts, failed experiments, and blocked evidence.

Every implementation handoff records:

- what changed and which business meanings stayed unchanged;
- which abstractions were kept, removed, or deliberately not added;
- the commands and tests actually run;
- the resulting receipt or artifact path;
- the remaining dependency and the next executable step.

Do not change research thresholds, fees, holding periods, PIT cutoffs, stop decisions, strategy specifications, or execution permissions during a style refactor.
