"""Explicit financial-factor definitions; no expression evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PURPOSES = ("ACTIVE", "DIAGNOSTIC", "PROPOSED")


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    definition_version: str
    purpose: str
    input_fields: tuple[str, ...]
    period_rule: str
    dependency_cutoff_rule: str
    missing_policy: str
    normalization_version: str
    allowed_consumers: tuple[str, ...]

    def __post_init__(self):
        for field in ("factor_id", "definition_version", "period_rule", "dependency_cutoff_rule",
                      "missing_policy", "normalization_version"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} is required")
        if self.purpose not in PURPOSES:
            raise ValueError("unknown factor purpose")
        if not self.input_fields or len(set(self.input_fields)) != len(self.input_fields):
            raise ValueError("factor input_fields must be non-empty and unique")
        if not self.allowed_consumers or len(set(self.allowed_consumers)) != len(self.allowed_consumers):
            raise ValueError("factor allowed_consumers must be non-empty and unique")

    def to_dict(self) -> dict[str, Any]:
        return {"factor_id": self.factor_id, "definition_version": self.definition_version,
                "purpose": self.purpose, "input_fields": list(self.input_fields),
                "period_rule": self.period_rule, "dependency_cutoff_rule": self.dependency_cutoff_rule,
                "missing_policy": self.missing_policy, "normalization_version": self.normalization_version,
                "allowed_consumers": list(self.allowed_consumers)}


F01 = FactorDefinition(
    factor_id="F01",
    definition_version="f01_asset_normalized_surprise_v1",
    purpose="ACTIVE",
    input_fields=("current_single_quarter_parent_net_profit", "prior_comparable_single_quarter_parent_net_profit",
                  "event_pre_assets"),
    period_rule="same_scope_currency_unit_comparable_single_quarter; cumulative_subtraction_explicit",
    dependency_cutoff_rule="all_facts_strictly_proven_before_event_lower_bound",
    missing_policy="preserve_unknown_and_return_non_ready_status",
    normalization_version="decimal_source_unit_preserved_v1",
    allowed_consumers=("f01_financial_factor", "f01_signal", "f01_account_replay"),
)


EXPLANATION_FACTORS = {
    "cash_support": FactorDefinition(
        "cash_support", "diagnostic_cash_support_v1", "DIAGNOSTIC",
        ("operating_cashflow", "parent_net_profit"), "same_report_period_and_scope",
        "max_dependency_available_at", "preserve_unknown", "decimal_source_unit_preserved_v1",
        ("financial_diagnostics",)),
    "gross_margin": FactorDefinition(
        "gross_margin", "diagnostic_gross_margin_v1", "DIAGNOSTIC",
        ("revenue", "operating_cost"), "same_report_period_and_scope",
        "max_dependency_available_at", "preserve_unknown", "decimal_source_unit_preserved_v1",
        ("financial_diagnostics",)),
}


REGISTRY = {F01.factor_id: F01, **EXPLANATION_FACTORS}


def get_definition(factor_id: str, *, consumer: str | None = None) -> FactorDefinition:
    if not isinstance(factor_id, str) or factor_id not in REGISTRY:
        raise KeyError(f"Unknown registered financial factor: {factor_id}")
    definition = REGISTRY[factor_id]
    if consumer is not None and consumer not in definition.allowed_consumers:
        raise PermissionError(f"Factor {factor_id} is not allowed for consumer {consumer}")
    return definition


__all__ = ["EXPLANATION_FACTORS", "F01", "FactorDefinition", "REGISTRY", "get_definition"]
