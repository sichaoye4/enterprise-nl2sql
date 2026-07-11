# Model Configuration Change

## Goal
- Router: DeepSeek V4 Flash (keep as is)
- Generation: DeepSeek V4 Pro with high reasoning effort
- Judge: DeepSeek V4 Pro with high reasoning effort

## Changes needed

### 1. `state_machine.py`
- Add `router_llm_gateway: Any | None = None` parameter to `NL2SQLPipeline.__init__`
- Store it as `self.router_llm_gateway`
- In `_llm_router_generate`, check `self.router_llm_gateway` first, fall back to `self.candidate_generator.llm_gateway`

### 2. `semantic_judge.py`
- `LLMJudge.__init__` already accepts any client with `.generate(prompt)`
- `DashScopeLLMClient` has `.generate(prompt)` that returns str
- `DeepSeekProvider` also has `.generate(prompt)` that returns str
- But `DeepSeekProvider.generate()` has a hardcoded SQL system prompt
- Solution: `LLMJudge` already passes the prompt to `self.client.generate(prompt)`, and `DeepSeekProvider.generate_text(prompt, system_prompt=...)` is available
- I'll modify `LLMJudge` to detect `DeepSeekProvider` and call `generate_text` with the judge system prompt instead

Actually, simplest approach: modify `LLMJudge.judge()` to build its OWN client call instead of delegating to `self.client.generate()`. Or create a thin wrapper class that adapts DeepSeekProvider for judge use.

### 3. Script to run benchmark with new config
Create a script that:
1. Creates `router_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-flash"))`
2. Creates `generation_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-pro", reasoning_effort="high"))`
3. Creates `judge_client = DeepSeekProvider(model="deepseek-v4-pro", reasoning_effort="high")`
4. Creates `judge = LLMJudge(client=judge_client)` — but need to fix the system prompt
5. Creates pipeline with these components
6. Runs on BIRD questions

For the judge, I need `DeepSeekProvider` to use a judge-specific system prompt. I'll use `generate_text(prompt, system_prompt=...)` instead of `generate(prompt)`.

The simplest fix: modify `LLMJudge.judge()` to call `self.client.generate_text(prompt, system_prompt=...)` if the client has `generate_text`, otherwise fall back to `self.client.generate(prompt)`.

### Files to modify
1. `~/enterprise-nl2sql/src/semantic_registry/pipeline/state_machine.py` — add router_llm_gateway
2. `~/enterprise-nl2sql/src/semantic_registry/pipeline/semantic_judge.py` — support DeepSeekProvider as judge client
3. NEW: `~/enterprise-nl2sql/scripts/run_bird_70ti_benchmark.py` — benchmark script with V4 Pro config

### Model names
- DeepSeek V4 Flash: `deepseek-v4-flash` or `deepseek-chat` (default)
- DeepSeek V4 Pro: `deepseek-v4-pro`
- Reasoning effort: "high" for Pro, default/omitted for Flash
