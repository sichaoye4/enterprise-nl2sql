from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JoinRelationship(StrEnum):
    one_to_one = "one_to_one"
    many_to_one = "many_to_one"
    one_to_many = "one_to_many"
    many_to_many = "many_to_many"


Relationship = JoinRelationship


class FanoutRisk(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class ColumnMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    column_name: str
    data_type: str = ""
    description: str = ""
    is_pii: bool = False
    concept: str | None = None
    aggregation: str | None = None
    unit: str | None = None
    nullable: bool = True
    default_value: str | None = None


class JoinPath(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_table: str
    to_table: str
    relationship: JoinRelationship = JoinRelationship.many_to_one
    join_condition: str
    safe_for_metrics: list[str] = Field(default_factory=list)
    fanout_risk: FanoutRisk = FanoutRisk.low


class TableMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    table_name: str
    description: str = ""
    domain: str | None = None
    certified: bool = False
    eligible_for_nl2sql: bool = False
    grain: list[str] = Field(default_factory=list)
    partition_column: str | None = None
    owner: str | None = None
    columns: list[ColumnMetadata] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    pii_reviewed: bool | None = None
    join_paths: list[JoinPath] = Field(default_factory=list)
    usage_popularity: float = 0.0


class ExampleQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query_text: str
    description: str = ""
    domain: str | None = None


MetadataJson = dict[str, Any]


__all__ = [
    "ColumnMetadata",
    "ExampleQuery",
    "FanoutRisk",
    "JoinPath",
    "JoinRelationship",
    "MetadataJson",
    "Relationship",
    "TableMetadata",
]
