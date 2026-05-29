# Product Design — Enterprise NL2SQL Copilot

> 以下内容来自飞书文档 [enterprises nl2sql](https://my.feishu.cn/wiki/DuCFwTAb6imVh5kchfGcMKJ5nJg)，仅做整理切分，未做内容修改。

---

## Product positioning

### 1.1 Product name

Internally, call it something like:

```
Enterprise NL2SQL Copilot
```

### 1.2 Primary users

```
1. Business analysts
2. Data analysts
3. Product managers
4. BI users
5. Analytics engineers
```

### 1.3 MVP promise

The MVP should allow users to ask questions such as:

```
Show paid GMV by channel for the last 30 days.

Compare active users and new users by region last month.

Show conversion rate by campaign for Q1.

List top 20 products by net revenue this week.
```

The system returns:

```
1. Generated SQL
2. Explanation of metric/table/column choice
3. Assumptions
4. Preview result
5. Validation result
6. Warnings or ambiguity clarification
7. Feedback / correction option
```

The product should be a trusted SQL copilot, not a black-box answer bot.

---

## MVP scope

### 2.1 In scope

```
Certified ADS / mart / serving-layer tables
SELECT-only analytical SQL
Business metric queries
Group-by / filter / time-series queries
Basic joins through governed join paths
SQL preview execution
Human feedback capture
Evaluation and regression testing
```

### 2.2 Out of scope for MVP

```
Raw-layer arbitrary table joins
DDL / DML / write-back SQL
PII-heavy exploratory queries
Autonomous dashboard generation
Root-cause analysis
Open-ended "why did this happen?" analysis
Unlimited agent loops
Complex multi-agent tournament selection
Automatic metric invention
```

### 2.3 MVP data scope

Start with:

```
20–50 certified analytical tables
30–50 business terms
20–40 certified metrics
20–50 common dimensions
5–10 core entities
200–300 internal evaluation questions
1–3 business domains first
```

Good first domains:

```
Commerce / order / revenue
User growth
Marketing / campaign performance
```

---

## Core product workflow

Example user question:

```
Show revenue by channel last month.
```

### Step 1: Classify question

```
{
  "domain": "finance",
  "query_type": "metric_by_dimension",
  "risk_level": "low",
  "write_intent": false,
  "sensitive_data_intent": false,
  "requires_time_range": true
}
```

### Step 2: Extract business terms

```
{
  "terms": ["revenue", "channel", "last month"]
}
```

### Step 3: Resolve semantics

The semantic layer checks:

```
revenue could mean:
1. GMV
2. Paid GMV
3. Net Revenue
```

In finance domain, the default rule says:

```
revenue → net_revenue
```

Resolved semantic query:

```
{
  "metric": "net_revenue",
  "dimension": "channel",
  "time_range": "previous_calendar_month",
  "time_semantics": "settlement_date"
}
```

### Step 4: Map to physical metadata

```
{
  "table": "ads_finance_channel_daily",
  "metric_column": "net_revenue_amt",
  "dimension_column": "channel_name",
  "time_column": "settlement_dt"
}
```

### Step 5: Generate SQL

```
SELECT
    channel_name,
    SUM(net_revenue_amt) AS net_revenue
FROM ads_finance_channel_daily
WHERE settlement_dt >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1' MONTH)
  AND settlement_dt < DATE_TRUNC('month', CURRENT_DATE)
GROUP BY channel_name
ORDER BY net_revenue DESC
LIMIT 100;
```

### Step 6: Validate

The system checks:

```
SELECT-only
allowed table
allowed columns
correct metric
correct time column
correct aggregation
partition filter present
no PII violation
cost acceptable
```

### Step 7: Preview and explain

The UI shows:

```
I interpreted "revenue" as Net Revenue because this query is in the finance domain.

Used metric:
net_revenue

Used table:
ads_finance_channel_daily

Used columns:
settlement_dt, channel_name, net_revenue_amt

Assumptions:
"last month" means the previous calendar month.
```

---

## Frontend product design

### 13.1 Main user page

UI sections:

```
Natural-language question input
Clarification prompt, when needed
Generated SQL editor
Assumptions
Tables and columns used
Semantic interpretation
Validation result
Preview result
Feedback buttons
Query history
```

### 13.2 Recommended UI layout

```
┌─────────────────────────────────────────────┐
│ Ask a data question                          │
│ [ Show paid GMV by channel last 30 days ]    │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ Semantic interpretation                       │
│ Metric: Paid GMV                              │
│ Dimension: Channel                            │
│ Time: Payment date                            │
│ Table: ads_order_channel_daily                │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ Generated SQL                                │
│ [ Monaco SQL editor ]                        │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ Validation                                   │
│ ✅ SELECT-only                                │
│ ✅ Certified table                            │
│ ✅ Partition filter                           │
│ ✅ Correct metric                             │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ Preview result                               │
│ [ table grid ]                               │
└─────────────────────────────────────────────┘

[Run full query] [Edit SQL] [Correct] [Wrong] [Save as example]
```

### 13.3 Frontend stack

Recommended:

```
React / Next.js
Monaco Editor for SQL
Data grid component for preview result
```
