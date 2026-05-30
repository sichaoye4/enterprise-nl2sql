from __future__ import annotations

from src.semantic_registry.metadata.models import TableMetadata
from src.semantic_registry.validation.partition_checker import PartitionFilterChecker


def test_pass_when_partition_filter_present() -> None:
    result = PartitionFilterChecker().check(
        "SELECT paid_gmv_amt FROM orders WHERE payment_dt >= '2026-01-01'",
        [TableMetadata(table_name="orders", partition_column="payment_dt")],
    )

    assert result.passed is True


def test_fail_when_partition_filter_missing_for_partitioned_table() -> None:
    result = PartitionFilterChecker().check(
        "SELECT paid_gmv_amt FROM orders",
        [TableMetadata(table_name="orders", partition_column="payment_dt")],
    )

    assert result.passed is False
    assert result.missing_filters == ["orders.payment_dt"]
