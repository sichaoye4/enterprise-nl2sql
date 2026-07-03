from __future__ import annotations

from src.semantic_registry.metadata.models import TableMetadata


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def _pii_reviewed(table: TableMetadata) -> bool:
    if table.pii_reviewed is not None:
        return table.pii_reviewed
    return bool(table.columns) and all(isinstance(column.is_pii, bool) for column in table.columns)


def eligibility_reasons(table: TableMetadata) -> list[str]:
    reasons: list[str] = []
    if not table.certified:
        reasons.append("certified must be true")
    if not _has_text(table.owner):
        reasons.append("owner_exists must be true")
    if not table.grain:
        reasons.append("grain_documented must be true")
    if not _has_text(table.partition_column):
        reasons.append("partition_documented must be true")
    if not _pii_reviewed(table):
        reasons.append("pii_reviewed must be true")
    if not _has_text(table.description):
        reasons.append("business_description_exists must be true")
    return reasons


def is_eligible(table: TableMetadata) -> bool:
    return eligibility_reasons(table) == []


__all__ = ["eligibility_reasons", "is_eligible"]
