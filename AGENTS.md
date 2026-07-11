# Enterprise NL2SQL Pipeline

## Project Overview

Governed NL2SQL pipeline that routes questions through a semantic engine, semantic router, LLM generation, cross-model judge, and validation. Built for accuracy-gated SQL generation.

This repo works in conjunction with the **semantic_modeling** repo (`/mnt/c/users/sicha/git/nl2sql/semantic_modeling`) which provides the semantic engine package (`semantic_engine`).

## Architecture

### Two-Repo Structure

```
nl2sql/
├── enterprise-nl2sql/     # This repo: NL2SQL pipeline + BIRD benchmarking
└── semantic_modeling/     # Semantic engine package (semantic_engine)
```

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

### Data Flow

```
question → classify → [semantic engine → quality gate] → [semantic router] → 
  extract → resolve → retrieve → build-context → generate → validate → 
  repair → select → judge → explain → response
```

## Directory Structure

```
enterprise-nl2sql/
├── bird_bench/              # BIRD benchmark data and results
│   ├── dev/                 # BIRD dev dataset (gitignored, ~1.5GB)
│   │   └── dev_20240627/
│   │       ├── dev.json     # 1534 questions across 11 databases
│   │       ├── dev_tables.json
│   │       └── databases/dev_databases/  # 11 SQLite databases
│   ├── results/             # Benchmark results JSON
│   └── ui/                  # HTML results viewer
├── bird_semantic/           # Semantic layer YAML definitions (11 BIRD databases)
│   ├── california_schools/
│   ├── card_games/
│   ├── codebase_community/
│   ├── debit_card_specializing/
│   ├── european_football_2/
│   ├── financial/
│   ├── formula_1/
│   ├── student_club/
│   ├── superhero/
│   ├── thrombosis_prediction/
│   └── toxicology/
├── bird_semantic_engine/    # Semantic engine models for BIRD (11 model.yml files)
│   ├── california_schools/model.yml
│   ├── ... (10 more)
│   └── _summary.json
├── demo/                    # Demo application (superhero database)
├── docs/                    # Design and architecture documentation
├── eval_cases/              # Test case YAML files
├── phases/                  # Development phase requirements
├── requirements/            # Historical requirement documents (req_*.md)
├── scripts/                 # Benchmark and utility scripts
│   ├── bird_eval.py         # Basic BIRD evaluation
│   ├── bird_eval_stratified.py
│   ├── build_bird_semantic.py
│   ├── build_bird_semantic_engine_models.py
│   ├── run_bird_full_eval.py
│   └── ... (25+ scripts)
├── semantic/                # Commerce domain semantic definitions
├── src/semantic_registry/   # Main application source
│   ├── pipeline/            # NL2SQL pipeline implementation
│   ├── metadata/            # Metadata models and providers
│   ├── registry/            # Semantic registry
│   ├── resolver/            # Term resolution
│   ├── retrieval/           # Hybrid retrieval (embeddings + pgvector)
│   ├── validation/          # SQL validation
│   └── ...
└── tests/                   # Test suite
```

## Key Files

### Pipeline (`src/semantic_registry/pipeline/`)

| File | Purpose |
|------|---------|
| `state_machine.py` | NL2SQLPipeline orchestrator, PipelineContext |
| `context_builder.py` | Builds LLM prompt with natural language table descriptions |
| `candidate_generator.py` | LLM SQL generation (2 strategies) |
| `semantic_judge.py` | Cross-model judge (DashScope/Qwen) |
| `semantic_router.py` | LLM-based router to governed measures/dimensions |
| `llm_gateway.py` | LLM provider abstraction (DeepSeek/Mock) |
| `response.py` | PipelineResponse + ResponseBuilder |

### Semantic Engine Integration

The semantic engine (`semantic_engine` package from `semantic_modeling` repo) provides:

| Component | Module | Purpose |
|-----------|--------|---------|
| `SemanticPipeline` | `pipeline.py` | Main orchestrator: load model, resolve, route, compile |
| `ResolutionService` | `resolution/service.py` | Term resolution, coverage, routing |
| `SemanticModelCompiler` | `compiler/model_compiler.py` | Validates and compiles YAML models |
| `QueryPlanner` | `compiler/sql_compiler.py` | Converts resolution results into QueryIR |
| `SQLCompiler` | `compiler/sql_compiler.py` | Generates parameterized SQL from QueryIR |
| `GuardrailContractBuilder` | `validation/guardrails.py` | Builds LLM-facing contract |
| `SemanticContextBuilder` | `context.py` | Builds bounded semantic context for LLM |

