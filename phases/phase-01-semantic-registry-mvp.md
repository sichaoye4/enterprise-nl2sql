# Phase 1 — Semantic Registry MVP

**Duration:** 2 weeks

**Depends on:** Phase 0 (domain scope, certified table list, SQL dialect, ownership model)

**Design references:** [02-Architecture §6 (Semantic layer design)](../docs/02-architecture.md#semantic-layer-design)

---

## Overview

Build the semantic registry — the heart of the NL2SQL system. This phase creates the data model, YAML definitions, and API for managing business terms, concepts, metrics, dimensions, entities, and their physical mappings.

---

## Requirements

### R1.1 — Semantic data model
- Define a Postgres schema for the following semantic entities: `semantic_terms`, `semantic_concepts`, `semantic_metrics`, `semantic_dimensions`, `semantic_entities`, `semantic_physical_mappings`, `semantic_join_paths`.
- Each entity must support versioning and status tracking (draft/reviewed/certified/deprecated).
- Each entity must store metadata: owner, domain, description, created_at, updated_at.
- The model must support the full attribute structure shown in the architecture document (e.g., for metrics: type, measure, aggregation, unit, default_time_dimension, allowed_dimensions; for terms: synonyms, candidate_concepts, default_concept_by_domain, ambiguity_level).

### R1.2 — YAML definition format
- Design a Git-managed YAML file format for all semantic entities.
- Each YAML file represents one entity (one term per file, one metric per file, etc.).
- Directory layout: `semantic/terms/`, `semantic/concepts/`, `semantic/metrics/`, `semantic/dimensions/`, `semantic/entities/`, `semantic/join_paths/`.
- YAML files must be human-readable, reviewable via pull requests, and machine-parseable.
- Provide a CLI tool or script that validates YAML files against the schema before merging.

### R1.3 — YAML-to-Postgres sync
- Build a sync mechanism that reads YAML definitions from the semantic/ directory and writes/updates them to Postgres.
- The sync must be idempotent: re-running it produces the same result.
- The sync must detect and report validation errors (e.g., missing required fields, broken references to non-existent entities).
- The sync must support incremental updates: only changed files are re-processed.

### R1.4 — Semantic registry API
- Build a REST API (FastAPI) for the semantic registry service.
- Endpoints:
  - `GET /api/v1/terms` — list terms, filterable by domain, status
  - `GET /api/v1/terms/{term_id}` — get term detail
  - `GET /api/v1/concepts` — list concepts
  - `GET /api/v1/metrics` — list metrics
  - `GET /api/v1/dimensions` — list dimensions
  - `GET /api/v1/entities` — list entities
  - `GET /api/v1/join-paths` — list join paths
  - `POST /api/v1/sync` — trigger YAML-to-Postgres sync
  - `GET /api/v1/status` — registry health and summary (count of terms, metrics, etc.)
- All list endpoints must support pagination, domain filtering, and status filtering.
- All endpoints must return structured JSON responses with consistent error formats.

### R1.5 — Initial semantic content
- Populate the registry with 30–50 business terms for the selected MVP domains.
- Define 20–40 certified metrics with complete physical mappings.
- Define 20–50 common dimensions with physical table/column mappings.
- Define core entity definitions (e.g., user, order, product, campaign, channel).
- Link all physical mappings to the certified ADS tables identified in Phase 0.
- Every term with high ambiguity must have a `default_concept_by_domain` rule.

### R1.6 — Join path definitions
- Define join paths between certified tables.
- Each join path must specify: from_table, to_table, relationship type (one-to-one, many-to-one, one-to-many), join condition, safe_for_metrics list, fanout_risk level.
- Document known fanout risks and how the pipeline should avoid double-counting.

---

## Exit Criteria

- 30–50 business terms defined and synced to Postgres.
- 20–40 metrics certified with physical mappings linked to ADS tables.
- 20–50 dimensions certified with table/column mappings.
- Core entity definitions created.
- Semantic registry API is deployed and all endpoints return correct data.
- YAML validation + sync process is functional and tested.
