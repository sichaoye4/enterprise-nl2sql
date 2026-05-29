# Phase 1 — Semantic Registry MVP — Codex Requirements

## Overview

Build the **Semantic Registry** — the heart of the Enterprise NL2SQL Copilot system. This phase creates the data model, Git-managed YAML definitions, a YAML-to-Postgres sync mechanism, and a REST API for managing business terms, concepts, metrics, dimensions, entities, and their physical mappings.

## Key Design Principle (read before starting)

Read `docs/02-architecture.md` sections §6 (Semantic layer design), §6.1–§6.7 for the complete semantic object models with examples.

The semantic layer links:
```
business terms → concepts → metrics/dimensions/entities → physical tables/columns
```

Every object must support: versioning, status tracking (draft/reviewed/certified/deprecated), owner, domain, description, created_at, updated_at.

## Project Structure

The monorepo layout is defined in `docs/02-architecture.md` §Repository structure. Create files under:

```
enterprise-nl2sql/
  services/
    semantic-registry/       # FastAPI service
      app/
        main.py              # FastAPI entry point
        models.py            # SQLAlchemy models
        schemas.py           # Pydantic schemas
        api.py               # REST endpoints
        yaml_loader.py       # YAML parsing + validation
        sync.py              # YAML-to-Postgres sync engine
        dependencies.py      # FastAPI dependency injection
      requirements.txt       # Python dependencies
      Dockerfile             # Container
  semantic/                  # Git-managed YAML definitions
    terms/                   # One YAML file per business term
    concepts/                # One YAML file per business concept
    metrics/                 # One YAML file per metric
    dimensions/              # One YAML file per dimension
    entities/                # One YAML file per entity
    join_paths/              # One YAML file per join path
  tests/
    test_semantic_registry/  # Tests for the registry
```

## Functional Requirements

### 1. Semantic Data Model (Postgres)

Create the following Postgres tables via SQLAlchemy models. Each table must support versioning and status tracking.

**Table `semantic_terms`:**
- Fields: id (UUID PK), name (unique), display_name, description, synonyms (text[]), candidate_concepts (text[]), default_concept_by_domain (JSONB), ambiguity_level (enum: low/medium/high), clarification_required_when (text[]), owner, domain, status (enum: draft/reviewed/certified/deprecated), version (int), created_at, updated_at

**Table `semantic_concepts`:**
- Fields: id (UUID PK), name (unique), display_name, domain, definition (text), type (enum: metric_concept/dimension_concept/entity_concept), related_but_different (JSONB), canonical_metric (nullable FK to metrics), owner, status, version, created_at, updated_at

**Table `semantic_metrics`:**
- Fields: id (UUID PK), name (unique), concept_id (FK to semantic_concepts), description, type (enum: simple_sum/count/ratio/distinct_count), numerator_metric_id (nullable FK, for ratio metrics), denominator_metric_id (nullable FK, for ratio metrics), expression (nullable text, for ratio metrics), measure_table (text), measure_column (text), aggregation (enum: sum/count/avg/distinct_count), unit (text), default_time_dimension (text), physical_time_column (text), allowed_dimensions (text[]), owner, status, version, created_at, updated_at

**Table `semantic_dimensions`:**
- Fields: id (UUID PK), name (unique), description, entity (nullable FK to entities), synonyms (text[]), physical_mappings (JSONB — array of {table, column} pairs), status, version, created_at, updated_at

**Table `semantic_entities`:**
- Fields: id (UUID PK), name (unique), description, primary_keys (text[]), related_entities (JSONB), ambiguity_notes (JSONB), status, version, created_at, updated_at

**Table `semantic_physical_mappings`:**
- Fields: id (UUID PK), semantic_type (enum: term/concept/metric/dimension/entity), semantic_id (UUID), physical_table (text), physical_column (text), certified (boolean), created_at, updated_at

