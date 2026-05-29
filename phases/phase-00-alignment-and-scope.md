# Phase 0 — Alignment and Scope

**Duration:** 1 week

**Depends on:** Nothing (project bootstrap phase)

**Design references:** [01-Product Design](../docs/01-product-design.md), [02-Architecture](../docs/02-architecture.md)

---

## Overview

This phase establishes the foundational decisions, domain scope, and governance framework that all subsequent phases will follow. No code is produced in this phase — only configuration, documentation, and decisions.

---

## Requirements

### R0.1 — Project scaffolding
- Initialize the monorepo following the layout defined in the architecture document (apps/, services/, packages/, semantic/, eval/, infra/, docs/).
- Set up Python package management (pyproject.toml per service, shared dev dependencies).
- Configure linters, formatters, and pre-commit hooks for both Python and frontend code.
- Set up CI pipeline with basic build and lint checks for all services.
- Create a shared `common` package for types, schemas, and constants that all services will import.

### R0.2 — MVP domain selection
- Select 1–3 business domains to target for the MVP (recommended: commerce/order/revenue, user growth, marketing/campaign performance).
- Document the rationale for each selected domain.
- Define the scope boundary for each domain: which business questions are "in scope" and which are "out of scope".

### R0.3 — Certified table inventory
- Identify 20–50 certified analytical tables (ADS/mart/serving-layer) across the selected domains.
- For each table, document: name, description, domain, grain, partition column, owner, eligible_for_nl2sql flag.
- Tag each table as certified/uncertified based on completion of required metadata.

### R0.4 — SQL dialect decision
- Select the warehouse SQL dialect to target for MVP (e.g., Spark SQL, Trino, Snowflake, BigQuery, Databricks).
- Document the dialect-specific SQL generation rules that the pipeline must follow.
- Note any dialect-specific syntax quirks that affect SQL validation or generation.

### R0.5 — Execution policy
- Define the warehouse execution policy: which roles, cost thresholds, query timeouts, row limits, and partition scan limits apply.
- Define the execution mode hierarchy: dry_run → explain → preview (LIMIT 100) → full_run (user-confirmed).
- Document how each execution mode is enforced at the warehouse level.

### R0.6 — Security and governance policy
- Define table-level and column-level permission model.
- Define PII classification criteria and how PII-protected columns are handled.
- Define the read-only warehouse role and service-specific DB role.
- Document the audit log schema: which fields must be captured for every query execution.

### R0.7 — Semantic layer ownership model
- Define who owns metric definitions, table metadata, and semantic mappings.
- Establish the review and certification workflow for new metrics and terms.
- Document how YAML files in the semantic/ directory are reviewed, approved, and merged.

### R0.8 — Evaluation methodology
- Define the two-level evaluation framework (semantic resolution eval + SQL generation eval).
- Define the metrics for each level (as specified in the product design document).
- Set MVP success targets: correct table retrieval top-5 > 85%, SQL parse > 95%, execution success > 85%, correct metric selection > 75%, dangerous SQL blocked 100%, positive user feedback > 70%.

### R0.9 — Technical stack decision
- Confirm the backend stack: FastAPI + Postgres + pgvector for MVP.
- Confirm the frontend stack: React/Next.js + Monaco Editor + data grid component.
- Confirm the observability stack: OpenTelemetry for traces/metrics/logs.

---

## Exit Criteria

- All requirements in R0.1 through R0.9 are fulfilled with documented decisions.
- The monorepo is initialized and CI passes.
- The certified table inventory and domain scope are documented and approved by stakeholders.
- Security and execution policies are documented and approved by the governance team.
