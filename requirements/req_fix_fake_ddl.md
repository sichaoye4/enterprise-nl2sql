# Phase 1: Fix Fake DDL in Context Builder

## Problem
The context builder's `_tables_section()` in `context_builder.py` lists individual columns (name, type, description) for each candidate table. These columns come from `_table_from_semantic_plan()` which only includes certified metric columns — e.g. only `GasStationID (numeric)` but not `Country`, `Segment`, or other columns that exist in the physical table. This "fake DDL" constrains the LLM from using columns that aren't registered as certified measures/dimensions.

## Required Changes

### 1. `src/semantic_registry/pipeline/context_builder.py` — `_tables_section()`

**Stop listing individual columns.** Replace the column listing with enriched natural-language table descriptions that tell the LLM what the table contains as prose.

**Before (current):**
```
Candidate tables:
- orders: Certified semantic source for Paid GMV.
  Columns:
  - paid_gmv_amt (numeric): Measure for Paid GMV.
  - payment_date (date): Time column for Payment Date.
  - channel (text): Sales channel.
```

**After (desired):**
```
Candidate tables:
- orders: Certified semantic source for Paid GMV. This table records order transactions — each row represents an individual order with its payment amount (paid_gmv_amt), payment date (payment_date), and sales channel (channel). Other columns available in the physical table may include region, customer segment, currency, and additional dimension fields — use column names mentioned in the user's question as filter targets even if not explicitly listed here.
```

The enriched description should mention all known metric/dimension columns (from `TableMetadata.columns`) as natural language prose, not as a column listing. The exact wording pattern:
- Start with the existing description
- Add "This table records ... each row represents ..." with info about what metrics/dimensions are available
- End with the caveat about additional columns

**Remove** the `"  Columns:"` section entirely (lines 88-92 in current code and the corresponding column-listing loop).

### 2. `src/semantic_registry/pipeline/context_builder.py` — `_schema_caveat_section()`

**Already exists** — keep it unchanged. It says:
```
Note: Physical tables may have additional columns beyond those listed above...
```
This is sufficient. The fix makes it more impactful by removing the misleading column listing that contradicts it.

### 3. `tests/pipeline/test_context_builder.py` — update test

Change the assertion on line 46:
```python
# Before:
assert "paid_gmv_amt (numeric)" in prompt
# After: check that the table description mentions the metric naturally
assert "Paid GMV" in prompt  # still checks metric is described
assert "paid_gmv_amt" in prompt  # column name still appears in prose description
```

Remove the column-listing-specific assertions like `"paid_gmv_amt (numeric)"` — the new format uses prose, not DDL-style listings.

Keep all other assertions unchanged (question, rules, caveats, etc.).

## Key Design Decisions
- **Don't change TableMetadata model** — columns are still stored and used by validators. Only the *presentation* changes.
- **Don't change RegistryMetadataProvider** — the provider still builds table metadata with columns. Only `_tables_section()` changes how they're rendered.
- **Natural language descriptions are more flexible** — they tell the LLM what the table contains without making it think "these are the only columns."
