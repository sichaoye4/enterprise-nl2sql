from __future__ import annotations

from pydantic import BaseModel, Field


class PermissionResult(BaseModel):
    granted: bool
    denied_tables: list[str] = Field(default_factory=list)
    denied_columns: list[str] = Field(default_factory=list)
    message: str | None = None


class PermissionChecker:
    def check_permissions(
        self,
        user: str,
        sql: str,
        parsed_tables: list[str],
        parsed_columns: list[str],
    ) -> PermissionResult:
        raise NotImplementedError


class AllowAllPermissionChecker(PermissionChecker):
    def check_permissions(
        self,
        user: str,
        sql: str,
        parsed_tables: list[str],
        parsed_columns: list[str],
    ) -> PermissionResult:
        return PermissionResult(granted=True, message="Permission granted.")


class RoleBasedPermissionChecker(PermissionChecker):
    def __init__(
        self,
        *,
        user_roles: dict[str, set[str]] | None = None,
        role_tables: dict[str, set[str]] | None = None,
        role_columns: dict[str, set[str]] | None = None,
    ) -> None:
        self.user_roles = user_roles or {}
        self.role_tables = role_tables or {}
        self.role_columns = role_columns or {}

    def check_permissions(
        self,
        user: str,
        sql: str,
        parsed_tables: list[str],
        parsed_columns: list[str],
    ) -> PermissionResult:
        roles = self.user_roles.get(user, set())
        allowed_tables = set().union(*(self.role_tables.get(role, set()) for role in roles)) if roles else set()
        allowed_columns = set().union(*(self.role_columns.get(role, set()) for role in roles)) if roles else set()
        denied_tables = [table for table in parsed_tables if table not in allowed_tables]
        denied_columns = [column for column in parsed_columns if column not in allowed_columns]
        granted = not denied_tables and not denied_columns
        return PermissionResult(
            granted=granted,
            denied_tables=denied_tables,
            denied_columns=denied_columns,
            message=None if granted else "Permission denied by role policy.",
        )
