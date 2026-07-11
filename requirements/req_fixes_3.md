# Fix 3 Issues Preventing Perfect Router-Based SQL

## The 3 Problems

### Problem 1: Measure built-in filters cause double-filtering (CASE WHEN + WHERE)

When the LLM router selects a measure AND provides explicit WHERE filters, the compiled SQL has both:
- A CASE WHEN wrapper inside the aggregation (inherited from the measure's built-in filters)
- A WHERE clause with the router's own filters

Example: `count_gasstationid` measure has a built-in filter `Country = %s`. The router adds `WHERE Country = 'CZE'`. Result: `COUNT(CASE WHEN Country = 'CZE' THEN GasStationID END) WHERE Country = 'CZE'` — redundant filtering that changes the result.

**Fix**: When the router provides filters, strip the measure's built-in filters from the snapshot before compiling. Create a modified copy of the SemanticModelSnapshot with the specific measure's `.filters` cleared to `[]`.

### Problem 2: Missing filter operators (LIKE, BETWEEN, >, <, >=, <=)

The `_predicate()` method in `sql_compiler.py` only supports: `equals`, `not_equals`, `last_n_*`. Missing operators:
- `like` → `LIKE %s`
- `not_like` / `not like` → `NOT LIKE %s`
- `contains` → `LIKE '%%s%'`
- `starts_with` → `LIKE 's%'`
- `ends_with` → `LIKE '%s'`
- `gt` / `greater_than` → `> %s`
- `gte` / `>=` → `>= %s`
- `lt` / `less_than` → `< %s`
- `lte` / `<=` → `<= %s`
- `between` → `BETWEEN %s AND %s`

### Problem 3: Router doesn't support diverse filter operators in prompt

The router prompt in `build_router_prompt` only mentions `"operator": "equals"`. It needs to:
- List all supported operators in the prompt
- For date patterns ("between August and November 2013", "January 2012"), instruct the router to use `like`, `between`, or `starts_with` operators
- Update `SUPPORTED_FILTER_OPERATORS` in semantic_router.py
- Update `_validate_choice` to accept new operators
- Update `_RouterResponse` validation to allow new operators

## Requirements

In `semantic_router.py`:
1. Expand `SUPPORTED_FILTER_OPERATORS` to include: `equals`, `not_equals`, `not equals`, `like`, `not_like`, `not like`, `contains`, `starts_with`, `ends_with`, `gt`, `greater_than`, `gte`, `>=`, `lt`, `less_than`, `lte`, `<=`, `between`
2. Update `_validate_choice` to accept the expanded operators
3. Update `build_router_prompt` to list the supported operators and give examples for date patterns
4. Add `_strip_measure_filters(snapshot, measure_name)` helper that deep-copies the snapshot and clears the specified measure's `.filters`
5. In `compile_from_router`, when `router_result.filters` is non-empty, call `_strip_measure_filters` before `compile_sql` so the measure's built-in filters don't cause double-filtering

In `sql_compiler.py`:
6. Add `like`, `not_like`, `not like`, `contains`, `starts_with`, `ends_with`, `gt`/`greater_than`, `gte`/`>=`, `lt`/`less_than`, `lte`/`<=`, `between` operator handling in `_predicate()`

In `test_semantic_router.py`:
7. Update test for `SUPPORTED_FILTER_OPERATORS` to match the new set
8. Add test for stripped measure filters when router provides its own

## Dialect Note

The compiler targets PostgreSQL (`NOW()`, `INTERVAL '1 day'`). For the BIRD SQLite benchmark:
- Only `equals`, `like`, `not_like`, `contains`, `starts_with`, `ends_with` are safe across dialects
- `between` is also safe across dialects
- Comparison operators (`>`, `<`, etc.) are safe across dialects
- No PostgreSQL-specific syntax needed for these operators

## Files to Modify
- ~/enterprise-nl2sql/src/semantic_registry/pipeline/semantic_router.py
- ~/semantic_modeling/src/semantic_engine/compiler/sql_compiler.py
- ~/enterprise-nl2sql/tests/pipeline/test_semantic_router.py

## Verification
After the fix, the 3 router e2e test cases should compile correctly:
1. "How many gas stations in CZE have Premium gasoline?" → COUNT(GasStationID) WHERE Country='CZE' AND Segment='Premium' (no CASE WHEN)
2. "How much did customer 6 consume in total between August and November 2013?" → SUM(Consumption) WHERE CustomerID=6 AND Date BETWEEN '2013-08-01' AND '2013-11-30' (or similar)
3. "What was the average total price of transactions that occurred in January 2012?" → AVG(Amount) WHERE Date LIKE '2012-01%'
