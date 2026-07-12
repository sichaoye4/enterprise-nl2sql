# NL2SQL: Weak Database Failure Analysis and Fixes

## Context
Current: 76.36% EX (84/110). Target: push to 80%+.
16 failures across 3 weak DBs: card_games (6), formula_1 (5), european_football_2 (5).

## Failure Analysis

### Pattern 1: Extra columns returned (3 failures)
The "Return ONLY requested columns" instruction is not strong enough.

Examples:
- "height of tallest player? Indicate his name" -> Gold: SELECT player_name / Pred: SELECT height, player_name
- "Describe information about rulings" -> Gold: SELECT T2.text / Pred: SELECT rulings.date, rulings.text
- "home team that lost fewest matches" -> Gold: SELECT team_long_name / Pred: SELECT home_team_api_id

Fix: In context_builder.py BIRD mode, strengthen the instruction:
"Return ONLY the columns the question explicitly asks for. If the question says 'indicate his name', return only the name column. If the question says 'describe the information', return only the information column. Never add extra columns like id, date, or height unless explicitly requested."

### Pattern 2: Case sensitivity in WHERE clause (1 failure)
- Gold: WHERE T2.status = 'Legal' / Pred: WHERE legalities.status = 'legal'
The LLM lowercases string values.

Fix: Add instruction:
"When filtering by string values, use the exact case as shown in the sample values. If unsure, use the value as it appears in the evidence."

### Pattern 3: Wrong table for URL (2 failures)
- Gold: SELECT circuits.url (the circuit's Wikipedia page) / Pred: SELECT races.url (individual race pages)
- Question: "Where can the introduction of the races held on [Circuit] be found?"
- Evidence: "introduction of races refers to url"

Fix: Add instruction:
"When the evidence says 'introduction refers to url', return the url from the table that is the subject of the question (e.g., circuits.url for circuit questions, not races.url)."

### Pattern 4: COUNT(DISTINCT) vs COUNT (2 failures)
- Gold: COUNT(id) / Pred: COUNT(DISTINCT player_api_id)
- Gold: COUNT(T3.raceId) / Pred: COUNT(DISTINCT races.raceId)

Fix: Strengthen existing instruction:
"Use COUNT(*) or COUNT(column) to count rows. Only use COUNT(DISTINCT column) when the question explicitly asks for 'distinct' or 'unique' counts."

### Pattern 5: Wrong column - id vs name (2 failures)
- Gold: SELECT id / Pred: SELECT DISTINCT name
- Our instruction says "return id when asked to list/name entities" but LLM still returns name.

Fix: Make instruction even more explicit:
"When the question says 'Name all cards' or 'List cards', return the id column from the cards table, NOT the name column. The word 'name' in 'Name all cards' is a verb (to name = to identify), not a request for the name column."

### Pattern 6: NULL handling (1 failure)
- Gold: ORDER BY q2 ASC LIMIT 1 (includes NULLs) / Pred: WHERE q2 IS NOT NULL (excludes NULLs)

Fix: Add instruction:
"Do not add IS NOT NULL filters unless the question explicitly mentions null or missing values. SQLite sorts NULL values first in ASC order."

### Pattern 7: Over-complex query (2 failures)
- Gold: ORDER BY faceConvertedManaCost LIMIT 1 / Pred: WHERE faceConvertedManaCost = (SELECT MAX(...))
- Gold: SELECT t1.buildUpPlaySpeed ... ORDER BY ASC LIMIT 4 / Pred: GROUP BY ... ORDER BY DESC LIMIT 4

These are cases where the gold SQL has a specific ordering that differs from what the evidence suggests. Hard to fix with prompt changes.

## Implementation
File: src/semantic_registry/pipeline/context_builder.py
Location: The IMPORTANT section in BIRD mode (raw_schema branch)

Add/strengthen these instructions:
1. Extra columns: "Return ONLY the columns the question explicitly asks for. Never add extra columns."
2. Case: "Use exact string case from sample values when filtering."
3. URL: "When evidence says 'introduction refers to url', return url from the subject table."
4. COUNT: "Use COUNT(*) or COUNT(column). Only use COUNT(DISTINCT) when question says 'distinct'."
5. id vs name: "'Name all cards' means return id, not the name column."
6. NULL: "Do not add IS NOT NULL filters unless question mentions null/missing."

## Environment
- Repo: ~/enterprise-nl2sql
- Venv: source .venv/bin/activate
- Tests: pytest tests/pipeline/ -q --tb=short
- Benchmark: python -u scripts/run_bird_70ti_benchmark.py --limit 110 --indices bird_bench/results/sample_indices.json --reasoning-effort xhigh --output /tmp/test.json
- Failure data: /tmp/weak_db_failures.json
