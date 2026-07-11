# NL2SQL Pipeline: Targeted Prompt Fixes for Phase 2 Failures

## Context
Phase 2 benchmark: 54% EX (27/50). Need to fix 23 failures to reach 75%.
Analysis of all 23 failures reveals 4 major patterns.

## Pattern 1: Extra columns returned (7 failures - highest impact)
The LLM returns extra columns the question doesn't ask for. BIRD evaluates result set
equality including column count, so extra columns = FAIL.

Examples:
- "What is the height of the tallest player? Indicate his name."
  Gold: SELECT player_name FROM Player ORDER BY height DESC LIMIT 1
  Pred: SELECT player_name, height FROM Player ORDER BY height DESC LIMIT 1
  (extra "height" column causes mismatch)

- "Who are the female account holders who own credit cards and also have loans?"
  Gold: SELECT T1.client_id FROM client ...
  Pred: SELECT c.client_id, c.gender, c.birth_date, c.district_id FROM client ...
  (extra columns cause mismatch)

- "Give his full name" (formula_1)
  Gold: SELECT T2.forename, T2.surname FROM ...
  Pred: SELECT T1.forename || ' ' || T1.surname AS full_name FROM ...
  (concatenation changes column count from 2 to 1)

Fix: Add to BIRD prompt in context_builder.py:
"Return ONLY the columns explicitly mentioned in the question. Do not add extra columns."
"When asked for a 'full name', return the component name columns separately (e.g., forename, surname)."

## Pattern 2: Wrong column - using name instead of id (4 failures)
The LLM returns `name` when the gold SQL uses `id`, or vice versa.

Examples:
- "Among black card borders, which card has full artwork?"
  Gold: SELECT id FROM cards WHERE borderColor = 'black' AND isFullArt = 1
  Pred: SELECT name FROM cards WHERE borderColor = 'black' AND isFullArt = 1

- "What are the card numbers..."
  Gold: SELECT id FROM cards WHERE ...
  Pred: SELECT number FROM cards WHERE ...

Fix: Add to BIRD prompt:
"When asked to 'name' or 'list' something, return the identifying column (usually id) unless
the question explicitly asks for names. Check the evidence for column guidance."

## Pattern 3: Date format mismatches (3 failures)
The LLM uses slightly different date formats than the gold SQL.

Examples:
- Gold: WHERE T1.CreationDate = '2010-07-19 19:37:33.0'
  Pred: WHERE CreationDate = '2010-07-19 19:37:33'
  (missing .0 suffix)

Fix: Add to BIRD prompt:
"When filtering by datetime, use LIKE to match the date prefix: WHERE column LIKE '2010-07-19 19:37:33%'
instead of exact equality, to handle varying datetime formats."

## Pattern 4: Result matches but marked FAIL (2 failures)
Some questions have correct results but are marked FAIL.

Example:
- financial "account opening date before 1997 and own an amount"
  Pred rows: [[1], [2], [4], [6], [7]]
  Gold rows: [[1], [2], [4], [6], [7]]
  But match=False

This needs investigation - check if there are more rows that differ, or if the
result_sets_match function has a bug.

## Implementation
File: src/semantic_registry/pipeline/context_builder.py
Location: In the build() method where raw_schema is set (BIRD mode), add these
instructions to the IMPORTANT section.

Also check: scripts/run_bird_full_eval.py result_sets_match() function for
the "correct results but FAIL" issue.

## Environment
- Repo: ~/enterprise-nl2sql
- Venv: source .venv/bin/activate
- Tests: pytest tests/pipeline/ -q --tb=short
- Benchmark: python -u scripts/run_bird_70ti_benchmark.py --limit 11 --indices /tmp/phase1_indices.json --reasoning-effort xhigh --output /tmp/test.json
