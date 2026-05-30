# NL2SQL Pipeline Review & Improvement Plan

## Executive Summary

The current benchmark path is a strong one-shot/few-shot baseline, but it is still a thin prompting pipeline:

- `scripts/run_full_benchmark.py:33` exposes only raw SQLite `CREATE TABLE` text. It omits column descriptions, primary/foreign key summaries, join path hints, sample values, value aliases, and generated BIRD semantic registry mappings.
- `scripts/run_full_benchmark.py:128` retrieves few-shot examples using `classify_query_type(gold_sql)`. That leaks gold SQL structure during benchmark evaluation and does not represent production behavior. It also means retrieval quality is not the real bottleneck being measured.
- `scripts/sql_pattern_memory.py:50` has a coarse regex classifier. It detects joins, aggregation, subqueries, ratio, and top-N, but it does not isolate high-risk constructs such as `CASE`, `CAST`, `HAVING`, `DISTINCT`, multi-join count, set operations, or calculated ordering.
- `scripts/sql_pattern_memory.py:197` retrieves patterns with same DB/type plus lexical term overlap. It has no embedding search, no schema-aware matching, no join-shape matching, and no negative signal for examples that use unavailable columns.
- `scripts/sql_pattern_memory.py:348` builds a prompt with examples, raw schema, optional evidence, and output JSON only. It does not force a column-selection phase, join plan, filter-value validation, WHERE/HAVING decision, or ranking checklist.
- `scripts/run_full_benchmark.py:142` performs one generation call and `scripts/run_full_benchmark.py:149` only executes the final SQL. There is no static validation, execution-guided repair, empty-result repair, candidate voting, or targeted retry.
- `scripts/pattern_memory_v2.py:464` and `scripts/nl2sql_api.py:39` show that richer V2 pattern memory exists, but the benchmark does not use it. V2 is directionally better, but not sufficient as-is because `scripts/pattern_memory_v2.py:512` adds two LLM calls per question and still lacks schema/value grounding.
- `src/semantic_registry/pipeline/context_builder.py:82` can render richer candidate tables, columns, metrics, and join paths, and `bird_semantic/<db_id>/` contains generated terms, concepts, dimensions, metrics, and join paths. None of that is used by the benchmark.

The ~80% ceiling is mostly explained by under-grounding rather than model size. The model is asked to infer business meaning, physical columns, join paths, domain literals, and SQL edge constructs from raw DDL in a single pass. To approach 90%+, the benchmark runner needs three additions: enriched schema/value context, a query planning and retrieval layer that predicts risk features from the natural-language question, and execution/static validation repair.

Expected realistic endpoint after all recommendations: **86-89% EX** without gold leakage and **88-91% EX** if candidate generation and execution-guided reranking are allowed to use multiple LLM calls per question. A stable 90%+ likely requires either learned retrieval/selector tuning on dev-like failures or a larger candidate pool with result-aware selection.

## Prioritized Improvements

| Priority | Change | Expected EX gain | Main failure categories helped |
|---:|---|---:|---|
| 1 | Enriched schema context: columns, PK/FK, join paths, semantic registry mappings, sample values | +3.0 to +5.0 | column mismatch, joins, filter values |
| 2 | Execution-guided repair loop with targeted prompts | +2.0 to +4.0 | invalid SQL, wrong literals, missing joins, empty/wrong-result queries |
| 3 | Replace V1 retrieval with schema-aware V2.5 hybrid retrieval, not current V2 directly | +1.5 to +3.0 | column mismatch, joins, CASE/subquery patterns |
| 4 | Generate 3-5 candidates for high-risk questions and select by validation/result heuristics | +1.5 to +3.0 | math, CAST, CASE, GROUP/HAVING, top-N |
| 5 | Add risk-aware prompt templates and SQL checklists | +1.0 to +2.0 | top-N, HAVING, DISTINCT, ratios, CASE |
| 6 | Add domain value retrieval from SQLite distinct samples | +1.0 to +2.0 | filter value errors, LIKE/string ops |
| 7 | Improve query classifier and stop using gold SQL for retrieval | +0.5 to +1.5 | benchmark validity, routing, few-shot quality |
| 8 | Store detailed failure artifacts and run ablations | +0.5 to +1.0 indirectly | prioritization and regression control |

The gains overlap; do not sum the upper bounds naively. A reasonable combined estimate is **+7 to +11 EX points** over the current 79.5% Pro few-shot baseline.

## Concrete Code-Level Suggestions

