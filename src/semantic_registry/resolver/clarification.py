from __future__ import annotations

from pydantic import BaseModel, Field

from src.semantic_registry.resolver.ambiguity import Ambiguity, AmbiguityType
from src.semantic_registry.resolver.domain import DomainResult


class ClarificationOption(BaseModel):
    value: str
    label: str
    description: str


class ClarificationResponse(BaseModel):
    needs_clarification: bool
    message: str
    options: list[ClarificationOption] = Field(default_factory=list)


class ClarificationBuilder:
    def build(self, ambiguities: list[Ambiguity], domain_result: DomainResult) -> ClarificationResponse:
        ambiguity = self._primary_ambiguity(ambiguities, domain_result)
        if ambiguity is None:
            return ClarificationResponse(needs_clarification=False, message="", options=[])

        options = [
            ClarificationOption(
                value=option,
                label=self._label(option),
                description=self._description(option, ambiguity.type),
            )
            for option in ambiguity.options
        ]
        return ClarificationResponse(
            needs_clarification=True,
            message=self._message(ambiguity, options),
            options=options,
        )

    def _primary_ambiguity(self, ambiguities: list[Ambiguity], domain_result: DomainResult) -> Ambiguity | None:
        if domain_result.requires_clarification and domain_result.candidates:
            return Ambiguity(
                type=AmbiguityType.domain,
                term="domain",
                options=domain_result.candidates,
                question="Which business area should I use?",
            )
        return ambiguities[0] if ambiguities else None

    def _message(self, ambiguity: Ambiguity, options: list[ClarificationOption]) -> str:
        labels = [option.label for option in options]
        if not labels:
            return ambiguity.question
        choices = self._join_labels(labels)
        if ambiguity.type == AmbiguityType.domain:
            return f"Which business area should I use: {choices}?"
        if ambiguity.type == AmbiguityType.time:
            return f"Which time convention should I use: {choices}?"
        if ambiguity.type == AmbiguityType.dimension:
            return f"Which dimension should I group '{ambiguity.term}' by: {choices}?"
        if ambiguity.type == AmbiguityType.metric:
            return f"Which metric should I use for '{ambiguity.term}': {choices}?"
        return f"Do you mean {choices} when you say '{ambiguity.term}'?"

    def _join_labels(self, labels: list[str]) -> str:
        quoted = [f"'{label}'" for label in labels]
        if len(quoted) <= 2:
            return " or ".join(quoted)
        return ", ".join(quoted[:-1]) + f", or {quoted[-1]}"

    def _label(self, value: str) -> str:
        overrides = {
            "gmv": "GMV",
            "gmv_concept": "Gross Merchandise Value",
            "paid_gmv": "Paid GMV",
            "net_revenue": "Net Revenue",
            "calendar_month": "Calendar Month",
            "fiscal_month": "Fiscal Month",
            "trailing_30_days": "Trailing 30 Days",
            "calendar_quarter": "Calendar Quarter",
            "fiscal_quarter": "Fiscal Quarter",
        }
        if value in overrides:
            return overrides[value]
        return value.replace("_", " ").title()

    def _description(self, value: str, ambiguity_type: AmbiguityType) -> str:
        if ambiguity_type == AmbiguityType.domain:
            return f"Use the {self._label(value)} business context."
        if ambiguity_type == AmbiguityType.time:
            return f"Interpret the time range as {self._label(value).lower()}."
        return f"Resolve to {self._label(value)}."
