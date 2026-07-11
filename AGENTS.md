# Enterprise NL2SQL Pipeline

## Project Overview
 governed NL2SQL pipeline that routes questions through a semantic engine, semantic router, LLM generation, cross-model judge, and validation. Built for accuracy-gated SQL generation.

## Key Architecture

### Pipeline Stages (in order)
1. `classify` — detect write/sensitive intent
2. `run_semantic_engine` — deterministic semantic model compilation
3. `run_semantic_quality_gate` — reject orphan filters in semantic SQL
4. `run_semantic_llm_router` — LLM-route to governed measures/dimensions
5. `extract_terms` — extract business terms from question
6. `resolve_semantics` — map terms to registry concepts
7. `retrieve_metadata` — hybrid retrieval of tables/metrics
8. `build_context` — assemble LLM prompt (context_builder)
9. `generate_candidates` — LLM generates SQL (2 strategies: direct + plan_first)
10. `validate` — static + semantic + permissions validation
11. `repair` — retry with repair loop on failure
12. `select` — pick best candidate
13. `run_llm_judge` — cross-model semantic judge (Qwen via DashScope)
14. `explain` — build SQL explanation
15. `build_response` — assemble PipelineResponse

## Key Files

### `src/semantic_registry/pipeline/`
- **`state_machine.py`** — NL2SQLPipeline orchestrator, PipelineContext, RegistryMetadataProvider. Central pipeline logic.
- **`context_builder.py`** — Builds LLM prompt from semantic plan + retrieved metadata. Uses natural language prose for table descriptions (no fake DDL column listings). `_enriched_table_description()` renders columns as prose. `_schema_caveat_section()` warns about additional physical columns.
- **`candidate_generator.py`** — LLM SQL generation (2 strategies). Captures LLM trace into PipelineContext.llm_trace.
- **`semantic_judge.py`** — Cross-model judge (DashScope/Qwen). LLMJudge + build_judge_prompt.
- **`semantic_router.py`** — LLM-based router to governed measures/dimensions. build_router_prompt + SemanticRouter.
- **`llm_gateway.py`** — LLM provider abstraction (DeepSeek/Mock). LLMGateway wraps providers.
- **`response.py`** — PipelineResponse + ResponseBuilder.

### `src/semantic_registry/metadata/`
- **`models.py`** — TableMetadata, ColumnMetadata, JoinPath models
- **`provider.py`** — MetadataProvider abstract base
- **`snapshot.py`** — MetadataSnapshot for DB snapshot management

### Data Flow
```
question → classfiy → [semantic engine → quality gate] → [semantic router] → extract → resolve → retrieve → build-context → generate → validate → repair → select → judge → explain → response
```

## LLM Trace Logging
Every LLM call in the pipeline is captured in `PipelineContext.llm_trace` as `{stage: {prompt, response}}`.

| Stage | When | What's captured |
|---|---|---|
| `semantic_router` | LLM router call | Full router prompt + raw LLM response |
| `candidate_a` | Direct SQL generation | context_prompt + SQLCandidate JSON |
| `candidate_b` | Plan-first SQL generation | plan_first prompt + SQLCandidate JSON |
| `llm_judge` | Cross-model judge | build_judge_prompt() output + JudgeResult JSON |
| `retry_N_*` | Judge retry iterations | Updated prompts with judge feedback |
| `fallback_*` | SEMANTIC_SQL fallback | Context prompt + fallback candidates |
| `retry_without_guardrails_*` | Guardrail retries | Context without guardrail contract |

Access via `context.llm_trace` after pipeline run.

## Context Builder
- Uses enriched natural language descriptions (no DDL column listings)
- Table prose includes known metric/dimension columns inline
- Caveat: "Physical tables may have additional columns beyond those listed"
- Components: tables, schema caveat, domain knowledge, semantic plan, metrics, join paths, question, generation rules

## Models
- `SemanticRegistryData` — in-memory registry (concepts, metrics, dimensions, terms, join paths)
- `RegistryMetadataProvider` — builds TableMetadata from registered metrics/dimensions
- `PipelineContext` — per-query context, carries llm_trace for debugging

## Testing
- `tests/pipeline/test_context_builder.py` — context prompt assembly
- `tests/pipeline/test_candidate_generator.py` — SQL generation + LLM trace
- `tests/pipeline/test_semantic_judge.py` — cross-model judge + trace
- `tests/pipeline/test_semantic_router.py` — LLM router + trace
- `tests/pipeline/test_pipeline.py` — end-to-end pipeline flow
- Run: `.venv/bin/python -m pytest tests/pipeline/ -q`