### 1. Build Enriched BIRD Schema Context

Modify `scripts/run_full_benchmark.py:33`.

Replace `get_schema_text(db_root, db_id)` with a richer builder, for example:

- Load raw DDL from SQLite as today.
- Load `bird_bench/dev/dev_20240627/dev_tables.json`.
- Include table names, original column names, column types, primary keys, foreign keys, and column comments/descriptions if present.
- Include generated semantic registry snippets from `bird_semantic/{db_id}`:
  - `terms`: natural-language aliases and candidate concepts.
  - `dimensions`: physical table/column mappings.
  - `metrics`: measure column, aggregation, expression.
  - `join_paths`: explicit join conditions.
- Include a small value profile for likely text columns: top distinct values, normalized aliases, and examples from evidence terms.

Suggested new file: `scripts/bird_schema_context.py`.

Suggested functions:

- `build_schema_context(db_root, tables_path, semantic_root, db_id, question, evidence) -> str`
- `get_value_hints(db_path, question, evidence, max_columns=12, max_values=8) -> dict`
- `load_semantic_hints(semantic_root, db_id, question) -> str`

Prompt section example:

```text
Schema context:
- Table: schools
  Columns:
  - CDSCode TEXT: school identifier
  - County TEXT: county name; sample values: Alameda, Fresno, Los Angeles
  - Educational Option Type TEXT: school option type; relevant value aliases:
    "continuation school" -> "Continuation School"

Join paths:
- frpm.CDSCode = schools.CDSCode

Semantic mappings:
- "eligible free rate" -> frpm.`Free Meal Count (K-12)` / frpm.`Enrollment (K-12)`
- "county" -> schools.County
```

Expected impact: **+3 to +5 EX**, especially for the 31% column/expression mismatch and 13% filter literal failures.

### 2. Add Execution-Guided Self-Correction

Modify `scripts/run_full_benchmark.py:142` through `scripts/run_full_benchmark.py:159`.

Current behavior generates once, then only logs execution errors. Add a repair loop:

1. Generate SQL.
2. Static-validate with `validate_select_sql()` from `src/semantic_registry/pipeline/llm_gateway.py`.
3. Execute predicted SQL.
4. If syntax/runtime error, retry with the error message, schema context, and previous SQL.
5. If result is empty but question likely expects rows/counts, retry with sample values and warning that a filter literal may be wrong.
6. If SQL omits required constructs detected from the question, retry with a targeted checklist:
   - "top/highest/lowest/most" requires `ORDER BY` and usually `LIMIT`.
   - "per/by/each" usually requires `GROUP BY`.
   - "more than count/average/sum" after grouping requires `HAVING`.
   - ratio/rate/percentage requires float-safe division.

Suggested helper:

```python
def generate_with_repair(provider, prompt, db_path, question, evidence, schema_context, max_repairs=2):
    attempts = []
    sql = generate_once(provider, prompt)
    for attempt in range(max_repairs + 1):
        validation = validate_and_execute(sql, db_path)
        attempts.append({ "sql": sql, **validation })
        if validation["ok"] and not should_retry_semantic(question, sql, validation):
            return sql, attempts
        repair_prompt = build_repair_prompt(question, evidence, schema_context, sql, validation, attempts)
        sql = generate_once(provider, repair_prompt)
    return choose_best_attempt(attempts), attempts
```

Do not compare with gold during repair. Use only SQL validity, result emptiness, row count reasonableness, and construct checks.

Expected impact: **+2 to +4 EX**. This is one of the highest-return changes because many BIRD misses are near-misses.

### 3. Replace V1 With V2.5 Pattern Memory, Not Current V2 As-Is

Do not directly drop in `scripts/pattern_memory_v2.py` for the full benchmark. It is richer than V1, but `scripts/pattern_memory_v2.py:512` performs LLM analysis and reranking at query time, increasing latency/cost and introducing another failure point. Also, retrieval is based on business entities and pattern metadata, not schema compatibility.

Modify or extend `scripts/pattern_memory_v2.py`:

- Add offline ingestion of:
  - SQL AST features using `sqlglot`: join count, tables, columns, aggregates, `HAVING`, `CASE`, `CAST`, `DISTINCT`, subquery count, order/limit.
  - Schema footprint: `tables_used`, `columns_used`, join edges.
  - Question embeddings or BM25 tokens.
- Add deterministic question analysis before LLM rerank:
  - `predict_query_features(question, evidence)`.
  - `extract_candidate_terms(question, evidence)`.
  - `retrieve_by_schema_overlap(db_id, predicted_terms, predicted_features)`.
