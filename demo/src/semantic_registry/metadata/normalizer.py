from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from src.semantic_registry.metadata.eligible_checker import is_eligible
from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata

logger = logging.getLogger(__name__)

TABLE_KEYS = {
    "table",
    "table_name",
    "name",
    "description",
    "business_description",
    "domain",
    "certified",
    "eligible_for_nl2sql",
    "grain",
    "partition_column",
    "partition",
    "owner",
    "columns",
    "caveats",
    "created_at",
    "pii_reviewed",
    "join_paths",
    "usage_popularity",
}

COLUMN_KEYS = {
    "column",
    "column_name",
    "name",
    "data_type",
    "type",
    "description",
    "is_pii",
    "pii",
    "pii_tag",
    "concept",
    "aggregation",
    "unit",
    "nullable",
    "is_nullable",
    "default_value",
    "column_default",
}


def _warn_unknown(raw: dict[str, Any], known_keys: set[str], object_name: str) -> None:
    for key in sorted(set(raw) - known_keys):
        logger.warning("Unknown metadata field on %s: %s", object_name, key)


def _coalesce(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return default


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _nullable(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().upper() not in {"NO", "FALSE", "0"}
    return _bool(value, default=True)


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Could not parse metadata timestamp: %s", value)
    return None


def normalize_column(raw: dict[str, Any]) -> ColumnMetadata:
    _warn_unknown(raw, COLUMN_KEYS, str(_coalesce(raw, "column_name", "column", "name", default="<unknown column>")))
    column_name = _coalesce(raw, "column_name", "column", "name")
    if not column_name:
        raise ValueError("column metadata is missing column_name")
    pii_value = _coalesce(raw, "is_pii", "pii", "pii_tag", default=False)
    return ColumnMetadata(
        column_name=str(column_name),
        data_type=str(_coalesce(raw, "data_type", "type", default="") or ""),
        description=str(raw.get("description") or ""),
        is_pii=_bool(pii_value, default=False),
        concept=_coalesce(raw, "concept"),
        aggregation=_coalesce(raw, "aggregation"),
        unit=_coalesce(raw, "unit"),
        nullable=_nullable(_coalesce(raw, "nullable", "is_nullable")),
        default_value=_coalesce(raw, "default_value", "column_default"),
    )


def _infer_pii_reviewed(raw: dict[str, Any], raw_columns: list[Any]) -> bool | None:
    if "pii_reviewed" in raw:
        return _bool(raw["pii_reviewed"])
    if not raw_columns:
        return None
    return all(
        isinstance(column, ColumnMetadata) or any(key in column for key in ("is_pii", "pii", "pii_tag"))
        for column in raw_columns
    )


def normalize_table(raw: dict[str, Any]) -> TableMetadata:
    _warn_unknown(raw, TABLE_KEYS, str(_coalesce(raw, "table_name", "table", "name", default="<unknown table>")))
    table_name = _coalesce(raw, "table_name", "table", "name")
    if not table_name:
        raise ValueError("table metadata is missing table_name")
    raw_columns = list(raw.get("columns", []))
    columns = [
        column if isinstance(column, ColumnMetadata) else normalize_column(column)
        for column in raw_columns
        if isinstance(column, (dict, ColumnMetadata))
    ]
    table = TableMetadata(
        table_name=str(table_name),
        description=str(_coalesce(raw, "description", "business_description", default="") or ""),
        domain=_coalesce(raw, "domain"),
        certified=_bool(raw.get("certified")),
        grain=_string_list(raw.get("grain")),
        partition_column=_coalesce(raw, "partition_column", "partition"),
        owner=_coalesce(raw, "owner"),
        columns=columns,
        caveats=_string_list(raw.get("caveats")),
        created_at=_datetime(raw.get("created_at")),
        pii_reviewed=_infer_pii_reviewed(raw, raw_columns),
        join_paths=raw.get("join_paths") or [],
        usage_popularity=float(raw.get("usage_popularity") or 0.0),
    )
    table.eligible_for_nl2sql = is_eligible(table)
    return table


def normalize_all(tables: list[dict[str, Any]]) -> list[TableMetadata]:
    normalized: list[TableMetadata] = []
    for raw in tables:
        if not raw:
            logger.warning("Skipping missing table metadata row")
            continue
        try:
            normalized.append(normalize_table(raw))
        except (ValueError, ValidationError) as exc:
            logger.warning("Skipping invalid table metadata row: %s", exc)
    return normalized


__all__ = ["normalize_all", "normalize_column", "normalize_table"]
