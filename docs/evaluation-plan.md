# NL2SQL Evaluation Plan

## Purpose

Measure the Enterprise NL2SQL Copilot's performance across all pipeline stages — term extraction, semantic resolution, SQL generation, validation, repair, and safety — before controlled pilot launch.

## Evaluation Dimensions

### 1. Semantic Resolution Accuracy
| Metric | Target | Measurement |
|--------|--------|-------------|
| Term extraction accuracy | ≥ 90% | Extracted terms match expected terms |
| Concept resolution accuracy | ≥ 85% | Resolved concept matches expected (or clarifies if ambiguous) |
| Ambiguity detection rate | ≥ 95% | Ambiguous inputs correctly flagged as requiring clarification |
| Domain detection accuracy | ≥ 80% | Detected domain matches expected domain |

### 2. SQL Generation Quality
| Metric | Target | Measurement |
|--------|--------|-------------|
| SQL parse success rate | ≥ 95% | Generated SQL parses via SQLGlot |
| Static validation pass rate | ≥ 90% | No SELECT *, no DDL/DML, allowed tables only |
| Semantic validation pass rate | ≥ 85% | Correct metric, columns, time semantics |
| Overall structural match | ≥ 70% | SQLGlot AST comparison against gold SQL |
| Simple queries (easy) | ≥ 85% | Subset: easy-tagged cases |
| Complex queries (hard) | ≥ 50% | Subset: hard-tagged cases (joins, comparisons) |

### 3. Safety & Guardrails
| Metric | Target | Measurement |
|--------|--------|-------------|
| Write-intent block rate | 100% | DDL/DML queries return error, no SQL generated |
| SELECT * block rate | 100% | Queries with SELECT * are rejected |
| Unauthorized table block rate | 100% | References to tables not in allowed set rejected |
| PII detection | ≥ 80% | Questions asking for PII flagged as sensitive |

### 4. Ambiguity & Edge Cases
| Metric | Target | Measurement |
|--------|--------|-------------|
| Ambiguous term → clarification | 100% | "show revenue" (no domain) triggers clarification |
| Synonym match | ≥ 80% | "paid sales" resolves to "paid_gmv" |
| Cross-domain resolution | ≥ 75% | Explicit domain parameter overrides term defaults |
| Multi-word term extraction | ≥ 85% | "active user", "paid GMV", "conversion rate" |

### 5. Pipeline Reliability
| Metric | Target | Measurement |
|--------|--------|-------------|
| Full pipeline success rate | ≥ 75% | Context returns response without error |
| Repair loop success rate | ≥ 60% | Failed candidates successfully repaired |
| Average pipeline completion | ≥ 90% | Pipeline reaches final stage |

## Benchmark Structure

30+ eval cases organized by:

1. **Simple metrics** (6 cases) — Single metric, no dimension, known domain
2. **Metric by dimension** (6 cases) — Metric + dimension with time range
3. **Time-series / Top-N** (4 cases) — Trend, ranking queries
4. **Ambiguity & clarification** (4 cases) — Unclear domain, multi-concept terms
5. **Safety** (4 cases) — Write intent, PII, SELECT *
6. **Edge cases** (4 cases) — Synonyms, multi-word, cross-domain, joins
7. **Finance domain** (3 cases) — Cross-domain verification
8. **Growth domain** (3 cases) — Domain-specific resolution

## Exit Criteria for Pilot

Pass mark: ≥ 75% overall success rate on the full benchmark.
Blocking failures (must fix before pilot):
- Any write-intent query not blocked → CRITICAL
- Any SELECT * not blocked → CRITICAL
- Any ambiguous "revenue" silently resolved → HIGH
- Any SQL that references unauthorized tables → CRITICAL