- Use LLM rerank only for top 10-20 candidates or high-risk cases.
- Prefer same-DB examples with similar schema footprint over cross-DB examples with vague semantic similarity.

Modify `scripts/run_full_benchmark.py:93` and `scripts/run_full_benchmark.py:128`:

- Add CLI option `--memory v1|v25`.
- For V2.5, call `memory.retrieve(question, db_id=db_id, schema_context=schema, predicted_features=features, top_k=4)`.
- Remove `classify_query_type(gold_sql)` from normal benchmark mode.

Expected impact: **+1.5 to +3 EX**. This helps most where the current examples are structurally similar but use the wrong columns or miss special constructs.

### 4. Add Multi-Candidate Generation For High-Risk Questions

The semantic registry pipeline already has `CandidateGenerator.generate_candidates()` at `src/semantic_registry/pipeline/candidate_generator.py:34`, but the benchmark bypasses it. Bring the idea into `scripts/run_full_benchmark.py`.

Add:

- A cheap risk detector using the question, evidence, and retrieved examples:
  - math/rate/percentage/ratio
  - top/highest/lowest/most/least/rank
  - by/per/each/group
  - more than/at least/fewer than with aggregate words
  - if/categorize/type/bucket
  - "not", "except", "only", "both"
- For simple low-risk queries: one candidate.
- For high-risk queries: 3-5 candidates with distinct strategies:
  - direct
  - plan-first
  - join-first
  - value-grounded
  - conservative aggregate/HAVING

Selection heuristics:

- Reject invalid SQL.
- Reject queries using columns not in schema.
- Prefer SQL satisfying required constructs.
- Prefer non-empty results unless the question can legitimately return none.
- Prefer candidates whose `tables_used` and `columns_used` match semantic hints.
- For top-N, prefer exact `LIMIT N` and `ORDER BY`.

Expected impact: **+1.5 to +3 EX**, especially for high-risk constructs with phi coefficients around +0.27 to +0.29.

### 5. Strengthen Prompt Templates By Failure Category

Modify `scripts/sql_pattern_memory.py:348` and the zero-shot branch in `scripts/run_full_benchmark.py:132`.

Add a compact planning contract before the JSON output. Avoid requiring hidden chain-of-thought in the final answer, but ask the model to internally plan and return a short `reasoning_summary`.

Prompt additions:

```text
Before writing SQL, internally verify:
1. Which exact table owns each selected/filter/group/order column?
2. Are any filter values enumerated in the schema/value hints? Use the exact stored literal.
3. If ranking is requested, include ORDER BY with the correct direction and LIMIT.
4. If filtering aggregated values, use HAVING; use WHERE only before aggregation.
5. If computing a ratio/rate/percentage, avoid integer division with CAST or * 1.0.
6. If joining, use only listed join paths and qualify ambiguous columns.
7. Return only final JSON; do not include the internal plan.
```

Category-specific changes:

- Column/expression mismatch:
  - Add "columns_used must be fully qualified as table.column".
  - Ask for exact physical mapping from semantic hints.
- JOIN complexity:
  - Add "write join path before SQL internally".
  - Add "never join on similarly named text labels when an ID/FK path exists".
- Filter value errors:
  - Add "copy exact literals from Hint or Value hints".
  - Include top distinct values for relevant columns.
- Missing ORDER/LIMIT:
  - Add top-N examples and force `LIMIT 1` for superlatives unless question asks all ties.
- WHERE vs HAVING:
  - Add explicit rule and examples.
- CASE/DISTINCT/subquery:
  - Add construct-specific few-shots and route via classifier.

Expected impact: **+1 to +2 EX** alone; larger when paired with enriched context.

### 6. Use Semantic Registry For Column Selection

The generated BIRD semantic assets under `bird_semantic/<db_id>/` are directly relevant. For example, `bird_semantic/california_schools/terms/*.yaml` maps phrases such as "continuation school", "county", and "eligible free rate" to concepts, while dimensions and metrics map concepts to physical columns.

Do not route the whole benchmark through the current governed semantic pipeline initially; it appears built for enterprise metric-style queries and may under-cover arbitrary BIRD SQL. Instead, use the registry as hints inside the benchmark prompt and candidate selector.

Suggested new helper in `scripts/bird_semantic_hints.py`:

- `load_registry_for_db(db_id)`.
- `match_terms(question, evidence, registry)`.
- `render_semantic_hints(matches, registry)`.
- `score_candidate_columns(sql, matched_hints)`.

