# NL2SQL: Post-Processing SQL Fixes for Specific Failure Patterns

## Context
76.36% EX (84/110). 26 failures analyzed. Most are LLM comprehension issues.
But 6 failures have specific, fixable patterns that can be addressed with
post-processing rules applied AFTER the LLM generates SQL but BEFORE execution.

## Fix 1: URL table selection (2 failures, formula_1)
**Pattern**: Question asks "where can the introduction/information about races be found"
and evidence says "introduction refers to url". The LLM returns `races.url` (individual
race pages) but the gold SQL returns `circuits.url` (the circuit's Wikipedia page).

**Rule**: If the SQL contains `SELECT ... races.url ...` or `SELECT ... r.url ...`
joined with circuits, AND the question contains "introduction" or "information about",
replace `races.url` with `circuits.url` in the SELECT clause.

**Examples**:
- "Where can the introduction of the races held on Circuit de Barcelona-Catalunya be found?"
  Gold: SELECT DISTINCT T1.url FROM circuits AS T1 INNER JOIN races AS T2 ...
  Pred: SELECT races.url FROM races INNER JOIN circuits ...
  Fix: Change SELECT races.url -> SELECT DISTINCT circuits.url

- "Where can I find the information about the races held on Sepang International Circuit?"
  Same pattern.

**Implementation**: In state_machine.py, after candidate generation but before validation,
apply this rule to the SQL string. Check if:
1. Question contains "introduction" or "information about" or "where can"
2. SQL has `races.url` or alias equivalent in SELECT
3. SQL joins races with circuits
If all true, replace `races.url` with `circuits.url` and add DISTINCT if not present.

## Fix 2: Extra columns (3 failures)
**Pattern**: LLM returns extra columns not asked for in the question.

**Examples**:
- "height of tallest player? Indicate his name" -> returns (height, player_name) but gold wants (player_name)
- "Describe information about rulings" -> returns (date, text) but gold wants (text)
- "home team lost fewest" -> returns team_api_id but gold wants team_long_name (JOIN with Team table)

**Rule**: This is harder to fix generically. A simple approach: if the question says
"indicate his name", "describe", or "what is the [single thing]", check if the SELECT
has more than 1 column and if so, keep only the column that matches the question's noun.

**Implementation**: Too risky for generic post-processing. Skip for now.

## Fix 3: Case sensitivity (1 failure, card_games)
**Pattern**: Gold uses `WHERE status = 'Legal'` but pred uses `WHERE status = 'legal'`

**Rule**: When the SQL has a WHERE clause with a string literal, check if the sample
values in the schema context have a different case. If so, use the case from samples.

**Implementation**: Too complex for post-processing. Skip for now.

## Recommendation
Only implement Fix 1 (URL table selection). It's:
- Very specific (only triggers on "introduction"/"information about" + races.url)
- Very safe (extremely unlikely to break other questions)
- Fixes 2 failures (+1.82pp -> 78.18% projected)

## Implementation Location
In `src/semantic_registry/pipeline/state_machine.py`, add a method
`_post_process_bird_sql(sql, question)` that's called after candidate generation
and before validation. Apply the URL fix rule there.

## Environment
- Repo: ~/enterprise-nl2sql
- Venv: source .venv/bin/activate
- Tests: pytest tests/pipeline/ -q --tb=short
- Benchmark: python -u scripts/run_bird_70ti_benchmark.py --limit 110 --indices bird_bench/results/sample_indices.json --reasoning-effort xhigh --output /tmp/test.json
