# Phase 6 — Repair, Selection, and Feedback

**Duration:** 1–2 weeks

**Depends on:** Phase 4 (SQL generation), Phase 5 (validation and execution)

**Design references:** [02-Architecture §9 (Pipeline state machine)](../docs/02-architecture.md#nl2sql-runtime-pipeline), [01-Product Design §13 (Frontend)](../docs/01-product-design.md#frontend-product-design)

---

## Overview

Build the repair-once loop, candidate selection logic, and user feedback capture. This phase closes the loop between the system and the user — when SQL fails validation, the system attempts one repair pass; when the user corrects the output, the system captures the correction for future improvement.

---

## Requirements

### R6.1 — Error classification
- Classify validation errors into categories: parse error, static validation error, semantic validation error, permission error, execution error, cost threshold exceeded.
- For each error category, define whether a repair attempt is possible or the system should return an error to the user.
- Parse errors and permission errors should not be repaired (return to user). Semantic validation errors and static validation errors may be repaired.

### R6.2 — One repair loop
- After validation fails, attempt exactly one repair pass.
- The repair must: identify the specific validation failure (e.g., "used column gmv_amt but resolved metric was net_revenue"), produce a corrected SQL, and re-run validation.
- The repair loop must execute at most once per pipeline run (no infinite repair loops).
- If the repaired SQL passes validation, proceed to execution with the repaired candidate.
- If the repaired SQL also fails validation, return both the original and repaired SQL with the validation failures explained to the user.

### R6.3 — Candidate selector
- After both candidates have been generated, validated, and (if needed) repaired, select the best candidate.
- Selection criteria in priority order: (1) passes all validations, (2) higher semantic match score, (3) simpler SQL (fewer joins, fewer subqueries), (4) higher semantic resolution confidence.
- Return only the selected candidate to the user, but log both candidates for evaluation.

### R6.4 — Feedback UI
- Add feedback buttons to the frontend: [Correct] [Partially correct] [Wrong].
- [Correct]: mark the generated SQL and interpretation as correct for future eval.
- [Partially correct]: allow the user to edit the SQL and submit a corrected version.
- [Wrong]: allow the user to explain what was wrong.
- The feedback UI must be inline with the result display, not on a separate page.

### R6.5 — Corrected SQL capture
- When a user submits a corrected SQL, store it in the `nl2sql_feedback` table.
- The captured data must include: query_id, original SQL, corrected SQL, user comment, feedback type, reviewer identity, timestamp.
- Corrected SQL should be available for promotion to eval cases (manual, not automatic in MVP).

### R6.6 — Query history
- Store every pipeline run in the `nl2sql_query_log` table.
- The query history must be viewable in the frontend, sortable by date, and filterable by domain and status.
- Each history entry must link to: the generated SQL, the user's feedback, the semantic plan, validation results, execution results, and the metadata snapshot version used.
- Users must be able to re-run a previous query from history or submit feedback retroactively.

### R6.7 — Query history API
- Build REST endpoints for query history:
  - `GET /api/v1/queries` — list queries with pagination and filters (by user, domain, date range, status).
  - `GET /api/v1/queries/{query_id}` — get full query detail including semantic plan, SQL, validation results, execution results.
  - `POST /api/v1/queries/{query_id}/feedback` — submit feedback for a specific query.

---

## Exit Criteria

- Common SQL errors (wrong column, wrong metric, missing partition filter) can be repaired in one pass.
- User corrections are captured and stored in the feedback table.
- Query history is viewable and filterable in the frontend.
- The feedback mechanism works for all three feedback types.
- The candidate selector produces a reasonable single output for every pipeline run.
