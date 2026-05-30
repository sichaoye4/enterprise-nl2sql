from __future__ import annotations

import logging

from src.semantic_registry.metadata.normalizer import normalize_all, normalize_column, normalize_table


def test_normalize_column_accepts_information_schema_fields() -> None:
    column = normalize_column(
        {
            "column_name": "amount",
            "data_type": "numeric",
            "description": "Order amount.",
            "is_pii": False,
            "is_nullable": "NO",
            "column_default": "0",
        }
    )

    assert column.column_name == "amount"
    assert column.data_type == "numeric"
    assert column.nullable is False
    assert column.default_value == "0"


def test_normalize_table_computes_eligibility_and_warns_on_unknown_fields(caplog) -> None:
    caplog.set_level(logging.WARNING)

    table = normalize_table(
        {
            "table_name": "public.orders",
            "description": "Orders fact table.",
            "domain": "sales",
            "certified": True,
            "grain": "order_id",
            "partition_column": "order_date",
            "owner": "analytics",
            "columns": [{"column_name": "order_id", "is_pii": False}],
            "unexpected": "ignored",
        }
    )

    assert table.eligible_for_nl2sql is True
    assert table.grain == ["order_id"]
    assert "Unknown metadata field" in caplog.text


def test_normalize_all_skips_missing_table_rows(caplog) -> None:
    caplog.set_level(logging.WARNING)

    tables = normalize_all([{}, {"description": "missing name"}])

    assert tables == []
    assert "Skipping missing table metadata row" in caplog.text
    assert "Skipping invalid table metadata row" in caplog.text
