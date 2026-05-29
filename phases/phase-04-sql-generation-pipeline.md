# Phase 4 — SQL Generation Pipeline

**Duration:** 2–3 weeks

**Depends on:** Phase 2 (metadata retrieval), Phase 3 (semantic resolver)

**Design references:** [02-Architecture §9 (NL2SQL runtime pipeline)](../docs/02-architecture.md#nl2sql-runtime-pipeline), [§10 (SQL generation)](../docs/02-architecture.md#sql-generation-design), [§15 (Service decomposition)](../docs/02-architecture.md#service-decomposition)

---

## Overview

Build the core SQL generation pipeline that orchestrates the full NL → SQL flow. This phase wires together the question classifier, context builder, LLM gateway, SQL generation, and response assembly into a deterministic state machine.

---

## Requirements

### R4.1 — Pipeline state machine
- Implement the NL2SQL pipeline as a deterministic state machine following the flow defined in the architecture document.
- The state machine must process: question classification → term extraction → semantic resolution → metadata retrieval → context building → SQL candidate generation → validation → preview → response.
- The pipeline must be synchronous and single-threaded for MVP (no parallel branching or complex agentic loops).
- Each stage must produce structured output that the next stage can consume.

### R4.2 — Question classifier
- Build a classifier that receives the user's natural language question and produces a classification response.
- The classifier must detect: `domain` (which business domain), `query_type` (metric_by_dimension, comparison, time_series, top_N, etc.), `risk_level` (low/medium/high), `write_intent` (true/false), `sensitive_data_intent` (true/false), `requires_time_range` (true/false).
- If `write_intent` is detected, the pipeline must block and return an error before any further processing.

### R4.3 — Context builder
- Build a prompt context that combines the semantic query plan, retrieved metadata, and the original user question.
- The context must include only: resolved metric/dimension/entity information from the semantic plan, table descriptions, column descriptions, grain info, partition info, join paths, known caveats.
- The context must NOT include: raw sensitive data, full table scans, PII values, internal system details.
- The context must be structured as a prompt template suitable for the LLM gateway.

### R4.4 — LLM gateway
- Build a thin gateway layer that handles LLM API calls.
- The gateway must enforce a strict JSON output contract: the LLM must return structured JSON with fields `sql`, `assumptions`, `tables_used`, `columns_used`, `confidence`, and `reasoning_summary`.
- The gateway must enforce generation rules: SELECT-only, no invented tables/columns, no SELECT *, no invented metrics, use specified dialect, return JSON only.
- The gateway must support configurable models (to allow model upgrades and A/B testing).
- The gateway must enforce a retry policy on transient API failures.

### R4.5 — 2-candidate SQL generation
- For each user question, generate exactly 2 SQL candidates.
- Candidate A: direct SQL generation from the semantic plan and metadata context.
- Candidate B: plan-first SQL generation using the same semantic plan (generate an execution plan / query steps first, then emit SQL).
- Both candidates must use the same input context.
- The system should not generate more than 2 candidates in MVP.

### R4.6 — Strict JSON parser
- Parse the LLM's response into the expected structured output.
- If the response is not valid JSON, retry the generation (up to a configurable retry limit).
- If the response is valid JSON but missing required fields, log the error and retry.
- Extract and validate each required field separately.

### R4.7 — SQL explanation generator
- For each generated SQL, produce a human-readable explanation.
- The explanation must cover: which metric was used and why, which table was selected, which columns were used, what assumptions were made about time ranges and filters.
- The explanation should reference business-level names (e.g., "Net Revenue") not physical column names (e.g., "net_revenue_amt"), unless the user requests details.
- The explanation must be presented in the frontend as part of the response.

### R4.8 — Basic frontend result page
- Build a minimal frontend page that displays the pipeline output.
- The page must show: the original question, the semantic interpretation (metric, dimension, time), the generated SQL (in a read-only Monaco editor), the assumptions, the tables/columns used, validation results, preview results (if available), and feedback buttons.
- The page does not need to support editing SQL or running queries yet (that comes in Phase 5 and 6).

---

## Exit Criteria

- Simple supported questions produce valid SQL using correct certified tables and columns.
- The pipeline state machine processes all steps without error for supported query types.
- The LLM gateway enforces the JSON output contract and generation rules.
- The frontend displays the pipeline output correctly.
- Write-intent queries are blocked before any processing.
