# V2.5 Pattern Memory Regression Analysis

## Summary

On the 220-question benchmark, V2.5 pattern memory drops from V1's 80.9% EX (178/220) to 75.0% EX (165/220). The regression is concentrated in mature databases where there are many same-DB seed examples:

| Database | V1 EX | V2.5 EX | Net loss |
| --- | ---: | ---: | ---: |
| california_schools | 90% | 65% | -5 / 20 |
| toxicology | 95% | 70% | -5 / 20 |
| european_football_2 | 80% | 65% | -3 / 20 |

Across the full sample there are 21 V1-correct/V2.5-wrong cases and only 8 V2.5-correct/V1-wrong cases.

The common failure mode is not the enriched schema or execution repair. It is retrieval precision: V2.5 often fails to surface the exact or near-exact same-DB training example that V1 retrieves.

## Root Cause 1: V2.5 infers query type from the question, while V1 is given the gold SQL type

In `scripts/run_full_benchmark.py`, V1 receives:

```python
qtype = classify_query_type(gold_sql)
patterns = memory.retrieve(question, db_id, qtype, top_k=3)
```

V2.5 receives only:

```python
patterns = memory.retrieve(question, db_id, top_k=3)
```

V2.5 then calls `analyze_question()` and predicts `qfeatures.query_type` from surface words. This is much noisier than V1's gold-SQL classifier. In the current 220-sample, V2.5's inferred type disagrees with V1's gold-SQL type on 182/220 questions. For V1-correct/V2.5-wrong cases, 18/21 have this mismatch.

Examples:

- `california_schools`, dev index 21: gold/V1 type is `agg_filter`; V2.5 infers `simple_agg` because "more than" and "less than" are not enough to mark the query as filtered. V1 retrieves the exact `frpm` count example; V2.5 retrieves unrelated count examples.
- `toxicology`, dev index 210: gold/V1 type is `join_simple`; V2.5 infers `simple_select`, so it retrieves single-table bond examples instead of the exact `bond JOIN connected` pattern.
- `european_football_2`, dev index 1058: gold/V1 type is `subquery`; V2.5 infers `ratio` due to "rate" and "average", so it retrieves average-rating division examples.

`analyze_question()` also over-promotes words like `highest`, `top`, `average`, `rate`, and `percent` into query types that can hide joins/subqueries. Natural language usually does not reveal whether the SQL needs joins; in BIRD, many "simple" questions require joins because labels live in lookup tables.

## Root Cause 2: Tier-1 V2.5 truncates the candidate set before scoring

For mature DBs (tier 1), V2.5 does:

```python
candidates = self.store.search(db_id=db_id, limit=80)
matches = [self._score_tier1(p, qfeatures) for p in candidates]
```

`PatternStoreV25.search()` orders by `match_count DESC, created_at DESC`, so for DBs with more than 80 patterns, older seeded examples are excluded before scoring. This is especially bad because all high-regression DBs are tier 1:

- `california_schools`: 104 V2.5 patterns
- `toxicology`: 157 V2.5 patterns
- `european_football_2`: 137 V2.5 patterns

Current candidate coverage on the 220 sample:

| Database | V1 exact in candidate | V2.5 tier-1 exact in candidate |
| --- | ---: | ---: |
| california_schools | 14/20 | 15/20 |
| toxicology | 12/20 | 5/20 |
| european_football_2 | 13/20 | 13/20 |

Even when the exact example exists in `patterns_v25`, it may not be in the 80 rows scored. For example:

- `california_schools`, dev index 21: exact V2.5 seed row exists (`agg_filter`) but is not in the tier-1 candidate set, so V2.5 returns unrelated `agg_filter` examples.
- `european_football_2`, dev index 1034: exact top-N seed row exists but is not scored; V2.5 retrieves "10 heaviest players" instead.
- `toxicology`, dev index 210: exact `bond JOIN connected` seed row exists but is not scored; V2.5 retrieves single-table bond examples.

