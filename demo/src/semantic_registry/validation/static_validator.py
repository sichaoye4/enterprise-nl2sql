from __future__ import annotations

import os
from collections.abc import Iterable

from pydantic import BaseModel, Field
from sqlglot import exp

from src.semantic_registry.validation.parser import extract_functions, extract_tables, is_select_only, parse_sql


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str


class ValidationResult(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class StaticValidator:
    STORED_PROCEDURE_COMMANDS = {"call", "exec", "execute"}
    EXTERNAL_NETWORK_FUNCTIONS = {
        "http_get",
        "http_post",
        "url",
        "url_decode",
        "url_encode",
        "read_csv",
        "read_json",
        "read_parquet",
        "load_file",
        "openrowset",
    }

    def __init__(
        self,
        *,
        dialect: str | None = None,
        require_limit: bool = True,
        partition_columns: dict[str, str] | None = None,
        partition_row_threshold: int = 10_000_000,
    ) -> None:
        self.dialect = dialect or os.getenv("SQL_DIALECT", "spark")
        self.require_limit = require_limit
        self.partition_columns = partition_columns or {}
        self.partition_row_threshold = partition_row_threshold

    def validate(
        self,
        sql: str,
        allowed_tables: set[str],
        allowed_columns: dict[str, set[str]],
    ) -> ValidationResult:
        checks: list[CheckResult] = []

        try:
            statement = parse_sql(sql, dialect=self.dialect)
        except Exception as exc:
            message = f"SQL parse failed: {exc}"
            return ValidationResult(
                passed=False,
                checks=[CheckResult(name="parse", passed=False, message=message)],
                errors=[message],
            )

        self._add(checks, "parse", True, "SQL parsed successfully.")
        self._add(checks, "select_only", is_select_only(statement), "Only SELECT statements are allowed.")
        self._add(
            checks,
            "no_stored_procedures",
            not self._has_stored_procedure_call(statement),
            "Stored procedure calls are not allowed.",
        )
        self._add(
            checks,
            "no_external_network_functions",
            not self._uses_external_network_function(statement),
            "External network and file access functions are not allowed.",
        )
        self._add(checks, "no_select_star", not self._has_select_star(statement), "Explicit columns are required.")

        parsed_tables = extract_tables(statement)
        self._add(
            checks,
            "allowed_tables",
            self._tables_allowed(parsed_tables, allowed_tables),
            "All referenced tables must be allowed.",
        )
        self._add(
            checks,
            "allowed_columns",
            self._columns_allowed(statement, allowed_tables, allowed_columns),
            "All referenced columns must be allowed for their table.",
        )
        self._add(
            checks,
            "authorized_schemas",
            self._schemas_allowed(parsed_tables, allowed_tables),
            "All referenced schemas must be authorized.",
        )
        self._add(
            checks,
            "no_uncontrolled_cross_join",
            not self._has_uncontrolled_cross_join(statement),
            "Uncontrolled CROSS JOIN is not allowed.",
        )
        self._add(
            checks,
            "limit_present",
            (not self.require_limit) or bool(statement.args.get("limit")),
            "Preview queries must include a LIMIT clause.",
        )
        self._add(
            checks,
            "partition_filter_present",
            self._partition_filters_present(statement, parsed_tables),
            "Partition filters are required for configured partitioned tables.",
        )

        errors = [check.message for check in checks if not check.passed]
        return ValidationResult(passed=not errors, checks=checks, errors=errors)

    def _add(self, checks: list[CheckResult], name: str, passed: bool, message: str) -> None:
        checks.append(CheckResult(name=name, passed=passed, message=message if not passed else "Passed."))

    def _has_stored_procedure_call(self, statement: exp.Expression) -> bool:
        if isinstance(statement, exp.Command):
            command = str(statement.this or "").lower()
            return command in self.STORED_PROCEDURE_COMMANDS
        return False

    def _uses_external_network_function(self, statement: exp.Expression) -> bool:
        return bool(set(extract_functions(statement)) & self.EXTERNAL_NETWORK_FUNCTIONS)

    def _has_select_star(self, statement: exp.Expression) -> bool:
        return any(True for _ in statement.find_all(exp.Star))

    def _tables_allowed(self, parsed_tables: list[str], allowed_tables: set[str]) -> bool:
        return all(self._table_is_allowed(table, allowed_tables) for table in parsed_tables)

    def _table_is_allowed(self, table: str, allowed_tables: set[str]) -> bool:
        return table in allowed_tables or self._unqualified(table) in allowed_tables

    def _schemas_allowed(self, parsed_tables: list[str], allowed_tables: set[str]) -> bool:
        allowed_schemas = {table.rsplit(".", 1)[0] for table in allowed_tables if "." in table}
        if not allowed_schemas:
            return True
        for table in parsed_tables:
            if "." in table and table.rsplit(".", 1)[0] not in allowed_schemas:
                return False
        return True

    def _columns_allowed(
        self,
        statement: exp.Expression,
        allowed_tables: set[str],
        allowed_columns: dict[str, set[str]],
    ) -> bool:
        alias_to_table = self._alias_to_table(statement)
        parsed_tables = list(alias_to_table.values()) or extract_tables(statement)
        select_aliases = self._select_aliases(statement)

        for column in statement.find_all(exp.Column):
            column_name = column.name
            if not column.table and column_name in select_aliases:
                continue
            candidate_tables = self._candidate_tables(column.table, parsed_tables, alias_to_table)
            if not candidate_tables:
                return False
            if not any(self._column_allowed_for_table(column_name, table, allowed_columns) for table in candidate_tables):
                return False
        return True

    def _alias_to_table(self, statement: exp.Expression) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            name = ".".join(str(part) for part in (table.catalog, table.db, table.name) if part)
            aliases[table.alias_or_name] = name
            aliases[table.name] = name
        return aliases

    def _candidate_tables(self, qualifier: str, parsed_tables: list[str], alias_to_table: dict[str, str]) -> list[str]:
        if qualifier:
            return [alias_to_table.get(qualifier, qualifier)]
        return parsed_tables

    def _column_allowed_for_table(self, column: str, table: str, allowed_columns: dict[str, set[str]]) -> bool:
        return column in allowed_columns.get(table, set()) or column in allowed_columns.get(self._unqualified(table), set())

    def _select_aliases(self, statement: exp.Expression) -> set[str]:
        return {alias.alias for alias in statement.find_all(exp.Alias) if alias.alias}

    def _has_uncontrolled_cross_join(self, statement: exp.Expression) -> bool:
        for join in statement.find_all(exp.Join):
            if str(join.args.get("kind") or "").lower() == "cross":
                return True
        return False

    def _partition_filters_present(self, statement: exp.Expression, parsed_tables: Iterable[str]) -> bool:
        if not self.partition_columns:
            return True
        where = statement.args.get("where")
        if where is None:
            return False
        filtered_columns = {column.name for column in where.find_all(exp.Column)}
        for table in parsed_tables:
            partition_column = self.partition_columns.get(table) or self.partition_columns.get(self._unqualified(table))
            if partition_column and partition_column not in filtered_columns:
                return False
        return True

    def _unqualified(self, table: str) -> str:
        return table.rsplit(".", 1)[-1]
