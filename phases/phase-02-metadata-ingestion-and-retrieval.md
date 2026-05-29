# Phase 2 — Metadata Ingestion and Retrieval

**Duration:** 2 weeks

**Depends on:** Phase 1 (semantic registry populated with certified tables and metrics)

**Design references:** [02-Architecture §7 (Metadata layer)](../docs/02-architecture.md#metadata-layer-design), [§8 (Retrieval architecture)](../docs/02-architecture.md#retrieval-architecture)

---

## Overview

Build the metadata ingestion pipeline and the retrieval system. This phase connects the warehouse's schema/dictionary to the NL2SQL system, normalizes metadata, generates embeddings, and provides hybrid search across tables, columns, terms, and metrics.

---

## Requirements

### R2.1 — Metadata adapter
- Build a metadata provider that connects to the warehouse's schema and data dictionary systems.
- The adapter must implement the `MetadataProvider` interface: `search_tables(query, domain)`, `get_table(table_name)`, `get_columns(table_name)`, `get_join_paths(tables)`, `get_example_queries(query)`.
- Support at least one warehouse dialect (as selected in Phase 0).
- The adapter must handle schema drift gracefully: unknown columns or missing tables should not crash the pipeline.

### R2.2 — Metadata normalization
- Normalize raw warehouse metadata into a canonical format: table descriptions, column descriptions, column data types, grain, partition columns, owner, PII tags, certified status.
- For each table, compute and store: the "eligible_for_nl2sql" flag based on the eligibility rule defined in the architecture doc (certified=true, owner_exists=true, grain_documented=true, etc.).
- Store normalized metadata in Postgres `metadata_snapshots` table for versioning.

### R2.3 — Metadata snapshotting
- On each sync, create a snapshot of the current metadata state with a version identifier.
- Store snapshots so the evaluation system can reproduce results against a known metadata version.
- The snapshot must include: all table/column metadata, all semantic registry entities, semantic-to-physical mappings.

### R2.4 — Retrieval document generation
- For each certified table, generate a "retrieval document" that combines: table name, description, column names with descriptions, grain, partition column, join paths, known caveats, sample values or value summaries, example queries.
- For each business term, generate a retrieval document that combines: term name, synonyms, description, associated concepts, domain-specific default mappings.
- For each metric, generate a retrieval document that combines: metric name, description, aggregation type, allowed dimensions, physical table/column mapping.

### R2.5 — Embedding generation
- Generate embeddings for all retrieval documents using a sentence-transformer model.
- Store embeddings in a pgvector index within Postgres.
- The embedding pipeline must be incremental: only generate and store embeddings for new or changed documents.
- Support configurable embedding models (to allow future upgrades).

### R2.6 — Hybrid retriever
- Build a hybrid retriever that scores candidate tables/metrics/terms using a weighted combination:
  - embedding similarity (weight: 0.35)
  - keyword/text match (weight: 0.30)
  - semantic concept match from query context (weight: 0.15)
  - certification boost for certified entities (weight: 0.10)
  - usage popularity boost (weight: 0.10)
- The retriever must return, for a given natural language query: candidate concepts, candidate metrics, candidate dimensions, candidate tables, candidate columns, and known caveats.
- The retriever must support domain filtering: only return candidates from the detected or specified domain.

### R2.7 — Retrieval debug UI
- Build a simple debug interface for the retrieval system.
- The debug UI must accept a natural language query and display: raw scores for each candidate, the top-5 table results, the top-5 metric results, and the final hybrid score breakdown.
- This is an internal tool for developers and data analysts to tune retrieval quality.

---

## Exit Criteria

- For eval questions, the correct table appears in the top 5 retrieval results more than 85% of the time.
- Metadata adapter successfully syncs all certified tables from the warehouse.
- Hybrid retriever is functional and returns structured candidate output.
- Retrieval debug UI is accessible and usable.
- Metadata snapshotting and versioning work correctly.
