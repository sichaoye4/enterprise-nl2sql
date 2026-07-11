# Phase 2: Add E2E LLM Trace Logging to Pipeline

## Goal
For every query through the NL2SQL pipeline, capture ALL prompts sent to LLM APIs and ALL LLM API responses, making them accessible for debugging, auditing, and quality analysis.

## What Logging Means

Not just logging to stdout — structure the data as `llm_trace` entries on `PipelineContext`, so callers (API endpoints, eval runners, benchmark scripts) can inspect or persist them. Each LLM interaction gets a named stage entry with prompt + response.

## Stages That Touch LLM APIs

| Stage name | Location | What's sent | What's received |
|---|---|---|---|
| `semantic_router` | `state_machine.py:_llm_router_generate()` | `build_router_prompt()` output | LLM's raw text response |
| `candidate_a` | `candidate_generator.py:_generate("A", ...)` | `context.context_prompt` (after all injections) | `llm_gateway.generate()` response (LLMResponse model) |
| `candidate_b` | `candidate_generator.py:_generate("B", ...)` | `_plan_first_prompt(context.context_prompt)` | Same |
| `llm_judge` | `semantic_judge.py:LLMJudge.judge()` | `build_judge_prompt()` output | `DashScopeLLMClient.generate()` output (raw text) |
| `fallback_candidate_a` | `state_machine.py:_retry_after_judge_failure()` then `generate_candidates()` | Updated `context.context_prompt` (with judge feedback) | Same as candidate_a |
| `fallback_candidate_b` | Same | Updated + plan_first variant | Same as candidate_b |

## Required Changes

### 1. `src/semantic_registry/pipeline/state_machine.py` — Add `llm_trace` to PipelineContext

```python
# Add field to PipelineContext:
llm_trace: dict[str, dict[str, str | None]] = Field(default_factory=dict)
```

Where key = stage name (e.g. `"semantic_router"`, `"candidate_a"`, `"candidate_b"`, `"llm_judge"`), value = `{"prompt": str | None, "response": str | None}`.

### 2. Capture at each LLM call point

#### a) `_llm_router_generate()` (line ~868)
Before calling LLM, record the prompt. After getting response, record it:
```python
def _llm_router_generate(self, prompt: str, context: PipelineContext | None = None) -> str:
    # ... existing system_prompt building ...
    
    # Record prompt before call
    stage = {}
    if context is not None:
        context.llm_trace["semantic_router"] = {"prompt": prompt, "response": None}
    
    # ... existing LLM call ...
    
    # Record response
    if context is not None:
        context.llm_trace["semantic_router"]["response"] = str(raw)
    
    return raw if isinstance(raw, str) else ...
```

But wait — `_llm_router_generate` is called from `run_semantic_llm_router()` which has access to `context`. We need to pass context through. Change the call site in `run_semantic_llm_router`:
```python
router = SemanticRouter(snapshot, lambda p: self._llm_router_generate(p, context))
```

#### b) `generate_candidates()` stage (line ~528)
`CandidateGenerator.generate_candidates()` calls `_generate()` which calls `llm_gateway.generate()`. The easiest approach:

In `state_machine.py:generate_candidates()`, after calling `self.candidate_generator.generate_candidates(context)`, inspect the `context.context_prompt` and add entries for candidate_a and candidate_b.

Actually, the candidate generator is where the actual LLM call happens. Let me think about this differently.

The `CandidateGenerator._generate()` creates the prompt from `context.context_prompt` (or plan_first variant), calls `llm_gateway.generate(prompt)`, and returns a `SQLCandidate`. The raw prompt and response aren't persisted.

Better approach: modify `CandidateGenerator._generate()` to also return the prompt/response, OR modify the `generate_candidates()` method in state_machine to capture them.

Simplest approach: In `state_machine.py:generate_candidates()`, capture the context_prompt before delegating:
```python
def generate_candidates(self, context: PipelineContext) -> PipelineContext:
    context.trace.append("generate_candidates")
    
    # Capture candidate prompts
    prompt_a = context.context_prompt or ""
    prompt_b = self.candidate_generator._plan_first_prompt(prompt_a)
    context.llm_trace["candidate_a"] = {"prompt": prompt_a, "response": None, "status": "pending"}
    context.llm_trace["candidate_b"] = {"prompt": prompt_b, "response": None, "status": "pending"}
    
    context.sql_candidates = self.candidate_generator.generate_candidates(context)
    
    # Capture responses from SQLCandidates
    for candidate in context.sql_candidates:
        stage = f"candidate_{candidate.candidate_id.lower()}"
        if stage in context.llm_trace:
            context.llm_trace[stage]["response"] = json.dumps(candidate.model_dump(mode="json"))
            context.llm_trace[stage]["status"] = "success" if candidate.sql else "failed"
    
    return context
```

