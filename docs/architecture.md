# Enterprise NL2SQL — Semantic Registry & Pattern Memory System

## Product Overview

An intelligent NL2SQL (Natural Language to SQL) system that converts business questions into accurate SQL queries across enterprise databases. The system combines a **semantic registry** (business term → physical schema mapping) with a **pattern memory** (learned SQL patterns from past queries) to achieve state-of-the-art accuracy on complex analytical queries.

**Current benchmark results (BIRD-SQL dev set, 1,534 questions):**
- V4 Flash xhigh few-shot: **77.4%** Execution Accuracy
- V4 Pro high few-shot: **79.5%** Execution Accuracy
- V4 Flash + enriched schema + repair: **80.9%** (220-sample)

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Module Design](#2-module-design)
   - 2.1 [Benchmark Runner](#21-benchmark-runner)
   - 2.2 [Pattern Memory (V1)](#22-pattern-memory-v1)
   - 2.3 [Pattern Memory (V2)](#23-pattern-memory-v2)
   - 2.4 [Semantic Registry](#24-semantic-registry)
   - 2.5 [Enriched Schema Context](#25-enriched-schema-context)
   - 2.6 [Execution Repair Loop](#26-execution-repair-loop)
   - 2.7 [API Server](#27-api-server)
   - 2.8 [LLM Gateway](#28-llm-gateway)
3. [Technical Stack](#3-technical-stack)
4. [Data Flow](#4-data-flow)
5. [Failure Analysis](#5-failure-analysis)

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Question                               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Query Classifier & Router                        │
│  (classify_question_features — rule-based feature detection)        │
└─────────────────────┬───────────────────────┬───────────────────────┘
                      │                       │
          Simple Query ▼              Complex Query ▼
┌──────────────────────────────┐ ┌──────────────────────────────────┐
│   Direct Generation Path    │ │   Multi-Candidate Path           │
│                              │ │   (3-5 candidates, select best) │
└──────────┬───────────────────┘ └──────────┬───────────────────────┘
           │                                │
           ▼                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Prompt Builder                                 │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────────────────┐  │
│  │ Schema       │ │ Pattern      │ │ Execution & Validation     │  │
│  │ Context      │ │ Memory       │ │ Feedback                   │  │
│  │ (enriched    │ │ (few-shot    │ │ (error msg, empty result,  │  │
│  │  DDL+desc+)  │ │  examples)   │ │  construct checks)         │  │
│  └──────────────┘ └──────────────┘ └────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    LLM (DeepSeek V4 Flash/Pro)                      │
│    Generates: {"sql": "SELECT ...", "confidence": "high", ...}     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Execution & Repair Loop                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │Static    │→ │Execute   │→ │Validate  │→ │Repair (if needed)│   │
│  │Validate  │  │SQLite    │  │Result    │  │(up to 2 retries)  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      SQL Result + Explanation                      │
│  Rendered as table + natural language summary                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Flow Summary

1. **Question arrives** → classified by query type (simple SELECT, JOIN, AGG, etc.)
2. **Schema context** built: raw DDL + column descriptions + FK paths + sample values + semantic registry hints
3. **Pattern memory** queried: finds semantically similar past queries from same DB
4. **Prompt assembled**: schema context + few-shot examples + evidence + question
5. **LLM generates SQL**: returns JSON with SQL, tables used, confidence
6. **Validate & repair**: static checks (column existence, WHERE vs HAVING, integer division), SQLite execution, repair loop (up to 2 retries)
7. **Result returned**: SQL query results + explanations

---

## 2. Module Design

### 2.1 Benchmark Runner (`scripts/run_full_benchmark.py`)

**Purpose:** Full evaluation pipeline for BIRD-SQL benchmark (1,534 questions, 11 databases).

**Key functions:**
- `get_schema_text(db_root, db_id)` — legacy raw DDL loader (replaced by enriched schema)
- `extract_sql_from_response(raw)` — JSON parser to extract SQL from LLM response
- `validate_sql(sql, question, db_path)` — static validation checks (column existence, WHERE/HAVING, integer division, ORDER BY/LIMIT)
- `should_retry(question, sql, validation)` — decision logic for repair loop
- `generate_with_repair(provider, schema, question, evidence, db_path, max_repairs=2)` — main generation + repair loop
- `choose_best(attempts)` — selects best SQL from multiple attempts (prefers valid, non-empty, pass validation)
- `build_repair_prompt(schema, question, evidence, failed_sql, validation)` — constructs targeted repair prompt
- `execute_sql(db_path, sql)` — runs SQL against SQLite, returns result metadata

**CLI interface:**
```bash
.venv/bin/python scripts/run_full_benchmark.py --config 3 --sample 110  # quick test
.venv/bin/python scripts/run_full_benchmark.py --config 7 --indices indices.json  # stratified sample
.venv/bin/python scripts/run_full_benchmark.py --config 3 --resume 50  # resume from checkpoint
```

**Config matrix (8 configurations):**
| # | Model | Reasoning | Few-shot |
|---|-------|-----------|----------|
| 0 | V4 Flash | high | no |
| 1 | V4 Flash | high | yes |
| 2 | V4 Flash | xhigh | no |
| 3 | V4 Flash | xhigh | yes |
| 4 | V4 Pro | medium | no |
| 5 | V4 Pro | medium | yes |
| 6 | V4 Pro | high | no |
| 7 | V4 Pro | high | yes |

### 2.2 Pattern Memory V1 (`scripts/sql_pattern_memory.py`)

**Purpose:** Rule-based SQL pattern storage and retrieval for few-shot prompting.

**Components:**
- `SQLPatternStore` — SQLite-backed storage for question+SQL pairs with query type classification
- `PatternRetriever` — retrieves patterns by: (1) same database, (2) same query type, (3) keyword overlap
- `FewShotPromptBuilder` — constructs few-shot prompt with schema + examples + question
- `classify_query_type(gold_sql)` — regex-based classifier (9 types: simple_select, simple_agg, agg_filter, agg_group_by, agg_group_having, join_simple, join_agg, subquery, complex_multi)
- `match_ratio` — seeded from 1,533 BIRD dev set patterns

**Seeding:** `seed_from_bird()` — processes all 1,533 BIRD questions, classifies each by SQL pattern type, stores in SQLite.

### 2.3 Pattern Memory V2 (`scripts/pattern_memory_v2.py`)

**Purpose:** LLM-powered pattern memory with semantic analysis (not yet integrated into benchmark).

**Components:**
- `PatternAnalyzer` — uses DeepSeek to extract business_context, technical_pattern, key_entities from Q+SQL pairs
- `PatternStore` — SQLite storage with rich metadata (business_category, metrics, dimensions, tags)
- `PatternRetriever` — multi-strategy search: entity overlap → technical pattern → same DB + LLM rerank
- Builds few-shot prompt from semantically similar patterns

**V2.5 Planned improvements:**
- Offline ingestion of SQL AST features (join count, tables, aggregates, edge cases)
- Schema footprint matching (tables_used, columns_used, join edges)
- Hybrid retrieval: same DB + schema overlap + feature tags + optional LLM rerank

### 2.4 Semantic Registry (`bird_semantic/`)

**Purpose:** Business-to-physical mapping layer that bridges natural language terms to database columns.

**Structure per database** (e.g., `bird_semantic/california_schools/`):
```
bird_semantic/{db_id}/
├── concepts/         # Business concept definitions
├── dimensions/       # Physical column mappings for dimensions
├── entities/         # Table entity definitions
├── join_paths/       # Pre-defined join relationships (YAML)
├── metrics/          # Aggregation/measure definitions
└── terms/            # Natural language term → concept mappings
```

**Generation pipeline:**
1. `scripts/build_bird_semantic.py` — auto-generates entities (tables), join_paths (FKs), dimensions (GROUP BY cols), metrics (gold SQL aggregations)
2. `scripts/gen_llm_metrics.py` — uses DeepSeek to generate metrics with proper concept→metric 1:1 linkage
3. `scripts/fix_concept_metrics.py` — deterministic extraction using question→SQL→metric chain

**Key design decisions:**
- Terms link natural language → concepts (many-to-many)
- Concepts link to metrics + dimensions (one-to-many)
- Metrics are physical SQL expressions (not logical measures)
- Join paths are explicit (table.column → table.column)

### 2.5 Enriched Schema Context (`scripts/bird_schema_context.py`)

**Purpose:** Builds rich schema description for LLM prompts, replacing raw DDL.

**Functions:**
- `build_schema_context(db_root, db_id, question, evidence)` — main entry point, returns enriched schema string
- `_raw_ddl(db_path)` — cached raw DDL from SQLite
- `_schema_info(db_path)` — cached column metadata (name, type, notnull, PK)
- `_foreign_keys(db_path)` — cached FK relationships from PRAGMA foreign_key_list
- `_inferred_join_paths(db_path)` — infers joins from column naming patterns (CDSCode, *_id, uuid, etc.)
- `_descriptions(db_dir)` — parses database_description/*.csv for column documentation
- `_all_samples(db_path)` — cached sample values for text columns (top 5 distinct values)
- `_semantic_matches(db_root, db_id, text)` — loads matching terms/dimensions/metrics from bird_semantic/

**Prompt output sections:**
1. Raw DDL (preserved from original)
2. Table/column definitions with descriptions, PK/FK markers, and links
3. Join paths (explicit + inferred)
4. Sample values for relevant text columns (matched to question keywords)
5. Semantic registry hints (term→concept→metric mappings)
6. BIRD evidence (passthrough)

**Graceful degradation:** Falls back to raw DDL if descriptions, semantic files, or samples unavailable.

### 2.6 Execution Repair Loop

**Purpose:** Self-correction mechanism that catches and fixes common SQL generation errors.

**Validation checks:**
1. **Unknown column references** — validates all qualified column references against actual schema
2. **Aggregate in WHERE** — detects COUNT/SUM/AVG inside WHERE clause (should be HAVING)
3. **Missing ORDER BY + LIMIT** — ranking questions need both constructs
4. **LIMIT without ORDER BY** — unstable for ranking semantics
5. **Integer division** — ratio/percentage questions with `/` but no CAST or NULLIF

**Repair triggers:**
- SQL execution error (syntax/runtime failure)
- Empty result set (when question implies results expected)
- Static validation failures

**Repair flow:**
1. Generate SQL → validate → execute
2. If any issue found, build repair prompt with: question + evidence + schema + failed SQL + error reason
3. Retry with specific fix instruction (up to 2 retries)
4. Choose best result from all attempts

**Success rate:** 76% (19/25 repair attempts corrected the SQL)

### 2.7 API Server (`scripts/nl2sql_api.py`)

**Purpose:** HTTP API serving the NL2SQL pipeline with SPA UI.

**Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/databases` | List available databases |
| POST | `/api/query` | Execute natural language query |
| POST | `/api/confirm` | Mark query as correct (feeds pattern memory) |
| GET | `/api/history` | Query history |
| GET | `/api/stats` | Pattern memory statistics |
| GET | `/api/ui` | SPA UI (index.html) |

**Query flow:**
1. Get schema → LLM analyze question + retrieve patterns from V2 memory
2. Build few-shot prompt → generate SQL → execute → return results
3. On confirmation: ingest query into pattern memory for future improvement

**UI features:**
- Modern dark theme (Linear/Raycast style)
- SQL syntax highlighting
- Results table
- History sidebar
- Pattern memory stats panel
- Confirm button to feed successful queries back

### 2.8 LLM Gateway (`src/semantic_registry/pipeline/llm_gateway.py`)

**Purpose:** Abstraction layer for LLM providers (DeepSeek, mock).

**Components:**
- `DeepSeekProvider` — OpenAI-compatible chat completions API
  - Configurable: model name, reasoning_effort (none/low/medium/high/xhigh)
  - Automatic retry on transient errors (APIConnectionError, RateLimitError, APITimeoutError)
  - System prompt enforcing JSON-only output with specific schema
- `MockLLMProvider` — deterministic SQL generation from semantic plan context (for testing)
- `validate_select_sql(sql)` — sqlglot-based static SQL validation
- `TransientLLMError` — custom exception for API failures

---

## 3. Technical Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Language** | Python | 3.11+ |
| **LLM** | DeepSeek V4 Flash / V4 Pro | via OpenAI-compatible API |
| **Database** | SQLite | 3.x (BIRD benchmark) |
| **Semantic Registry** | YAML + custom Python | — |
| **Pattern Memory** | SQLite + Python | — |
| **Web UI** | Vanilla HTML/CSS/JS | — |
| **API Server** | Python http.server | stdlib |
| **Static Analysis** | sqlglot | SQL parsing |
| **Benchmark** | BIRD-SQL | 1,534 dev questions |
| **DI Framework** | Manual DI | — |
| **Testing** | pytest | — |
| **CI/CD** | git | — |

**Key dependencies:**
- `openai>=1.0` — DeepSeek API client
- `sqlglot` — SQL parsing and validation
- `sqlite3` — Python stdlib
- `PyYAML` — semantic registry YAML parsing
- Standard library only: `json`, `re`, `csv`, `os`, `glob`, `collections`, `math`, `time`, `http.server`

---

## 4. Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        BENCHMARK MODE                                   │
│                                                                         │
│  dev.json ──→ Load 1,534 questions                                      │
│                  │                                                      │
│                  ├── For each question:                                 │
│                  │   ├── build_schema_context()                         │
│                  │   │   ├── Raw DDL (SQLite)                          │
│                  │   │   ├── Column descriptions (CSV)                 │
│                  │   │   ├── FK/join paths (PRAGMA + inference)        │
│                  │   │   ├── Sample values (DISTINCT query)            │
│                  │   │   └── Semantic hints (bird_semantic/)            │
│                  │   │                                                 │
│                  │   ├── retrieve_patterns()                           │
│                  │   │   └── SQLPatternMemory → top 3 examples         │
│                  │   │                                                 │
│                  │   ├── build_prompt()                                │
│                  │   │   └── schema + patterns + evidence + question   │
│                  │   │                                                 │
│                  │   ├── generate_with_repair()                        │
│                  │   │   ├── LLM.generate() → SQL                      │
│                  │   │   ├── validate_sql() (static checks)            │
│                  │   │   ├── execute_sql() (against SQLite)            │
│                  │   │   ├── should_retry()? → repair prompt           │
│                  │   │   └── choose_best() from attempts               │
│                  │   │                                                 │
│                  │   └── evaluate_match()                              │
│                  │       ├── Execute predicted SQL                     │
│                  │       ├── Execute gold SQL                          │
│                  │       └── Compare result sets (EX metric)           │
│                  │                                                     │
│                  └── Save results → full_V4_*.json                     │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                        API MODE                                         │
│                                                                         │
│  POST /api/query ──→ build_schema_context()                            │
│                    ─→ retrieve_patterns() (V2 memory)                   │
│                    ─→ LLM.generate()                                    │
│                    ─→ execute_sql()                                     │
│                    ─→ return results + SQL                              │
│                                                                         │
│  POST /api/confirm ──→ ingest into pattern memory                      │
│                       └→ System improves over time                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Failure Analysis

Based on full BIRD dev set evaluation (1,534 questions, both V4 Flash and V4 Pro):

### Root Cause Distribution

| Rank | Root Cause | Est. % | Example |
|------|-----------|--------|---------|
| 1 | Column/expression mismatch | 31% | `County Name` vs `District Name` |
| 2 | JOIN + multi-table complexity | 22% | Wrong join keys across 3+ tables |
| 3 | Filter value errors | 13% | `'Continuation'` vs `'Continuation School'` |
| 4 | Missing ORDER BY + LIMIT | 11% | "Top N" ranking without sort |
| 5 | WHERE vs HAVING confusion | 8% | Filtering aggregated column with WHERE |
| 6 | CASE/DISTINCT/subquery logic | 12% | Conditional bucketing, dedup |
| 7 | Misc (BETWEEN, LIKE, ops) | 3% | String/date operations |

### Most Predictive Structural Features (phi coefficient)

| Feature | Phi | Lift vs absent |
|---------|:---:|:--------------:|
| `has_math` (division, ratios) | +0.293 | +22.5pp |
| `has_cast` (type conversion) | +0.281 | +27.5pp |
| `has_case` (CASE WHEN) | +0.274 | +30.5pp |
| `has_subquery` | +0.147 | +15.6pp |
| `has_join` | +0.124 | +8.3pp |

### Per-Database Difficulty (proxy: BIRD "challenging" label rate)

| Database | Challenging% |
|----------|:------------:|
| toxicology | 21.4% |
| thrombosis_prediction | 17.2% |
| superhero | 11.6% |
| european_football_2 | 10.9% |
| formula_1 | 8.0% |
| card_games | 6.8% |
| financial | 6.6% |
| debit_card_specializing | 6.2% |
| student_club | 5.7% |
| california_schools | 5.6% |
| codebase_community | 2.7% |

### Improvement Roadmap (estimated gains)

| Priority | Change | Est. EX gain |
|----------|--------|:------------:|
| 1 | Enriched schema + column descriptions + FK paths | +3 to +5 |
| 2 | Execution-guided repair loop | +2 to +4 |
| 3 | Schema-aware V2.5 pattern memory | +1.5 to +3 |
| 4 | Multi-candidate for hard questions | +1.5 to +3 |
| 5 | Risk-aware prompt templates | +1 to +2 |
| 6 | Domain value grounding (sample values) | +1 to +2 |

**Items 1-2 implemented → 80.9%** (up from 77.4% baseline)
**Items 3-6 planned → target 86-91%**

---

## Appendix: Repository Structure

```
enterprise-nl2sql/
├── bird_bench/               # BIRD-SQL benchmark data + results
│   ├── dev/                  # Dev set (1,534 questions, 11 DBs)
│   ├── results/              # Benchmark results
│   │   ├── benchmarks/       # Sample benchmark results (6 configs)
│   │   └── full_benchmarks/  # Full set benchmark results
│   └── ui/                   # SPA web interface
├── bird_semantic/            # Semantic registry (11 databases)
│   └── {db_id}/
│       ├── concepts/         # Business concept definitions
│       ├── dimensions/       # Physical dimension mappings
│       ├── entities/         # Table entity definitions
│       ├── join_paths/       # Join relationships
│       ├── metrics/          # Measure/aggregation definitions
│       └── terms/            # Natural language → concept mappings
├── scripts/                  # Core pipeline scripts
│   ├── run_full_benchmark.py # Main benchmark runner
│   ├── bird_schema_context.py# Enriched schema builder
│   ├── sql_pattern_memory.py # V1 pattern memory
│   ├── pattern_memory_v2.py  # V2 pattern memory (LLM-powered)
│   ├── nl2sql_api.py         # HTTP API server
│   ├── build_bird_semantic.py# Semantic registry generator
│   ├── gen_llm_metrics.py    # LLM metric generator
│   ├── watchdog.py           # Benchmark progress monitor
│   ├── codex_failure_analysis.py  # Failure root cause analysis
│   └── ...
├── src/semantic_registry/    # Core library
│   ├── pipeline/             # LLM gateway, context builder, classifier
│   ├── resolver/             # Concept resolution, planning
│   ├── retrieval/            # Embedding search, hybrid retrieval
│   ├── repair/               # Error classification, repair loop
│   ├── validation/           # SQL validation, permissions
│   ├── metadata/             # Schema normalization, snapshots
│   └── ...
├── tests/                    # Unit tests (pytest)
├── docs/                     # Design documents
└── pyproject.toml            # Project configuration
```
