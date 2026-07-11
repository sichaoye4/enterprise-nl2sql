# NL2SQL Pipeline: Systematic Failure Analysis and Fixes

## Context
We have a BIRD benchmark pipeline that scored 44% EX (22/50) with DeepSeek V4 Flash
xhigh reasoning. The baseline (same model, different prompt) achieves 66% on 50 questions.
We need to close the gap and reach 75% on 100+ questions.

## Task 1: Analyze All 28 Failures
Read the benchmark results at `/tmp/phase2_bench_run1.json`. For each failing question:
1. Print: db_id, question, gold SQL, predicted SQL, predicted rows, gold rows, error
2. Categorize the failure type:
   - WRONG_TABLE: used wrong table for a column
   - WRONG_AGGREGATION: used COUNT(DISTINCT) instead of COUNT(*), etc.
   - WRONG_FILTER: wrong WHERE condition
   - WRONG_JOIN: wrong join condition or missing join
   - WRONG_COLUMN_ORDER: columns returned in wrong order
   - MISSING_SQL: no SQL generated
   - SQL_ERROR: SQL execution error
   - OTHER: describe the issue
3. Save the analysis to `/tmp/phase2_failure_analysis.json`

## Task 2: Compare Pipeline Prompt vs Baseline Prompt
The baseline script at `scripts/run_full_benchmark.py` achieves 66% EX.
Compare:
1. How the baseline builds the prompt (schema, evidence, few-shot)
2. How the pipeline builds the prompt (in `context_builder.py` when raw_schema is set)
3. Key differences that might explain the accuracy gap
4. Look at `scripts/bird_schema_context.py:build_schema_context()` - this is what the pipeline uses
5. Look at `scripts/run_full_benchmark.py:build_repair_prompt()` and the main prompt builder

## Task 3: Implement Fixes
Based on the analysis, implement fixes in `src/semantic_registry/pipeline/context_builder.py`
and `src/semantic_registry/pipeline/state_machine.py`. Focus on:
1. Making the prompt match the baseline's approach more closely
2. The baseline uses SQLPatternMemory for few-shot examples - check if this helps
3. The baseline has a repair loop that retries with error feedback - evaluate if re-enabling
   a lightweight repair (1 retry with error message) would help
4. Check if the evidence is being passed correctly to all questions

## Environment
- Repo: ~/enterprise-nl2sql
- Venv: source .venv/bin/activate
- API key: ~/.hermes/.env (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL)
- Tests: pytest tests/pipeline/ -q --tb=short
- Benchmark: python -u scripts/run_bird_70ti_benchmark.py --limit 50 --indices /tmp/phase2_indices.json --reasoning-effort xhigh --output /tmp/test.json
- BIRD data: bird_bench/dev/dev_20240627/

## Constraints
- Do NOT change the pipeline architecture
- Only modify existing files
- Run tests after each fix
- Keep changes minimal and focused