This differs from V1, which first searches same DB plus same query type with a smaller but higher-precision pool.

## Root Cause 3: AST feature scoring rewards absent features and misses important structure

`_feature_similarity()` currently counts a "hit" when expected and actual booleans are equal:

```python
hits = [name for name, actual in checks if bool(expected_features.get(name)) == actual]
```

That means absent features dominate the score. A simple query with no `GROUP BY`, no `ORDER BY`, no `LIMIT`, and no ratio can get a high AST score for matching those absences, even if the important structure is wrong. The match reason string also labels these as feature hits, making debugging misleading.

The feature set omits or underweights the structures that most distinguish V1's useful examples:

- join count / has join
- table count
- subquery
- window function
- distinct
- having
- aggregate function family
- filter vs no filter with comparison/date predicates

Concrete failures:

- `california_schools`, dev index 80: exact pattern is `join_simple` with `ORDER BY/LIMIT`, but V2.5 infers `top_n`; unrelated top-N single-table examples outrank the exact join example. The exact example's rank is 8.
- `california_schools`, dev index 87: exact pattern is `join_simple`, but V2.5 infers `agg_filter`; simple count-filter examples outrank the exact email-selection join. The exact example's rank is 15.
- `toxicology`, dev index 210: the needed join structure is invisible to `analyze_question()`, and `_feature_similarity()` does not penalize using single-table examples strongly enough.

## Root Cause 4: Schema footprint matching is noisy and sometimes mismatched

`analyze_question()` builds table and column hints by token intersection. This creates many false column hints from generic words like `school`, `type`, `name`, `county`, `id`, and numbers. It also compares these hints against `extract_sql_features()` output that is inconsistent about qualification:

- SQL columns may be stored as `frpm.school type`, `schools.latitude`, or just `county name`.
- Question hints are usually fully qualified as `table.column`.

This can give exact examples low footprint scores and let unrelated examples win. For `california_schools`, dev index 80, the exact join pattern has only a 0.07 footprint score even though it contains the correct `frpm` and `schools` tables and the needed latitude/order pattern.

Because `_score_tier1()` gives footprint 35% of the final score, noisy hints have enough weight to distort ranking.

## Root Cause 5: V2.5 mutates memory differently during benchmark runs

V1's `record_match()` only increments an existing `(question, sql, db_id)` row:

```python
self.store.increment_match(question, sql, db_id)
```

V2.5's `record_match()` ingests the generated successful SQL first, then increments it:

```python
added = self.ingest(question, sql, db_id)
self.store.increment_match(question, sql, db_id)
```

This means V2.5 can add generated equivalent SQL variants during a run. The current V2.5 store has 1,639 rows versus V1's 1,533 seed rows. Those extra recent rows are ordered ahead of older seed rows by `created_at`, which worsens the tier-1 truncation problem. It also makes V1/V2.5 comparisons less controlled.

## Prioritized Fixes

### P0: Restore a V1-style high-precision same-DB retrieval path

Expected impact: highest. This directly addresses the 21 V1-correct/V2.5-wrong cases where V1's exact or near-exact example is often available.

Recommended change:

1. Keep V2.5 scoring as a secondary reranker, but first gather a broad candidate union:
   - same DB + inferred query type
   - same DB + V1-style fallback query type
   - same DB all patterns, with a large enough limit or no limit for mature DBs
   - exact/near-exact question term candidates
2. For benchmark comparability, optionally let `PatternMemoryV25.retrieve()` accept `query_type` from the caller, then pass `classify_query_type(gold_sql)` in `run_full_benchmark.py` when evaluating against V1.
3. Always include exact question matches and high lexical-overlap same-DB matches before applying score cutoffs.

Concrete implementation direction:

```python
def retrieve(self, question, db_id, top_k=3, query_type=None):
    qfeatures = analyze_question(question, profile)
    query_types = [query_type, qfeatures.query_type, self._dominant_db_type(db_id)]
    candidates = []
    for qt in unique_nonempty(query_types):
        candidates.extend(self.store.search(db_id=db_id, query_type=qt, limit=80))
    candidates.extend(self.store.search(db_id=db_id, limit=max(200, pattern_count)))
    ...
```

