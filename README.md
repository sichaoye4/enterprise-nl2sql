# Enterprise NL2SQL Copilot

A governed SQL copilot that maps natural language to business semantics, certified metadata, safe SQL, and validated results. Integrates a deterministic semantic engine with LLM-assisted SQL generation for accuracy-gated text-to-SQL.

## Architecture

```
question → classify → [semantic engine → quality gate] → [semantic router] →
  extract → resolve → retrieve → build-context → generate → validate →
  repair → select → judge → explain → response
```

The pipeline combines:
- **Deterministic semantic engine** — term resolution, coverage checking, SQL compilation from governed models
- **LLM-assisted routes** — fallback to LLM with guardrail contracts when semantic engine can't resolve
- **Cross-model judge** — Qwen validates LLM-generated SQL against semantic contracts
- **Validation layer** — static SQL checks, semantic validation, partition/permission checks

## Repository Structure

| Directory | Purpose |
|-----------|---------|
| `src/semantic_registry/` | Main NL2SQL pipeline source code |
| `bird_bench/` | BIRD benchmark data, results, and UI viewer |
| `bird_semantic/` | Semantic layer YAML definitions for 11 BIRD databases |
| `bird_semantic_engine/` | Semantic engine models (model.yml) for BIRD databases |
| `scripts/` | Benchmark runners, evaluation scripts, utilities |
| `semantic/` | Commerce domain semantic definitions |
| `demo/` | Demo application (superhero database) |
| `docs/` | Design and architecture documentation |
| `phases/` | Development phase requirements |
| `requirements/` | Historical requirement documents |
| `tests/` | Test suite |

### Companion Repository

This project depends on the **semantic_modeling** repo which provides the `semantic_engine` package:

```
nl2sql/
├── enterprise-nl2sql/     # This repo
└── semantic_modeling/     # Semantic engine (semantic_engine package)
```

## Setup

### 1. Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)
- API keys for LLM providers

### 2. Clone and Configure

```bash
# Configure environment
cp .env.example .env
# Edit .env with your API keys (DEEPSEEK_API_KEY, DASHSCOPE_API_KEY, etc.)
```

### 3. Install Dependencies

```bash
python -m venv .venv311
source .venv311/bin/activate
pip install -e .
```

### 4. Start PostgreSQL

```bash
docker run -d --name nl2sql-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=enterprise_nl2sql \
  -p 5432:5432 \
  postgres:15-alpine
```

### 5. BIRD Benchmark Data

The BIRD dev dataset is required for benchmarking. Download from [bird-bench.github.io](https://bird-bench.github.io/) and extract to:

```
bird_bench/dev/dev_20240627/
├── dev.json              # Questions + gold SQL
├── dev_tables.json       # Schema metadata
└── databases/dev_databases/
    ├── california_schools/california_schools.sqlite
    ├── card_games/card_games.sqlite
    └── ... (11 databases total)
```

This directory is gitignored.

## Running Benchmarks

```bash
source .venv311/bin/activate

# Mock router benchmark (tests max potential with ideal filters)
python bird_benchmark_full.py --mode mock

# Real LLM router benchmark (tests actual performance)
python bird_benchmark_full.py --mode real

# Multi-database benchmark
python bird_multidb_benchmark.py

# Basic BIRD evaluation
python scripts/bird_eval.py
```

### Viewing Results

Open `bird_bench/ui/index.html` in a browser to view benchmark results.

## Testing

```bash
source .venv311/bin/activate

# Pipeline tests
python -m pytest tests/pipeline/ -q

# All tests
python -m pytest tests/ -q
```

## Documentation

| Document | Description |
|----------|-------------|
| [Product Design](./docs/01-product-design.md) | Product positioning, MVP scope, 7-step workflow |
| [Architecture](./docs/02-architecture.md) | System architecture, semantic layer, NL2SQL pipeline |
| [AGENTS.md](./AGENTS.md) | Technical reference for AI agents working on this codebase |

### Development Phases

| Phase | Focus |
|-------|-------|
| [Phase 0](./phases/phase-00-alignment-and-scope.md) | Alignment and scope |
| [Phase 1](./phases/phase-01-semantic-registry-mvp.md) | Semantic Registry MVP |
| [Phase 2](./phases/phase-02-metadata-ingestion-and-retrieval.md) | Metadata ingestion and retrieval |
| [Phase 3](./phases/phase-03-semantic-resolver.md) | Semantic resolver |
| [Phase 4](./phases/phase-04-sql-generation-pipeline.md) | SQL generation pipeline |
| [Phase 5](./phases/phase-05-sql-validation-and-execution.md) | SQL validation and execution |
| [Phase 6](./phases/phase-06-repair-selection-and-feedback.md) | Repair, selection, and feedback |
| [Phase 7](./phases/phase-07-evaluation-runner-and-pilot.md) | Evaluation runner and pilot |

## Environment Variables

| Variable | Purpose | Required |
|----------|---------|----------|
| `DEEPSEEK_API_KEY` | Primary LLM provider | Yes |
| `DEEPSEEK_BASE_URL` | DeepSeek API endpoint | No (defaults to `https://api.deepseek.com/v1`) |
| `DASHSCOPE_API_KEY` | LLM judge (Qwen via DashScope) | Yes |
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `SEMANTIC_DIR` | Path to semantic YAML definitions | No |
| `SQL_DIALECT` | SQL dialect (`sqlite`, `postgresql`, `spark`) | No (defaults to `spark`) |

See `.env.example` for the full template.
