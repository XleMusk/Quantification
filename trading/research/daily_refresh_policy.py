"""Data-driven command policy for the existing daily refresh routes.

The refresh supervisor is responsible for orchestration and persistence.  This
module owns the smaller, stable boundary that turns a saved logical route into
the exact local command that is allowed to run.  Keeping the policy table here
prevents route-specific safety rules from becoming an ever-growing conditional
chain in the supervisor.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_FORBIDDEN_FLAGS = frozenset({"--save-next", "--refit", "--train", "--submit", "--live"})

ALLOWED = frozenset({
    "baostock-bulk-extras", "leader-close-publication", "leader-recent-source-preview",
    "leader-eight-type-forward-register", "leader-eight-type-split-forward",
    "leader-environment", "leader-identity", "leader-identity-episodes", "leader-eight-type-inputs",
    "leader-eight-type-paper", "leader-eight-type-forward", "leader-published-forward",
    "recent-source-qlib-provider", "recent-source-alpha158", "recent-source-alpha158-target-publication",
    "recent-source-alpha158-forward-run", "shortline-native-preopen-forward",
    "leader-dated-catchup-inputs", "leader-dated-catchup-paper",
    "leader-dated-identity", "leader-dated-catchup-forward", "leader-eight-type-residual",
    "leader-known-identity-paper", "leader-entry-confirmation-inputs", "leader-entry-comparison",
    "leader-entry-comparison-forward", "seat-append-source-audit", "seat-append-dated-inputs",
    "seat-append-forward", "seat-hypothesis-dated-inputs", "seat-hypothesis-dated-forward", "seat-daily",
    "seat-collect", "seat-disclosure-catalog", "leader-type-router-paper", "leader-type-router-forward",
    "native-intraday-paper", "leader-type-router-runtime-repair",
})


def _values(argv: Sequence[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == flag]


Validator = Callable[[list[str]], None]


@dataclass(frozen=True)
class RoutePolicy:
    """One route's subcommand and argument constraints."""

    subcommands: tuple[tuple[str, ...], ...] = ()
    validate: Validator | None = None
    runtime_module: str | None = None

    def check(self, argv: list[str]) -> None:
        if self.subcommands and tuple(argv[1:2]) not in self.subcommands:
            raise ValueError(self._subcommand_error(argv[0]))
        if self.validate is not None:
            self.validate(argv)

    @staticmethod
    def _subcommand_error(route: str) -> str:
        messages = {
            "leader-eight-type-residual": "The attached residual method may only publish or consume its existing registration",
            "leader-known-identity-paper": "The attached known-identity method may only publish or consume its existing registration",
            "leader-entry-comparison": "The attached entry comparison may only publish its existing registration",
            "leader-type-router-paper": "The attached router may only publish or consume its existing registration",
            "leader-type-router-forward": "The attached router may only publish or consume its existing registration",
            "seat-daily": "The seat helper may only project the saved exact source audit",
            "seat-disclosure-catalog": "The daily source may only build or append its exact saved disclosure batch",
            "leader-type-router-runtime-repair": "The daily supervisor may only publish/consume an existing explicit runtime repair",
        }
        return messages[route]


def _validate_seat_collect(argv: list[str]) -> None:
    if ("--execute" not in argv or _values(argv, "--max-pages") != ["3"]
            or len(_values(argv, "--start")) != 1
            or _values(argv, "--start") != _values(argv, "--end")):
        raise ValueError("Daily disclosure collection requires one exact day and the fixed three-page bound")


def _validate_native_intraday(argv: list[str]) -> None:
    if (_values(argv, "--phase") != ["closing"]
            or any(len(_values(argv, flag)) != 1
                   for flag in ("--run-dir", "--plan", "--prior-package", "--quote"))):
        raise ValueError("Daily refresh may only close an existing explicit native PAPER session")


ROUTE_POLICIES: dict[str, RoutePolicy] = {
    "leader-eight-type-residual": RoutePolicy(subcommands=(("publish",), ("forward",))),
    "leader-known-identity-paper": RoutePolicy(subcommands=(("publish",), ("forward",))),
    "leader-entry-comparison": RoutePolicy(subcommands=(("publish",),)),
    "leader-type-router-paper": RoutePolicy(subcommands=(("publish",),)),
    "leader-type-router-forward": RoutePolicy(subcommands=(("run",),)),
    "seat-daily": RoutePolicy(subcommands=(("project-static-audit",),)),
    "seat-disclosure-catalog": RoutePolicy(subcommands=(("batch",), ("append",))),
    "seat-collect": RoutePolicy(validate=_validate_seat_collect),
    "native-intraday-paper": RoutePolicy(validate=_validate_native_intraday),
    "leader-type-router-runtime-repair": RoutePolicy(
        subcommands=(("publish",), ("forward",)),
        runtime_module="trading.research.leader_type_router_runtime_repair",
    ),
}


def _normalize(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    committee = next((i for i, token in enumerate(values)
                      if Path(token).name == "run_committee.py"), None)
    return values[committee + 1:] if committee is not None else values


def command(argv: Sequence[str], *, root: Path) -> list[str]:
    """Validate and materialize one existing, non-live daily route command."""

    normalized = _normalize(argv)
    if (not normalized or normalized[0] not in ALLOWED
            or any(value in normalized for value in _FORBIDDEN_FLAGS)):
        raise ValueError("Only the fixed existing research production commands are supported")
    policy = ROUTE_POLICIES.get(normalized[0])
    if policy is not None:
        policy.check(normalized)
    if policy is not None and policy.runtime_module is not None:
        return [str(root / "env/python.exe"), "-B", "-X", "utf8", "-m",
                policy.runtime_module, *normalized[1:]]
    return [str(root / "env/python.exe"), "-B", "-X", "utf8",
            str(root / "run_committee.py"), *normalized]

