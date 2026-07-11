# LLM SQL Judge — Cross-Model Gated Validation

## Background

The NL2SQL pipeline generates SQL through two paths:
- **SEMANTIC_SQL**: deterministic compilation from governed measures (no retry, same result)
- **CLARIFY/LLM path**: LLM generates SQL candidates (retry helps, different result each time)

Currently only syntactic/structural validation exists (static parser, table/column permissions). There is no semantic check that answers: "Does this SQL actually answer the user's question?"

## Solution: LLM Judge Stage

A new pipeline stage `run_llm_judge` inserted between `select` and `explain`:

```
select → llm_judge → (pass) → explain → build_response
                  → (fail) → retry LLM generation (up to 3x) → validate → select → judge again
                  → (fail after 3 retries) → explain with warning
```

### Model diversity
- **Generator** uses the pipeline's default LLM (DeepSeek V4 Flash via llm_gateway)
- **Judge** uses Qwen 3.5-plus via DashScope API (different provider, different model family)
- This prevents same-model confirmation bias

### Judge prompt
The judge receives:
1. Original user question
2. Generated SQL
3. Route type (SEMANTIC_SQL or LLM-generated)
4. Semantic plan (measure, filters, dimensions) — if available

The judge outputs structured JSON:
```json
{
  "pass": true/false,
  "reasoning": "Brief explanation of why this SQL does/doesn't answer the question",
  "confidence": 0.0-1.0
}
```

### Retry logic

- **SEMANTIC_SQL route**: judge runs but does NOT trigger retry (deterministic — same SQL each time). If fail, accept with a warning flag.
- **LLM path**: if judge fails and retry_count < 3:
  1. Increment retry_count
  2. Clear selected_sql and sql_candidates
  3. Inject judge's reasoning into the context_prompt as "Previous attempt feedback: ..."
  4. Re-run generate_candidates → validate → select → judge again
- After 3 failures: accept with warning (don't block user permanently)

### What to modify

**New file: `src/semantic_registry/pipeline/semantic_judge.py`**
- `DashScopeLLMClient` — wraps DashScope OpenAI-compatible API (qwen3.5-plus)
  - Reads config from `~/.hermes/skills/mlops/multimodal-vision/config.json` (api_key, base_url, model)
  - Falls back to env var `DASHSCOPE_API_KEY` if config not found
- `JudgeResult` dataclass — pass, reasoning, confidence
- `build_judge_prompt(question, sql, route_type, semantic_plan)` — builds the judge prompt
- `parse_judge_response(text)` — extracts structured output
- `LLMJudge` class — `judge(question, sql, route_type, semantic_plan) -> JudgeResult`

**Modified: `src/semantic_registry/pipeline/state_machine.py`**
- Add `llm_judge_retry_count: int = 0` and `llm_judge_result: dict | None` to `PipelineContext`
- Add `run_llm_judge` stage to the stages list (after `select`, before `explain`)
- In `_should_skip_stage`: for SEMANTIC_SQL path, don't skip judge (it still reviews, just doesn't retry)
- In `run_llm_judge`:
  - Build judge client with DashScope config
  - Call judge on selected_sql
  - If pass → proceed
  - If fail + retries < 3 + not SEMANTIC_SQL:
    - Re-inject context with judge feedback
    - Re-run generate_candidates
    - Re-run validate, select
    - Re-run judge (recursive entry? or loop in the stage?)
  - If fail + retries >= 3 or SEMANTIC_SQL → accept with warning flag

**Modified: `src/semantic_registry/pipeline/llm_gateway.py`**
- No changes needed — the judge uses its own DashScope client, not the llm_gateway

### Retry flow in detail

```python
def run_llm_judge(self, context):
    if not context.selected_sql:
        return context
    
    judge = LLMJudge(...)
    result = judge.judge(context.question, context.selected_sql.sql, 
                         context.semantic_route, context.semantic_plan)
    
    if result.pass:
        context.llm_judge_result = {"pass": True, "reasoning": result.reasoning}
        return context
    
    # Failed
    context.llm_judge_result = {"pass": False, "reasoning": result.reasoning}
    
    if context.semantic_route == "SEMANTIC_SQL":
        # Deterministic — accept with warning
        context.selected_sql.reasoning_summary += f" [Judge: {result.reasoning}]"
        return context
    
    if context.llm_judge_retry_count >= 3:
        # Max retries — accept with warning
        return context
    
    # Retry: inject judge feedback, regenerate
    context.llm_judge_retry_count += 1
    context.selected_sql = None
    context.sql_candidates = []
    # Inject judge feedback into the context prompt for the next generation
    feedback = f"The previous SQL attempt was rejected. Reason: {result.reasoning}. Review your approach and try again."
    # Re-run LLM pipeline stages
    context = self.generate_candidates(context)
    context = self.validate(context)
    context = self.repair(context)
    context = self.select(context)
    # Re-enter judge (recursive)
    return self.run_llm_judge(context)
```

### Files to modify
- NEW: `~/enterprise-nl2sql/src/semantic_registry/pipeline/semantic_judge.py`
- MODIFY: `~/enterprise-nl2sql/src/semantic_registry/pipeline/state_machine.py`
- NEW: `~/enterprise-nl2sql/tests/pipeline/test_semantic_judge.py`

### Testing
1. Unit test: judge accepts correct SQL, rejects wrong SQL
2. Unit test: judge retry flow (mock judge, verify retry count)
3. Unit test: DashScope client config loading
4. Integration test: judge with real DashScope API call (can be skipped if no API key)

### Dialect note for static validator
While investigating the static validator: it uses `sqlglot.parse_one()` with default dialect `spark`. This DOES catch syntax errors properly (sqlglot is a real SQL parser). However, for the BIRD benchmark targeting SQLite, the dialect should be `sqlite` not `spark`. This is a separate concern from the judge.

## Config reference
DashScope OpenAI-compatible API:
- base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
- model: qwen3.5-plus
- Config file: ~/.hermes/skills/mlops/multimodal-vision/config.json
- Expected format: {"api_key": "...", "model": "qwen3.5-plus", "base_url": "...", "max_tokens": 4096}