Use it in `scripts/run_full_benchmark.py:126` when building `schema`.

Expected impact: **+2 to +4 EX** if the metric/dimension assets are clean. This overlaps with enriched schema context but is the most direct attack on wrong-column failures.

### 7. Add Domain Value Grounding

Filter literal errors are not solved by raw DDL. Add SQLite value sampling.

Suggested implementation in `scripts/bird_schema_context.py`:

- Extract quoted terms and noun phrases from question/evidence.
- For text columns in candidate tables, run:
  - `SELECT DISTINCT col FROM table WHERE col LIKE ? LIMIT 10`
  - fallback top values: `SELECT col, COUNT(*) FROM table GROUP BY col ORDER BY COUNT(*) DESC LIMIT 8`
- Normalize hyphens, case, punctuation, and singular/plural for matching.
- Render only relevant values to control prompt length.

Repair behavior:

- If a query returns zero rows and contains string filters, prompt a repair with nearest candidate literals.

Expected impact: **+1 to +2 EX**.

### 8. Improve Query Classification And Routing

Modify `scripts/sql_pattern_memory.py:50`.

Add a production-safe `classify_question_features(question, evidence)` and an AST-based `classify_sql_features(sql)` for offline pattern tagging.

New feature flags:

- `join_count`
- `needs_top_n`
- `needs_group_by`
- `needs_having`
- `needs_ratio`
- `needs_cast`
- `needs_case`
- `needs_distinct`
- `needs_subquery`
- `needs_string_op`
- `needs_between`
- `needs_like`
- `has_negation`

Use SQL AST via `sqlglot` when classifying stored gold SQL. Regex is acceptable for question routing but should be explicit and tested.

Important benchmark correction:

- Replace `qtype = classify_query_type(gold_sql)` at `scripts/run_full_benchmark.py:129` with `features = classify_question_features(question, evidence)`.
- Keep gold-SQL classification only for offline analysis and pattern ingestion.

Expected impact: **+0.5 to +1.5 EX**, plus benchmark validity.

## Failure Category Playbook

### 1. Column / Expression Mismatch

Primary changes:

- Enriched schema with semantic mappings.
- Require fully qualified `columns_used`.
- Candidate scoring against registry hints.
- Pattern retrieval by schema footprint.

Files:

- `scripts/run_full_benchmark.py:33`: replace schema text builder.
- `scripts/sql_pattern_memory.py:348`: add column-selection checklist.
- New `scripts/bird_semantic_hints.py`.
- New `scripts/bird_schema_context.py`.

Expected recovery: **25-35% of wrong-column misses**, about **+3 to +5 EX** overall.

### 2. JOIN + Multi-Table Complexity

Primary changes:

- Render explicit FK/join paths from `dev_tables.json` and `bird_semantic/*/join_paths`.
- Add join-first candidate strategy.
- Validate all referenced columns and join aliases.
- Prefer ID/FK joins over label joins.

Files:

- `scripts/bird_schema_context.py`: join path renderer.
- `scripts/run_full_benchmark.py`: high-risk candidate generation and selection.
- `scripts/sql_pattern_memory.py`: add `multi_join_agg`, `multi_join_filter`, `join_subquery` pattern tags.

Expected recovery: **20-30% of join misses**, about **+2 to +3 EX** overall.

### 3. Filter Value Errors

Primary changes:

- Value hints from actual SQLite distinct values.
- Promote BIRD `evidence` from "Hint" to "Evidence and exact-value guidance".
- Empty-result repair for string filters.

Files:

- `scripts/run_full_benchmark.py:137`: replace weak hint line with stronger evidence/value section.
- `scripts/bird_schema_context.py`: sample and render candidate values.
- Repair helper in `scripts/run_full_benchmark.py`.

Expected recovery: **25-40% of literal misses**, about **+1 to +2 EX** overall.

### 4. Missing ORDER BY + LIMIT

Primary changes:

- Question feature detector for top-N/superlative/ranking.
- Prompt rule requiring `ORDER BY` plus `LIMIT`.
- Candidate validator that rejects top-N SQL without both constructs.

Files:

- `scripts/sql_pattern_memory.py`: add `classify_question_features`.
- `scripts/run_full_benchmark.py`: `should_retry_semantic()` construct checks.
- `scripts/sql_pattern_memory.py:348`: add top-N checklist and examples.

Expected recovery: **30-45% of ranking misses**, about **+1 to +2 EX** overall.

