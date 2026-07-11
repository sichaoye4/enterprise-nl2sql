# Full Pipeline BIRD Benchmark — Compare Baseline vs Semantic-Enhanced

## Context

We have:
- 11 BIRD databases with 1534 questions at `~/enterprise-nl2sql/bird_bench/dev/dev_20240627/`
- 11 semantic models at `~/enterprise-nl2sql/bird_semantic_engine/*/model.yml`
- Previous baseline results show 66-91% EX with V4 Flash few-shot (see `~/enterprise-nl2sql/bird_bench/results/full_benchmarks/`)
- API keys in `~/.hermes/.env` (DEEPSEEK_API_KEY for SQL generation, DASHSCOPE for Qwen judge)
- The `scripts/bird_eval.py` script has the baseline infrastructure

## What we need to measure

### Baseline (no semantic engine)
Re-run the baseline: DeepSeek V4 Flash generates SQL directly from schema prompts. 
Use the existing scripts/bird_eval.py as reference but run on debit_card_specializing (64 questions) for quick comparison.

### Full pipeline (with semantic engine + router + LLM fallback + judge)
The pipeline stages:
1. classify → run_semantic_engine → quality_gate → run_semantic_llm_router
2. If any of these produce SEMANTIC_SQL → compile SQL, run judge
3. If CLARIFY or judge rejects → LLM generates SQL (DeepSeek V4 Flash, same prompt as baseline)
4. validate → repair → select → judge → explain → response

Key: the LLM fallback should use the EXACT same generation approach as the baseline, so the only difference is whether the semantic engine contributed.

### Critical: do NOT use "mock router" — use the real pipeline
Do not extract ideal filters from gold SQL. The real pipeline:
- First runs the semantic engine's standard process()
- If it returns SEMANTIC_SQL → use it (it's deterministic)
- If it returns CLARIFY → try the LLM semantic router (real LLM call, not mock)
- If router fails → LLM fallback

### What to compare
For each question, record:
- route (SEMANTIC_SQL, GUARDED_LLM_SQL, CLARIFY, BLOCKED)
- generated SQL
- gold SQL
- EX (execution accuracy vs gold)
- error message if any

### Implementation approach

The simplest path: use the existing NL2SQLPipeline with the NL2SQLPipeline.run(question, domain=db_id) method.

However, the pipeline expects "registry_data" which is business-specific. For BIRD, we need to either:
- Create a minimal SemanticRegistryData for each BIRD DB, OR
- Bypass stages that need registry_data and directly test the semantic engine + router + LLM

Actually the cleanest approach: write a standalone benchmark script that:
1. Initializes the NL2SQLPipeline with:
   - semantic_engine pointing to the BIRD model
   - semantic_model_path pointing to the bird_semantic_engine/{db_id} directory
   - semantic_dir pointing to the bird_semantic_engine directory
   - candidate_generator with DeepSeekProvider (same model as baseline)
   - llm_judge with DashScopeLLMClient (Qwen 3.5-plus)
2. For each question in debit_card_specializing (start with just this DB):
   a. Call pipeline.run()
   b. Extract the result
   c. Execute against SQLite
   d. Compute EX vs gold SQL
3. Compare with the historical baseline (66% on 50 questions, 90% on 220 with indices)

### Key files:
- `~/enterprise-nl2sql/src/semantic_registry/pipeline/state_machine.py` — the pipeline
- `~/enterprise-nl2sql/src/semantic_registry/pipeline/llm_gateway.py` — DeepSeekProvider
- `~/enterprise-nl2sql/src/semantic_registry/pipeline/semantic_judge.py` — LLMJudge with Qwen
- `~/enterprise-nl2sql/src/semantic_registry/pipeline/semantic_router.py` — SemanticRouter
- `~/enterprise-nl2sql/scripts/bird_eval.py` — existing baseline eval
- `~/enterprise-nl2sql/bird_semantic_engine/debit_card_specializing/model.yml` — semantic model for debit
- `~/enterprise-nl2sql/bird_bench/dev/dev_20240627/dev.json` — questions + gold SQL
- `~/.hermes/.env` — API keys (DEEPSEEK_API_KEY, DASHSCOPE_API_KEY)

### Expected outcomes
The full pipeline should be >= baseline EX, with the semantic engine providing a small uplift for questions that match governed measures. The judge prevents wrong semantic SQL from degrading results.

### Dialect note
DeepSeek generates SQLite SQL. The semantic compiler generates PostgreSQL SQL (%s placeholders, DATE_TRUNC). For fair comparison:
- Convert %s to ? for SQLite
- Skip queries with DATE_TRUNC / NOW() that can't run on SQLite
- Run all SQL against the SQLite database