Do not truncate to 80 before scoring for tier-1 DBs.

### P1: Fix query-type inference and taxonomy alignment

Expected impact: high.

Recommended change:

- Add a V1-compatible SQL type classifier to V2.5 and keep stored `query_type` semantics aligned with V1 unless there is a clear reason to split them.
- In `classify_query_type_from_features()`, check joins before ratio, matching V1's behavior:
  - `subquery`
  - `join_agg`
  - `join_simple`
  - `ratio`
  - `top_n`
  - etc.
- In `analyze_question()`, stop treating `average` as ratio unless the question explicitly asks for rate/ratio/percentage/share.
- Mark comparative filter language as filtering:
  - `more than`, `less than`, `greater than`, `at least`, `at most`, `equal to`, `between`
- Preserve multiple query-type hypotheses rather than one hard label. Example: a question can be `top_n` plus `join_simple` candidate-compatible.

### P2: Replace boolean-equality AST scoring with positive-feature and penalty scoring

Expected impact: high.

Recommended change:

- Count positive expected features as hits only when present.
- Add explicit penalties for structural contradictions:
  - expected join but pattern has no join
  - expected subquery/window but pattern lacks it
  - expected top-N but pattern lacks `ORDER BY/LIMIT`
  - non-ranking question pattern has `ORDER BY/LIMIT` only if it otherwise matches strongly
- Add feature dimensions for `has_join`, `join_count_bucket`, `table_count_bucket`, `has_subquery`, `has_window`, `has_distinct`, and aggregate function family.

The current equality scoring makes absence look like evidence. It should become evidence only when the absence is discriminative and low-risk.

### P3: Make schema footprint scoring less noisy

Expected impact: medium to high, especially for `california_schools`.

Recommended change:

- Normalize both hinted columns and extracted columns into comparable forms:
  - full `table.column`
  - bare `column`
  - table name separately
- Downweight generic tokens (`id`, `name`, `type`, `school`, `player`, `count`, `date`) unless they match an exact column phrase.
- Give a stronger boost for exact table-set overlap and required table pairs.
- Do not let a huge list of weak column hints dilute a correct table match.

### P4: Make benchmark memory immutable or reset per run

Expected impact: medium for reproducibility; may indirectly improve retrieval by preventing recent generated variants from pushing seed rows out.

Recommended change:

- Add a `--readonly-memory` or `--no-memory-writeback` option to `run_full_benchmark.py`.
- For A/B comparisons, seed into a run-local temporary DB and disable online ingestion.
- If online learning is desired later, add generated successes into a separate table or mark source=`generated_success`, then cap their influence until validated against held-out questions.

### P5: Simplify prompt examples back toward V1

Expected impact: lower than retrieval fixes, but useful once retrieval is cleaner.

V1 prompt examples are just question, optional difficulty, and SQL. V2.5 adds:

```text
Pattern: {query_type}; tables=...
```

When retrieval is wrong, this metadata can amplify the wrong structure. Remove it or make it less directive. The model needs good SQL examples more than labels like `ratio` or `top_n`.

## Suggested Fix Order

1. Add `query_type=None` support to V2.5 retrieval and use gold SQL query type in the benchmark for apples-to-apples V1/V2.5 comparison.
2. Change tier-1 retrieval to score a broad same-DB candidate union rather than the newest 80 rows.
3. Align V2.5 SQL query-type classification with V1, especially join-before-ratio.
4. Rewrite `_feature_similarity()` so absent features do not dominate and joins/subqueries/window functions are scored.
5. Normalize schema footprint matching.
6. Disable V2.5 writeback during benchmark comparisons.

The first two fixes should recover most of the lost `california_schools`, `toxicology`, and `european_football_2` cases because they restore V1's core advantage: same-DB, same-structure examples are retrieved before broader heuristic scoring has a chance to distract the prompt.