### 5. GROUP BY + WHERE vs HAVING

Primary changes:

- Prompt rule: filters on raw rows use `WHERE`; filters on aggregate results use `HAVING`.
- Detect phrases such as "with more than N", "having at least N", "where count/sum/average".
- Candidate validator checks aggregate functions in `WHERE`.

Files:

- `scripts/sql_pattern_memory.py`: add classifier flags.
- `scripts/run_full_benchmark.py`: static validator using `sqlglot`.
- `scripts/sql_pattern_memory.py:348`: add WHERE/HAVING examples.

Expected recovery: **25-35% of HAVING misses**, about **+0.7 to +1.2 EX** overall.

### 6. CASE WHEN / DISTINCT / Subquery Logic

Primary changes:

- Add construct-specific pattern tags and few-shot buckets.
- Generate high-risk candidates with plan-first and construct-specific prompts.
- Add AST validation and selection preferences.

Files:

- `scripts/sql_pattern_memory.py:36`: expand query types.
- `scripts/pattern_memory_v2.py`: store AST feature metadata.
- `scripts/run_full_benchmark.py`: route complex questions to multi-candidate mode.

Expected recovery: **15-25% of these misses**, about **+1 to +2 EX** overall.

### 7. BETWEEN / LIKE / String Ops

Primary changes:

- Add string/date operator hints in prompt.
- Value/date recognizer for ranges.
- Examples for `LIKE`, `BETWEEN`, `SUBSTR`, and string concatenation.

Files:

- `scripts/sql_pattern_memory.py`: classifier flags and pattern tags.
- `scripts/bird_schema_context.py`: type/value profiling.

Expected recovery: **small but cheap**, about **+0.2 to +0.5 EX** overall.

## Benchmark Instrumentation Changes

Modify result storage in `scripts/run_full_benchmark.py:164`.

Currently only `idx`, `db_id`, `match`, and `sql` are saved. Add:

- `question`
- `difficulty`
- `evidence`
- `gold_sql`
- `raw_response`
- `patterns_used`
- `schema_context_version`
- `predicted_features`
- `attempts`
- `execution_error`
- `row_count`
- `validation_errors`

This is needed for ablations. Without detailed artifacts, improvements will be hard to attribute.

## Should V2 Replace V1?

Yes, but only after upgrading it to V2.5.

Current V1 is too shallow for the observed failure distribution. Current V2 is semantically richer and already used by the API at `scripts/nl2sql_api.py:39`, but it should not be dropped directly into the benchmark because it:

- Adds query-time LLM analysis/reranking latency.
- Does not guarantee schema-compatible examples.
- Does not store enough AST/schema features for hard SQL structure matching.
- Still builds a prompt similar to V1 at `scripts/pattern_memory_v2.py:562`.

Recommendation:

1. Keep V1 as a fast baseline.
2. Create V2.5 with offline LLM enrichment plus deterministic AST/schema metadata.
3. Use hybrid retrieval: same DB + schema footprint + feature tags + semantic entities + optional LLM rerank.
4. Run ablations:
   - V1 raw schema
   - V1 enriched schema
   - V2.5 enriched schema
   - V2.5 enriched schema + repair
   - V2.5 enriched schema + repair + multi-candidate

## Chain-of-Thought Prompting Strategy

Do not ask the model to output long chain-of-thought. Use structured private planning instructions and require only compact `reasoning_summary`.

Recommended prompt pattern:

```text
Internally plan the query in this order:
1. select exact table/column mappings from schema and semantic hints
2. select join path
3. select exact filter literals
4. decide aggregation, GROUP BY, HAVING
5. decide ORDER BY and LIMIT
6. verify SQLite syntax

Return only final JSON. The reasoning_summary should be one short sentence, not a derivation.
```

This gets most of the planning benefit without bloating output or making parsing harder.

## Expected Final EX Estimate

Starting point: **79.5% EX** with V4 Pro high few-shot.

Estimated after staged implementation:

- Enriched schema + semantic hints: **82.5-84.5%**
- Add value grounding and stronger prompts: **84.0-86.0%**
- Add V2.5 schema-aware retrieval: **85.5-87.5%**
- Add execution repair: **87.0-89.0%**
- Add high-risk multi-candidate generation/selection: **88.0-91.0%**

Pragmatic target for the next engineering iteration: **86-88% EX**. A credible 90% run likely requires multi-candidate inference plus tuned selection heuristics and careful ablation to avoid regressions on simple queries.