**Table `semantic_join_paths`:**
- Fields: id (UUID PK), from_table (text), to_table (text), relationship (enum: one_to_one/many_to_one/one_to_many), join_condition (text), safe_for_metrics (text[]), fanout_risk (enum: low/medium/high), status, version, created_at, updated_at

### 2. YAML Definition Format

Design a YAML schema for each semantic entity type. Each YAML file represents exactly one entity.

**Directory layout:**
```
semantic/
  terms/revenue.yaml
  terms/gmv.yaml
  terms/active_user.yaml
  concepts/net_revenue.yaml
  concepts/paid_gmv.yaml
  metrics/net_revenue.yaml
  metrics/conversion_rate.yaml
  dimensions/channel.yaml
  dimensions/region.yaml
  entities/user.yaml
  entities/order.yaml
  join_paths/order_to_channel.yaml
```

**YAML structure per type:**

For terms, follow the example in `docs/02-architecture.md` §6.2:
- Fields: term, description, synonyms (list), candidate_concepts (list), default_concept_by_domain (map of domain→concept), ambiguity_level, clarification_required_when (list)

For concepts, follow §6.3:
- Fields: concept, display_name, domain, definition, type, owner, related_but_different (map), canonical_metric, status

For metrics, follow §6.4:
- Fields: metric, concept, description, type (simple_sum/count/ratio), measure table/column (for simple), aggregation, unit, default_time_dimension, physical_time_column, allowed_dimensions, owner, status
- For ratio metrics: numerator/denominator (each with table+column), expression, allowed_dimensions

For dimensions, follow §6.5:
- Fields: dimension, description, entity, synonyms, physical_mappings (list of {table, column}), status

For entities, follow §6.6:
- Fields: entity, description, primary_keys, related_entities (map), ambiguity_notes (map)

For join paths, follow §6.7:
- Fields: join_path name, from, to, relationship, join_condition, safe_for_metrics (list), fanout_risk

### 3. YAML Validation Tool

Build a CLI tool (`semantic/validate.py`) that:
- Parses all YAML files in the semantic/ directory tree
- Validates against the schema for each type
- Checks for broken references (e.g., metric references a non-existent concept)
- Reports errors with file path + line number + description
- Exits with non-zero code on any validation failure
- Supports `--fix` flag for auto-fixable issues (e.g., trailing whitespace, missing required fields with defaults)
- Designed to be run as a pre-commit hook and in CI

### 4. YAML-to-Postgres Sync

Build a sync engine (`services/semantic-registry/app/sync.py`) that:
- Reads all YAML files from the semantic/ directory
- Compares with existing Postgres records using a content hash
- Creates new records for new YAML files
- Updates existing records when YAML content changes
- Detects deleted YAML files and marks corresponding records as deprecated (not hard delete)
- Is idempotent: running twice produces the same result
- Reports a summary: N created, M updated, D deprecated, E errors
- Can be triggered via API endpoint and via CLI

### 5. Semantic Registry REST API

Build a FastAPI service at `services/semantic-registry/`:

**List endpoints** (GET, all support pagination + domain filter + status filter):
- `GET /api/v1/terms`
- `GET /api/v1/concepts`
- `GET /api/v1/metrics`
- `GET /api/v1/dimensions`
- `GET /api/v1/entities`
- `GET /api/v1/join-paths`

**Detail endpoints** (GET by ID or name):
- `GET /api/v1/terms/{id_or_name}`
- `GET /api/v1/concepts/{id_or_name}`
- `GET /api/v1/metrics/{id_or_name}`
- `GET /api/v1/dimensions/{id_or_name}`
- `GET /api/v1/entities/{id_or_name}`
- `GET /api/v1/join-paths/{id_or_name}`

**Search endpoint**:
- `GET /api/v1/search?q={query}&domain={domain}&type={type}` — full-text search across all semantic entities

**Sync endpoint**:
- `POST /api/v1/sync` — trigger YAML-to-Postgres sync, return summary

**Health endpoint**:
- `GET /api/v1/health` — return service health + counts of each entity type

