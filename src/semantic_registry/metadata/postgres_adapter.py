from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.normalizer import normalize_column, normalize_table
from src.semantic_registry.metadata.provider import MetadataProvider


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"unsafe SQL identifier: {value}")
    return value


class PostgresMetadataProvider(MetadataProvider):
    def __init__(
        self,
        connection: Any,
        *,
        warehouse_schema: str = "warehouse_catalog",
        metadata_table: str = "warehouse_metadata",
    ) -> None:
        self.connection = connection
        self.warehouse_schema = _safe_identifier(warehouse_schema)
        self.metadata_table = _safe_identifier(metadata_table)

    @property
    def metadata_relation(self) -> str:
        return f"{self.warehouse_schema}.{self.metadata_table}"

    def _execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.connection.execute(text(sql), params or {})
        if hasattr(result, "mappings"):
            return [dict(row) for row in result.mappings().all()]
        if isinstance(result, Iterable):
            rows = result
        elif hasattr(result, "all"):
            rows = result.all()
        else:
            rows = []
        return [dict(getattr(row, "_mapping", row)) for row in rows]

    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        rows = self._execute(
            f"""
            SELECT
                t.table_schema || '.' || t.table_name AS table_name,
                wm.description,
                wm.domain,
                COALESCE(wm.certified, false) AS certified,
                wm.eligible_for_nl2sql,
                wm.grain,
                wm.partition_column,
                wm.owner,
                wm.caveats,
                wm.created_at,
                wm.pii_reviewed,
                wm.usage_popularity
            FROM information_schema.tables AS t
            LEFT JOIN {self.metadata_relation} AS wm
                ON wm.object_type = 'table'
               AND wm.table_schema = t.table_schema
               AND wm.table_name = t.table_name
            WHERE t.table_type = 'BASE TABLE'
              AND (:domain IS NULL OR wm.domain = :domain)
              AND (
                    :query = ''
                 OR t.table_name ILIKE '%' || :query || '%'
                 OR COALESCE(wm.description, '') ILIKE '%' || :query || '%'
              )
            ORDER BY COALESCE(wm.certified, false) DESC, t.table_schema, t.table_name
            """,
            {"query": query or "", "domain": domain},
        )
        tables = [normalize_table({**row, "columns": self.get_columns(str(row["table_name"]))}) for row in rows]
        for table in tables:
            table.join_paths = self.get_join_paths([table.table_name])
        return tables

    def get_table(self, table_name: str) -> TableMetadata | None:
        if "." in table_name:
            schema_name, short_name = table_name.split(".", 1)
        else:
            schema_name, short_name = "public", table_name
        rows = self._execute(
            f"""
            SELECT
                t.table_schema || '.' || t.table_name AS table_name,
                wm.description,
                wm.domain,
                COALESCE(wm.certified, false) AS certified,
                wm.eligible_for_nl2sql,
                wm.grain,
                wm.partition_column,
                wm.owner,
                wm.caveats,
                wm.created_at,
                wm.pii_reviewed,
                wm.usage_popularity
            FROM information_schema.tables AS t
            LEFT JOIN {self.metadata_relation} AS wm
                ON wm.object_type = 'table'
               AND wm.table_schema = t.table_schema
               AND wm.table_name = t.table_name
            WHERE t.table_schema = :schema_name
              AND t.table_name = :table_name
            LIMIT 1
            """,
            {"schema_name": schema_name, "table_name": short_name},
        )
        if not rows:
            return None
        table = normalize_table({**rows[0], "columns": self.get_columns(table_name)})
        table.join_paths = self.get_join_paths([table.table_name])
        return table

    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        if "." in table_name:
            schema_name, short_name = table_name.split(".", 1)
        else:
            schema_name, short_name = "public", table_name
        rows = self._execute(
            f"""
            SELECT
                c.column_name,
                c.data_type,
                COALESCE(wm.description, '') AS description,
                COALESCE(wm.is_pii, false) AS is_pii,
                wm.concept,
                wm.aggregation,
                wm.unit,
                c.is_nullable,
                c.column_default
            FROM information_schema.columns AS c
            LEFT JOIN {self.metadata_relation} AS wm
                ON wm.object_type = 'column'
               AND wm.table_schema = c.table_schema
               AND wm.table_name = c.table_name
               AND wm.column_name = c.column_name
            WHERE c.table_schema = :schema_name
              AND c.table_name = :table_name
            ORDER BY c.ordinal_position
            """,
            {"schema_name": schema_name, "table_name": short_name},
        )
        return [normalize_column(row) for row in rows]

    def get_join_paths(self, tables: list[str]) -> list[JoinPath]:
        if not tables:
            return []
        table_names = sorted({*tables, *(table.split(".", 1)[1] if "." in table else table for table in tables)})
        rows = self._execute(
            f"""
            SELECT
                from_table,
                to_table,
                relationship,
                join_condition,
                safe_for_metrics,
                fanout_risk
            FROM {self.metadata_relation}
            WHERE object_type = 'join_path'
              AND (from_table = ANY(:tables) OR to_table = ANY(:tables))
            """,
            {"tables": table_names},
        )
        return [JoinPath.model_validate(row) for row in rows]

    def get_example_queries(self, query: str) -> list[ExampleQuery]:
        rows = self._execute(
            f"""
            SELECT query_text, description, domain
            FROM {self.metadata_relation}
            WHERE object_type = 'example_query'
              AND (:query = '' OR query_text ILIKE '%' || :query || '%' OR description ILIKE '%' || :query || '%')
            ORDER BY created_at DESC NULLS LAST
            LIMIT 25
            """,
            {"query": query or ""},
        )
        return [ExampleQuery.model_validate(row) for row in rows]


__all__ = ["PostgresMetadataProvider"]
