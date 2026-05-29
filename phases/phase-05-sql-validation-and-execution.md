# Phase 5 — SQL Validation and Execution

**Duration:** 2 weeks

**Depends on:** Phase 4 (SQL generation pipeline)

**Design references:** [02-Architecture §11 (SQL validation)](../docs/02-architecture.md#sql-validation-design), [§12 (Query execution)](../docs/02-architecture.md#query-execution-design), [§18 (Security and governance)](../docs/02-architecture.md#security-and-governance)

---

## Overview

Build the SQL validation and query execution layer. This phase adds two layers of validation (static and semantic), integrates SQLGlot for parsing, and enables safe preview execution against the warehouse.

---

## Requirements

### R5.1 — SQLGlot parser integration
- Integrate SQLGlot as the SQL parsing engine.
- Use SQLGlot to parse generated SQL and extract: table names, column names, function calls, JOIN types, subqueries, and aggregations.
- Support the SQL dialect selected in Phase 0 (e.g., Spark, Trino, Snowflake).
- Reject SQL that cannot be parsed.

### R5.2 — Static SQL validator
- Implement static validation checks against the parsed SQL AST:
  - SELECT-only: no INSERT, UPDATE, DELETE, MERGE, DROP, CREATE, ALTER.
  - No stored procedure calls.
  - No external network functions.
  - No SELECT * (explicit column listing required).
  - All tables referenced are in the allowed table list.
  - All columns referenced are in the allowed column list for their table.
  - No unauthorized schemas.
  - No uncontrolled CROSS JOIN.
  - LIMIT clause must be present for preview executions.
  - Partition filter must be present for large tables (configurable row threshold).
- Each check must produce a pass/fail result with a clear error message.
- If any static check fails, the SQL is rejected with no further processing.

### R5.3 — Semantic SQL validator
- Implement semantic validation against the resolved semantic plan:
  - The SQL's metric column matches the resolved metric from the semantic plan.
  - The SQL uses the correct time semantic (the right date column for the query context).
  - The SQL uses only allowed dimensions for the selected metric.
  - The SQL's aggregation function matches the metric definition (SUM for additive metrics, COUNT for count metrics, etc.).
  - The SQL does not accidentally swap related-but-different metrics (e.g., using GMV when net_revenue was resolved).
  - The SQL respects the join graph and fanout rules (no joins that would cause double-counting).
  - The SQL's grain is compatible with the requested output granularity.
- Each check must produce a pass/fail result with a clear explanation.
- Semantic validation failure must block execution and trigger the repair loop.

### R5.4 — Permission checker
- Check that the requesting user has table-level and column-level permissions for all tables and columns in the generated SQL.
- If the user lacks permission for any table or column, reject the SQL and return a permission-denied error.
- The permission checker must integrate with the SSO/user-to-role mapping system.

### R5.5 — Partition filter checker
- For tables with a documented partition column, verify that the SQL includes a filter on that column.
- For large uncertified tables (user-specified threshold, e.g., > 10M rows), enforce a mandatory partition filter.
- If the partition filter is missing and the table is large, reject the SQL with a message specifying the required partition column.

### R5.6 — LIMIT injection
- For preview executions, automatically inject a LIMIT clause into the SQL if one is not already present.
- The default LIMIT should be 100 rows for preview.
- The LIMIT must be injected at the SQL level, not via application-side truncation, to control warehouse resource usage.

### R5.7 — Dry-run / EXPLAIN integration
- Run an EXPLAIN or dry-run of the SQL against the warehouse.
- Parse the EXPLAIN output to estimate: cost, number of partitions scanned, number of rows scanned, estimated execution time.
- Compare the estimate against the configured cost threshold.
- If the estimated cost exceeds the threshold, block execution and inform the user.

### R5.8 — Preview executor
- Execute the validated SQL in preview mode against the warehouse.
- Preview execution uses: the read-only warehouse role, the injected LIMIT, and the pre-configured query timeout.
- Return the preview result as a table (column names + sample rows).
- Log the execution: query_id, user, SQL, tables accessed, columns accessed, cost, execution time, status, timestamp.
- Full execution (beyond preview) must require explicit user approval and is not automated in MVP.

---

## Exit Criteria

- Unsafe SQL (DDL, DML, SELECT *, unauthorized tables) is blocked with clear error messages.
- Semantic mismatches (wrong metric, wrong time dimension) are caught before execution.
- Preview execution works against the warehouse with LIMIT 100.
- All executions are audited with complete log entries.
- Cost guardrails block queries that exceed the configured threshold.
