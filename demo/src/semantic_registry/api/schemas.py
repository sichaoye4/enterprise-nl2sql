from __future__ import annotations

from pydantic import BaseModel, Field


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class ListResponse(BaseModel):
    data: list[dict]
    pagination: Pagination


class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class SyncRequest(BaseModel):
    dry_run: bool = False


class StatusResponse(BaseModel):
    db_connected: bool
    counts: dict[str, int] = Field(default_factory=dict)