All endpoints return consistent JSON: `{"data": ..., "meta": {"total": N, "page": P, "page_size": S}}` for lists, `{"data": ...}` for single entities, `{"error": {"code": "...", "message": "..."}}` for errors.

### 6. Initial Semantic Content

Create initial YAML files for the **commerce** domain. Populate:

- **Terms (30-50):** Include at least: revenue, gmv, paid_gmv, net_revenue, active_user, new_user, customer, payer, buyer, order, conversion, retention, churn, campaign, channel, traffic_source, acquisition_cost, refund, discount, commission, settlement, gross_profit, margin, ARPU, ARPPU, LTV, DAU, MAU, session, click, impression, CTR, CVR, CPC, CPM, ROAS, spend, budget, forecast, target, actual, variance

- **Concepts (15-20):** Include at least: net_revenue, paid_gmv, gmv, active_user, conversion_rate, retention_rate, customer_acquisition_cost, return_on_ad_spend, average_revenue_per_user, churn_rate, gross_margin, order_count, paid_order_count, click_count, impression_count

- **Metrics (20-40):** Include at least: net_revenue (simple_sum), paid_gmv (simple_sum), gmv (simple_sum), active_users (distinct_count), new_users (distinct_count), order_count (count), paid_order_count (count), conversion_rate (ratio: paid_order_count/click_count), CTR (ratio: click_count/impression_count), CPC (ratio: spend/click_count), CPM (ratio: spend*1000/impression_count), ROAS (ratio: paid_gmv/spend), ARPU (ratio: net_revenue/active_users), ARPPU (ratio: net_revenue/payers), customer_acquisition_cost (ratio: spend/new_users)

- **Dimensions (20-30):** Include at least: channel, campaign, region, product_category, device_type, traffic_source, user_segment, date, week, month, quarter, year, payment_method, order_status, marketing_channel, ad_group, creative, landing_page, browser, os_version, language, country, city, age_group, gender

- **Entities (5-10):** Include at least: user, order, product, campaign, channel, merchant, payment

- **Join paths (10-15):** Include realistic join paths between tables covering the entity relationships for the commerce domain

### 7. Tests

All tests go in `tests/test_semantic_registry/`.

- Unit tests for YAML parsing and validation (test valid YAML, invalid YAML, missing fields, broken references)
- Unit tests for sync engine (test create, update, deprecate, idempotency)
- Unit tests for API endpoints (test list, detail, search, sync, health)
- Unit tests for data model constraints (test uniqueness, foreign keys, enum values, version increment)
- Integration test: write sample YAMLs → run sync → verify Postgres state → verify API returns correct data
- Integration test: modify YAML → re-run sync → verify updates propagated

### 8. Docker Setup

Create a `Dockerfile` for the semantic-registry service and a `docker-compose.yml` at the project root that starts:
- semantic-registry API
- Postgres database

## Non-Functional Requirements

- All API responses must be under 500ms for list endpoints (with < 1000 records)
- YAML validation must process 200+ files in under 2 seconds
- Sync must process 200+ YAML files in under 10 seconds
- All database queries must use proper indexing (unique indexes on name fields, indexes on domain and status)
- API must return proper HTTP status codes (200, 201, 400, 404, 409, 500)
- All Python code must be typed with type hints
- Follow PEP 8 style

## What Not to Build

- Do not build authentication/authorization (that comes later)
- Do not build the frontend UI (that comes in Phase 4)
- Do not build LLM integration (that comes in Phase 3-4)
- Do not build metadata ingestion from warehouse (Phase 2)
- Do not build the retrieval system (Phase 2)

## Design References

- `docs/01-product-design.md` — Product context and MVP scope
- `docs/02-architecture.md` §6 (Semantic layer design) — Complete object models with examples
- `docs/02-architecture.md` §15 (Service decomposition) — How this service fits into the system
- `docs/02-architecture.md` §17 (Storage design) — Database table patterns and conventions