#### c) `LLMJudge.judge()` (in semantic_judge.py)
In `state_machine.py:_judge_selected_sql()`:
```python
def _judge_selected_sql(self, context: PipelineContext) -> Any | None:
    # ... build judge_context ...
    
    # Capture judge prompt
    judge_prompt = build_judge_prompt(context.question, context.selected_sql.sql, context.semantic_route or context.selected_sql.generation_strategy, judge_context)
    context.llm_trace["llm_judge"] = {"prompt": judge_prompt, "response": None}
    
    try:
        result = self.llm_judge.judge(
            context.question, context.selected_sql.sql,
            context.semantic_route or context.selected_sql.generation_strategy,
            judge_context,
        )
        # Capture judge response
        context.llm_trace["llm_judge"]["response"] = json.dumps(result.model_dump() if hasattr(result, "model_dump") else {"pass": result.pass_, "reasoning": result.reasoning, "confidence": result.confidence})
        return result
    except Exception as exc:
        context.llm_trace["llm_judge"]["response"] = f"ERROR: {exc}"
        raise
```

Wait, `_judge_selected_sql` currently builds `judge_context` but doesn't call `build_judge_prompt()` — that's internal to `LLMJudge.judge()`. We need to either:
- Refactor `LLMJudge.judge()` to expose the prompt, OR
- Rebuild the prompt in `_judge_selected_sql()` for logging purposes

The cleanest way: capture inside `_judge_selected_sql`. Since `judge_context` is dict, we can reconstruct:
```python
judge_payload = {
    "question": context.question,
    "generated_sql": context.selected_sql.sql,
    "route_type": context.semantic_route or context.selected_sql.generation_strategy,
    "semantic_plan": judge_context,
}
```
Then log the serialized form.

Actually, let me take a different approach. Don't refactor the judge class — just capture in the caller.

For the judge, in `_judge_selected_sql()`:
```python
context.llm_trace["llm_judge"] = {
    "prompt": f"question: {context.question}\nsql: {context.selected_sql.sql}\nroute: {context.semantic_route or context.selected_sql.generation_strategy}",
    "response": None
}
# ... call judge ...
context.llm_trace["llm_judge"]["response"] = json.dumps(result.model_dump())
```

Hmm, but that's not the exact prompt. The user specifically asked for "all the prompts prepared for llm call and llm api response". Let me be more precise.

Actually, let me take the approach of logging the FULL judge prompt: just call `build_judge_prompt` in the state_machine and use it.

OK, let me simplify the requirements and just describe what to do.

#### d) Fallback/judge retry case
When `_retry_after_judge_failure()` regenerates candidates, the new prompts should also be captured. In `_retry_after_judge_failure()`, after generating candidates, add `"fallback_a"` and `"fallback_b"` entries.

## Files to Change

1. **`src/semantic_registry/pipeline/state_machine.py`** — PipelineContext.llm_trace field + capture in:
   - `run_semantic_llm_router()` (router prompt)
   - `generate_candidates()` (candidate prompts)
   - `_judge_selected_sql()` (judge prompt)
   - `_retry_after_judge_failure()` (fallback prompts)
   - `_llm_router_generate()` signautre update to accept optional context

2. **`src/semantic_registry/pipeline/semantic_judge.py`** (optional) — no changes needed, capture in caller

3. **`src/semantic_registry/pipeline/candidate_generator.py`** (optional) — no changes needed, capture in state_machine

## Key Design Decisions
- **Don't add DB persistence** — the llm_trace stays on PipelineContext. Callers (API, eval runner) can persist it as needed.
- **Capture the ACTUAL prompt text** sent to the LLM, not a summary or hash.
- **Capture the RAW response text** from the LLM, not a parsed model. For parsed responses (SQLCandidates, RouterResult), serialize the full object.
- **Prefix retry entries** with `retry_` to distinguish from first attempts.
