from __future__ import annotations

from src.semantic_registry.metadata.eligible_checker import eligibility_reasons, is_eligible
from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata


def test_table_is_eligible_when_required_metadata_is_present() -> None:
    table = TableMetadata(
        table_name="public.orders",
        description="Business description.",
        certified=True,
        grain=["order_id"],
        partition_column="order_date",
        owner="analytics",
        columns=[ColumnMetadata(column_name="order_id", is_pii=False)],
    )

    assert is_eligible(table)
    assert eligibility_reasons(table) == []


def test_eligibility_reasons_reports_missing_requirements() -> None:
    table = TableMetadata(table_name="public.orders", description=" ", columns=[])

    reasons = eligibility_reasons(table)

    assert "certified must be true" in reasons
    assert "owner_exists must be true" in reasons
    assert "grain_documented must be true" in reasons
    assert "partition_documented must be true" in reasons
    assert "pii_reviewed must be true" in reasons
    assert "business_description_exists must be true" in reasons
    assert not is_eligible(table)
