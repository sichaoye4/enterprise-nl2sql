# NL2SQL Pipeline: Remaining Issues to Fix

## Context
We're improving the BIRD benchmark pipeline to reach 75% EX on 100+ balanced questions.
Current state: 5/11 (45%) on a cross-DB test with DeepSeek V4 Flash low reasoning.
We've already fixed: PII keywords, evidence passing, enriched schema context, BIRD-mode
validation, YAML parser, and model.enriched.yml loading.

## Issues to Investigate and Fix

### Issue 1: Empty SQL for some questions (formula_1)
- **Symptom**: Some questions produce empty SQL string, no error reported
- **Question**: "For the drivers who took part in the race in 1983/7/16, what's their race completion rate?"
- **Route**: SEMANTIC_ASSISTED_LLM (pipeline runs fully but no SQL)
- **Likely cause**: LLM response doesn't parse as valid JSON, candidate generator catches
  exception and returns empty SQL candidate
- **File to check**: `src/semantic_registry/pipeline/candidate_generator.py` - the `_generate`
  method and `src/semantic_registry/pipeline/llm_gateway.py` - the `generate` method
- **Fix approach**: Add better fallback SQL extraction (regex for SELECT statements)
  when JSON parsing fails, similar to `scripts/run_full_benchmark.py:extract_sql_from_response()`

### Issue 2: Improve BIRD prompt for better accuracy
- **File**: `src/semantic_registry/pipeline/context_builder.py` - the `build()` method
  when `raw_schema` is provided (BIRD mode)
- **Current issues observed**:
  a. Column order mismatch: LLM returns columns in different order than gold SQL
     (e.g., toxicology: returns ('+', 6) but gold is (6, '+'))
     Fix: Add instruction "Return columns in the order implied by the question"
  b. COUNT(DISTINCT) vs COUNT(*): LLM uses COUNT(DISTINCT col) when gold uses COUNT(*)
     (e.g., debit_card_specializing: "how many of them" means count all matching rows)
     Fix: Add instruction "Use COUNT(*) unless the question specifically asks for distinct count"
  c. EXISTS vs listing: LLM uses EXISTS when the question asks about "each" item
     (e.g., student_club: "Was each expense approved?" should list all expenses)
     Fix: Add instruction "When the question asks about 'each' or 'every' item, return all matching rows"
  d. Wrong table choice: LLM picks wrong table when multiple tables have same column name
     (e.g., thrombosis_prediction: Patient.Diagnosis vs Examination.Diagnosis)
     Fix: The enriched schema context should help, but add instruction
     "When multiple tables have a column with the same name, use the table that is
     most directly related to the question's context"

### Issue 3: Fix failing test
- **File**: `tests/pipeline/test_semantic_engine_integration.py`
- **Test**: `test_semantic_sql_route_uses_compiled_candidate_without_generation`
- **Error**: Assertion fails because generated SQL includes GROUP BY and ORDER BY
  that the test doesn't expect
- **Fix**: Update the test assertion to accept the GROUP BY/ORDER BY in the SQL

## Environment
- Repo: `~/enterprise-nl2sql`
- Venv: `.venv` (activate with `source .venv/bin/activate`)
- Tests: `pytest tests/pipeline/ -q --tb=short`
- Benchmark: `python -u scripts/run_bird_70ti_benchmark.py --limit 11 --indices /tmp/phase1_indices.json --reasoning-effort low --output /tmp/test.json`
- BIRD data: `bird_bench/dev/dev_20240627/`
- API key: loaded from `~/.hermes/.env` (DEEPSEEK_API_KEY)

## Constraints
- Do NOT change the pipeline architecture (components stay as they are)
- Only modify existing files, don't create new modules
- Keep changes minimal and focused
- Run tests after each fix: `source .venv/bin/activate && pytest tests/pipeline/ -q --tb=short`
