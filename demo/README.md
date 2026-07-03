# Enterprise NL2SQL — Demo Package

A self-contained demo of the NL2SQL system using the **Superhero** database (0.23 MB, 129 questions).

## Contents

| What | Path |
|------|------|
| Benchmark runner | `scripts/run_full_benchmark.py` |
| Enriched schema | `scripts/bird_schema_context.py` |
| V2.5 pattern memory | `scripts/pattern_memory_v25.py` |
| DB Registry | `src/semantic_registry/registry/db_registry.py` |
| Semantic registry | `bird_semantic/superhero/` |
| LLM gateway | `src/semantic_registry/pipeline/llm_gateway.py` |
| API server | `scripts/nl2sql_api.py` |
| Web UI | `bird_bench/ui/index.html` |
| Architecture doc | `docs/architecture.md` |
| Superhero DB | `data/superhero.sqlite` (232 KB) |
| Demo questions | `data/dev.json` (129 questions) |

## Quick Start

```bash
# 1. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install openai PyYAML sqlglot

# 2. Set your API key
export DEEPSEEK_API_KEY="your-key-here"

# 3. Run benchmark (5 questions smoke test)
.venv/bin/python scripts/run_full_benchmark.py --config 3 --memory v25 --sample 5

# 4. Run full demo (129 superhero questions)
.venv/bin/python scripts/run_full_benchmark.py --config 3 --memory v25 --indices data/sample_indices.json

# 5. Start API server
.venv/bin/python scripts/nl2sql_api.py
# Open http://localhost:8765/api/ui in browser
```

## Requirements

- Python 3.11+
- DeepSeek API key (or modify `llm_gateway.py` for other providers)
- Dependencies: `openai`, `PyYAML`, `sqlglot` (pip install)
