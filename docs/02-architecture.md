# Architecture Design — Enterprise NL2SQL Copilot

> 以下内容来自飞书文档 [enterprises nl2sql](https://my.feishu.cn/wiki/DuCFwTAb6imVh5kchfGcMKJ5nJg)，仅做整理切分，未做内容修改。

---

## Key design principle

The most important architectural principle is:

```
Natural language → semantic resolution → physical metadata → SQL
```

Not:

```
Natural language → schema retrieval → SQL
```

Because in enterprise environments, terms like these are ambiguous:

```
revenue
GMV
paid GMV
net revenue
active user
new user
customer
payer
buyer
order date
payment date
settlement date
event date
```

So the product must resolve business meaning before generating SQL.

---

## High-level architecture

```
User Question
   ↓
Question Classifier
   ↓
Business Term Extractor
   ↓
Semantic Resolver
   ↓
Semantic Query Plan
   ↓
Metadata Retriever
   ↓
Prompt / Context Builder
   ↓
SQL Candidate Generator
   ↓
Static SQL Validator
   ↓
Semantic SQL Validator
   ↓
Dry Run / EXPLAIN / Preview Execution
   ↓
Candidate Selector / Repair Once
   ↓
Response Builder
   ↓
User Review + Feedback
   ↓
Evaluation / Learning Loop
```

Expanded architecture:

```
                         ┌─────────────────────────┐
                         │        Frontend          │
                         │ NL question + SQL editor │
                         └────────────┬────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │       NL2SQL API         │
                         │ auth / orchestration     │
                         └────────────┬────────────┘
                                      │
      ┌───────────────────────────────┼───────────────────────────────┐
      ▼                               ▼                               ▼
┌───────────────┐          ┌────────────────────┐          ┌─────────────────┐
│ Metadata       │          │ Semantic Registry  │          │ Permission       │
│ Service        │          │ terms / concepts   │          │ Service          │
│ schema / dict  │          │ metrics / entities │          │ table/column ACL │
└───────┬───────┘          └─────────┬──────────┘          └────────┬────────┘
        │                            │                              │
        ▼                            ▼                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            NL2SQL Pipeline                                  │
│                                                                             │
│  1. Classifier                                                              │
│  2. Business term resolver                                                   │
│  3. Semantic query planner                                                   │
│  4. Metadata retriever                                                       │
│  5. Context builder                                                          │
│  6. SQL candidate generator                                                  │
│  7. Static validator                                                         │
│  8. Semantic validator                                                       │
│  9. Dry-run / preview executor                                               │
│ 10. Repair-once loop                                                         │
│ 11. Candidate selector                                                       │
│ 12. Response builder                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │ Feedback + Eval Store    │
                         │ logs / corrections /     │
                         │ gold SQL / metrics       │
                         └─────────────────────────┘
```

---

## Semantic layer design

The semantic layer is the heart of enterprise NL2SQL.
It links:

```
business terms → concepts → metrics/dimensions/entities → physical tables/columns
```

Modern semantic-layer systems commonly model entities, metrics/measures, dimensions, and relationships. dbt Semantic Layer / MetricFlow uses semantic models with entities and dimensions, and MetricFlow is designed to generate SQL for metrics across dimensions. Cube similarly models measures, dimensions, and joins in a centralized semantic layer.  

### 6.1 Semantic registry objects

DataHub's business glossary is a useful reference pattern because it defines shared business vocabulary and associates terms with physical data assets. It also supports YAML-based glossary ingestion, which fits a Git-managed semantic registry workflow.  

---

### 6.2 Business term example

```
term: revenue
description: Generic business term for monetary business performance.
synonyms:
  - sales
  - income
  - turnover
candidate_concepts:
  - gmv
  - paid_gmv
  - net_revenue
default_concept_by_domain:
  finance: net_revenue
  commerce: paid_gmv
  growth: paid_gmv
ambiguity_level: high
clarification_required_when:
  - domain_unknown
  - user_mentions_only_revenue_without_context
```

---

### 6.3 Concept example

```
concept: net_revenue
display_name: Net Revenue
domain: finance
definition: Revenue after refunds, discounts, commissions, and settlement adjustments.
type: metric_concept
owner: finance_analytics
related_but_different:
  gmv: Submitted order value before payment success and refunds.
  paid_gmv: Successfully paid order value before refund adjustment.
  settlement_amount: Amount settled to merchant or partner.
canonical_metric: net_revenue
status: certified
```

---

### 6.4 Metric example

```
metric: net_revenue
concept: net_revenue
description: Total realized revenue after refund and settlement adjustment.
type: simple_sum
measure:
  table: ads_finance_channel_daily
  column: net_revenue_amt
aggregation: sum
unit: CNY
default_time_dimension: settlement_date
physical_time_column: settlement_dt
allowed_dimensions:
  - channel
  - product_category
  - region
  - settlement_dt
owner: finance_analytics
status: certified
```

For ratio metrics:

```
metric: conversion_rate
concept: conversion_rate
description: Paid orders divided by ad clicks.
type: ratio
numerator:
  metric: paid_order_count
denominator:
  metric: click_count
expression: paid_order_count / nullif(click_count, 0)
allowed_dimensions:
  - campaign
  - channel
  - dt
status: certified
```

Rule:

```
The LLM may select certified metrics.
The LLM must not invent new metric formulas at runtime.
```

---

### 6.5 Dimension example

```
dimension: channel
description: Marketing acquisition or transaction attribution channel.
entity: channel
synonyms:
  - traffic source
  - acquisition source
  - source channel
physical_mappings:
  - table: ads_order_channel_daily
    column: channel_name
  - table: ads_finance_channel_daily
    column: channel_name
status: certified
```

---

### 6.6 Entity example

```
entity: user
description: Registered platform user.
primary_keys:
  - user_id
related_entities:
  - buyer
  - payer
  - account
  - device
ambiguity_notes:
  - buyer_user_id means the user who placed the order.
  - payer_user_id means the user who completed payment.
  - device_id is not equivalent to user_id.
```

---

### 6.7 Join path example

```
join_path:
  from: ads_order_daily
  to: dim_channel
  relationship: many_to_one
  join_condition: ads_order_daily.channel_id = dim_channel.channel_id
  safe_for_metrics:
    - gmv
    - paid_gmv
    - order_count
  fanout_risk: low
```

Cube's join modeling explicitly includes relationship types such as one-to-one, one-to-many, and many-to-one, which is the kind of information your NL2SQL system needs to avoid double counting.  

---

## Metadata layer design

You already have schema and data dictionaries, so the MVP should not replace the metadata system. It should build a normalized metadata access layer.

### 7.1 Metadata provider interface

```
class MetadataProvider:
    def search_tables(self, query: str, domain: str) -> list[TableMetadata]:
        ...

    def get_table(self, table_name: str) -> TableMetadata:
        ...

    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        ...

    def get_join_paths(self, tables: list[str]) -> list[JoinPath]:
        ...

    def get_example_queries(self, query: str) -> list[ExampleQuery]:
        ...
```

### 7.2 Table metadata contract

```
table: ads_order_channel_daily
description: Daily order and payment metrics by marketing channel.
domain: commerce
certified: true
eligible_for_nl2sql: true
grain:
  - dt
  - channel_id
partition_column: dt
owner: commerce_analytics
columns:
  dt:
    type: date
    description: Payment business date.
  channel_name:
    type: string
    description: Marketing channel name.
  paid_gmv_amt:
    type: decimal
    description: Successful payment amount after coupon discount, before refund.
    concept: paid_gmv
    aggregation: sum
    unit: CNY
```

### 7.3 Table eligibility rule

Only tables that pass these checks should be exposed to NL2SQL:

```
eligible_for_nl2sql: true
certified: true
owner_exists: true
grain_documented: true
partition_documented: true
pii_reviewed: true
business_description_exists: true
```

This is one of the highest-value guardrails.

---

## Retrieval architecture

The retriever should not retrieve only schemas. It should retrieve:

```
business terms
concept definitions
metric definitions
dimension definitions
table descriptions
column descriptions
grain
partition column
join paths
sample values or value summaries
example queries
known caveats
```

### 8.1 Recommended retrieval stack

For MVP:

```
Postgres + pgvector
```

pgvector provides vector similarity search inside Postgres and supports exact and approximate nearest-neighbor search, which makes it a good simple MVP choice when you also want to store metadata, logs, and eval cases in Postgres.  

Later upgrade:

```
OpenSearch / Elasticsearch / dedicated vector DB
```

Use that only when metadata scale, hybrid search quality, or multi-tenant retrieval performance requires it.

### 8.2 Hybrid scoring

```
final_score =
  0.35 * embedding_similarity
+ 0.30 * keyword_match
+ 0.15 * semantic_concept_match
+ 0.10 * certification_boost
+ 0.10 * usage_popularity
```

### 8.3 Retrieval output

```
{
  "candidate_concepts": ["paid_gmv"],
  "candidate_metrics": ["paid_gmv"],
  "candidate_dimensions": ["channel", "dt"],
  "candidate_tables": ["ads_order_channel_daily"],
  "candidate_columns": [
    "ads_order_channel_daily.dt",
    "ads_order_channel_daily.channel_name",
    "ads_order_channel_daily.paid_gmv_amt"
  ],
  "known_caveats": [
    "Do not use gmv_amt for paid GMV."
  ]
}
```

---

## NL2SQL runtime pipeline

### 9.1 Pipeline state machine

For MVP, use a deterministic state machine, not a complex multi-agent graph.

```
def nl2sql_pipeline(question, user_context):
    classification = classify_question(question, user_context)

    if classification.write_intent:
        return block_write_intent()

    terms = extract_business_terms(question)

    semantic_plan = resolve_semantics(
        question=question,
        terms=terms,
        classification=classification
    )

    if semantic_plan.requires_clarification:
        return ask_clarification(semantic_plan)

    metadata_context = retrieve_metadata(semantic_plan)

    prompt_context = build_context(
        question=question,
        semantic_plan=semantic_plan,
        metadata_context=metadata_context,
        user_context=user_context
    )

    candidates = generate_sql_candidates(prompt_context, n=2)

    validated_candidates = validate_candidates(
        candidates=candidates,
        semantic_plan=semantic_plan,
        metadata_context=metadata_context,
        user_context=user_context
    )

    repaired_candidates = repair_once_if_needed(validated_candidates)

    preview_results = dry_run_and_preview(repaired_candidates)

    selected = select_best_candidate(preview_results)

    log_everything(selected)

    return build_response(selected)
```

### 9.2 Why state machine first

A state machine is easier to:

```
debug
audit
test
secure
regression-test
explain to governance teams
```

Move to LangGraph or another workflow framework only when branching and tool usage become materially more complex.

---

## SQL generation design

### 10.1 LLM output contract

The model must return strict JSON:

```
{
  "sql": "...",
  "assumptions": [
    "Paid GMV maps to paid_gmv_amt.",
    "Last 30 days means last 30 complete business dates."
  ],
  "tables_used": ["ads_order_channel_daily"],
  "columns_used": [
    "dt",
    "channel_name",
    "paid_gmv_amt"
  ],
  "confidence": "high",
  "reasoning_summary": "Used the certified paid GMV metric from the commerce ADS table."
}
```

Do not expose hidden chain-of-thought. Store only a short reasoning summary.

### 10.2 Generation rules

```
Generate SELECT-only SQL.
Use only provided tables and columns.
Do not invent tables.
Do not invent columns.
Do not invent metric formulas.
Do not use SELECT *.
Always include a partition/time filter for large tables.
Use the specified SQL dialect.
Prefer certified tables and metrics.
Return JSON only.
```

### 10.3 Candidate generation

For MVP:

```
Generate 2 candidates.
```

Candidate A:

```
Direct SQL generation from semantic plan.
```

Candidate B:

```
Plan-first SQL generation using the same semantic plan.
```

Avoid 10–20 candidate generation in MVP because it increases latency, cost, and complexity.

---

## SQL validation design

Validation has two layers:

```
1. Static SQL validation
2. Semantic SQL validation
```

### 11.1 Static SQL validation

Use SQLGlot for parsing, table extraction, column extraction, dialect handling, and AST-level validation. SQLGlot is a no-dependency Python SQL parser, transpiler, optimizer, and engine that supports many SQL dialects including Spark/Databricks, Snowflake, BigQuery, DuckDB, and Trino/Presto.  

Static checks:

```
SELECT-only
no INSERT / UPDATE / DELETE / MERGE
no DROP / CREATE / ALTER
no stored procedure calls
no SELECT *
no unauthorized schemas
only allowed tables
only allowed columns
no forbidden functions
no uncontrolled CROSS JOIN
LIMIT required for preview
partition filter required for large tables
```

### 11.2 Semantic SQL validation

Semantic checks:

```
SQL uses the resolved metric column.
SQL uses the correct time semantic.
SQL uses allowed dimensions for the metric.
SQL aggregation matches metric definition.
SQL does not use related-but-different metrics.
SQL respects join graph and fanout rules.
SQL grain is compatible with requested output.
SQL does not silently replace net revenue with GMV.
```

Example:

```
User asks:
Show revenue by channel.

Resolved metric:
net_revenue

Generated SQL uses:
gmv_amt

Semantic validation:
Fail. "revenue" was resolved to net_revenue, but SQL used GMV.
```

This catches the most dangerous class of NL2SQL errors: syntactically valid but semantically wrong SQL.

---

## Query execution design

### 12.1 Execution modes

```
dry_run
explain
preview
full_run
export
```

MVP default:

```
dry_run → preview with LIMIT 100
```

Full execution should require user action.

### 12.2 Execution safety controls

```
read-only warehouse account
service-specific DB role
query timeout
row limit
cost threshold
partition scan threshold
no write permissions
audit logging
PII masking
warehouse-level resource group
```

### 12.3 Execution flow

```
1. Parse SQL.
2. Validate SQL.
3. Run EXPLAIN or dry-run.
4. Estimate cost / scanned partitions.
5. Add LIMIT for preview.
6. Execute preview.
7. Return SQL + sample result.
8. Let user approve full execution.
```
