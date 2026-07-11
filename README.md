# Enterprise NL2SQL Copilot — Development Repository

## Overview

This is the development repository for the Enterprise NL2SQL Copilot project — a governed SQL copilot that maps natural language → business semantics → certified metadata → safe SQL → preview result → feedback loop.

## Design References

| Doc | Description |
|-----|-------------|
| [01-Product Design](./docs/01-product-design.md) | Product positioning, MVP scope, core 7-step workflow, frontend UI design |
| [02-Architecture](./docs/02-architecture.md) | System architecture, semantic layer, metadata layer, retrieval design, NL2SQL pipeline, SQL generation/validation/execution |

**Read these two documents before starting any phase.** They define the product vision and architectural foundation that all phases build upon.

## Development Phases

| Phase | Doc | Duration | Focus |
|-------|-----|----------|-------|
| Phase 0 | [Alignment and Scope](./phases/phase-00-alignment-and-scope.md) | 1 week | Domain selection, dialect choice, governance model |
| Phase 1 | [Semantic Registry MVP](./phases/phase-01-semantic-registry-mvp.md) | 2 weeks | YAML definitions, Postgres semantic tables, registry API |
| Phase 2 | [Metadata Ingestion and Retrieval](./phases/phase-02-metadata-ingestion-and-retrieval.md) | 2 weeks | Metadata adapter, embeddings, pgvector, hybrid retriever |
| Phase 3 | [Semantic Resolver](./phases/phase-03-semantic-resolver.md) | 2 weeks | Term extractor, synonym matcher, ambiguity detection |
| Phase 4 | [SQL Generation Pipeline](./phases/phase-04-sql-generation-pipeline.md) | 2–3 weeks | Question classifier, LLM gateway, 2-candidate SQL gen |
| Phase 5 | [SQL Validation and Execution](./phases/phase-05-sql-validation-and-execution.md) | 2 weeks | SQLGlot validation, semantic checks, preview execution |
| Phase 6 | [Repair, Selection, and Feedback](./phases/phase-06-repair-selection-and-feedback.md) | 1–2 weeks | Repair loop, candidate selection, feedback capture |
| Phase 7 | [Evaluation Runner and Pilot](./phases/phase-07-evaluation-runner-and-pilot.md) | 2 weeks | Offline eval, regression tests, pilot onboarding |

## Setup

### 1. Environment Configuration

Copy and configure the `.env` file with your API keys:

```bash
cp .env.example .env  # or create .env manually
```

Required environment variables:
- `DEEPSEEK_API_KEY` — Primary LLM provider
- `DASHSCOPE_API_KEY` — LLM judge (Qwen via DashScope)
- `DATABASE_URL` — PostgreSQL connection string

See `.env` for the full list of configurable variables.

### 2. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Database

```bash
docker run -d --name nl2sql-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=enterprise_nl2sql \
  -p 5432:5432 \
  postgres:15-alpine
```

## Rules

- **No hand-written code.** All implementation must be done through Codex with requirements-only documents.
- **No code in requirements.** Phase documents contain only functional and non-functional requirements.