### BIRD Benchmark Scripts

| Script | Purpose |
|--------|---------|
| `bird_benchmark_full.py` | Full pipeline benchmark with LLM router |
| `bird_multidb_benchmark.py` | Multi-database benchmark |
| `scripts/bird_eval.py` | Basic BIRD evaluation |
| `scripts/bird_eval_stratified.py` | Stratified evaluation |
| `scripts/build_bird_semantic_engine_models.py` | Generate semantic models from BIRD schemas |
| `scripts/run_bird_full_eval.py` | Full evaluation runner |

## LLM Trace Logging

Every LLM call is captured in `PipelineContext.llm_trace`:

| Stage | When | What's captured |
|-------|------|-----------------|
| `semantic_router` | LLM router call | Full router prompt + raw LLM response |
| `candidate_a` | Direct SQL generation | context_prompt + SQLCandidate JSON |
| `candidate_b` | Plan-first SQL generation | plan_first prompt + SQLCandidate JSON |
| `llm_judge` | Cross-model judge | build_judge_prompt() output + JudgeResult JSON |
| `retry_N_*` | Judge retry iterations | Updated prompts with judge feedback |
| `fallback_*` | SEMANTIC_SQL fallback | Context prompt + fallback candidates |
| `retry_without_guardrails_*` | Guardrail retries | Context without guardrail contract |

## Context Builder

- Uses enriched natural language descriptions (no DDL column listings)
- Table prose includes known metric/dimension columns inline
- Caveat: "Physical tables may have additional columns beyond those listed"
- Components: tables, schema caveat, domain knowledge, semantic plan, metrics, join paths, question, generation rules

## Testing

```bash
# Activate virtual environment
source .venv311/bin/activate

# Run pipeline tests
.venv311/bin/python -m pytest tests/pipeline/ -q

# Run all tests
.venv311/bin/python -m pytest tests/ -q
```

Test files:
- `tests/pipeline/test_context_builder.py` — context prompt assembly
- `tests/pipeline/test_candidate_generator.py` — SQL generation + LLM trace
- `tests/pipeline/test_semantic_judge.py` — cross-model judge + trace
- `tests/pipeline/test_semantic_router.py` — LLM router + trace
- `tests/pipeline/test_pipeline.py` — end-to-end pipeline flow

## BIRD Benchmarking

### Running Benchmarks

```bash
# Activate environment and load .env
source .venv311/bin/activate

# Mock router benchmark (ideal filters, tests max potential)
python bird_benchmark_full.py --mode mock --limit 10

# Real LLM router benchmark (uses actual LLM)
python bird_benchmark_full.py --mode real --limit 10

# Multi-database benchmark
python bird_multidb_benchmark.py
```

### Current Status

The BIRD benchmarking infrastructure is functional:
- ✅ BIRD dev dataset downloaded (1534 questions, 11 SQLite databases)
- ✅ 11 semantic engine models generated (`bird_semantic_engine/`)
- ✅ 11 semantic layer YAML definitions (`bird_semantic/`)
- ✅ PostgreSQL Docker container for metadata storage
- ✅ Benchmark scripts running end-to-end

Known engineering gaps:
- ⚠️ SQL compiler defaults to PostgreSQL dialect (`dialect="postgres"` in SQLCompiler.__init__). BIRD benchmarks use SQLite — semantic-compiled SQL won't execute. Fix: pass `dialect="sqlite"` when running BIRD benchmarks. Affects semantic-only route EX.
- ⚠️ Synonym coverage for BIRD natural language is poor — entity-scoping fix (commit `06bdf62`) resolved the CLARIFY dead-end, but most questions still can't match BIRD's phrasing to semantic model terms. Only ~2/110 questions reach `SEMANTIC_SQL`; the rest fall through to LLM fallback. Needs model enrichment with BIRD-specific synonyms.

## Environment Configuration

Required in `.env`:
- `DEEPSEEK_API_KEY` — Primary LLM provider
- `DASHSCOPE_API_KEY` — LLM judge (Qwen)
- `DATABASE_URL` — PostgreSQL connection string

See `.env.example` for the full template.
