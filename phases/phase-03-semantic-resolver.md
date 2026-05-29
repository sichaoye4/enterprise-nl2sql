# Phase 3 — Semantic Resolver

**Duration:** 2 weeks

**Depends on:** Phase 1 (semantic registry), Phase 2 (metadata retrieval)

**Design references:** [02-Architecture §6 (Semantic layer)](../docs/02-architecture.md#semantic-layer-design), [§9 (NL2SQL runtime pipeline)](../docs/02-architecture.md#nl2sql-runtime-pipeline)

---

## Overview

Build the semantic resolver that translates natural language business terms into a structured semantic query plan. This is the core reasoning component — it extracts business terms from the user question, resolves them against the semantic registry, detects ambiguity, and produces a machine-readable semantic query plan.

---

## Requirements

### R3.1 — Term extractor
- Build a component that extracts business terms from a natural language question.
- The extractor must handle multi-word terms (e.g., "paid GMV", "net revenue", "last month", "active user").
- The extractor must use the semantic registry's term list and synonyms to identify known business terms.
- Output: a list of extracted terms with their positions in the original question.

### R3.2 — Synonym matcher
- When a term is not found by exact match, search the synonym lists of all registered terms.
- Return all candidate terms whose synonyms match, ranked by match confidence (exact synonym match > partial match).
- If multiple terms share a synonym, flag all of them as candidates.

### R3.3 — Business concept resolver
- For each extracted (or synonym-matched) term, determine which concept(s) it could map to.
- Use the term's `candidate_concepts` list and the question's detected domain to apply `default_concept_by_domain` rules.
- If a clear domain-specific default exists, resolve silently.
- If multiple concepts are equally plausible, flag for clarification (do not guess).

### R3.4 — Domain detection and default rules
- Detect the business domain of the question from: explicit user-provided domain parameter, terms that are domain-specific, or historical user query patterns.
- Apply domain-specific default rules (e.g., in finance domain, "revenue" → "net_revenue"; in commerce domain, "revenue" → "paid_gmv").
- If domain cannot be detected and ambiguity exists, require clarification.

### R3.5 — Ambiguity detector
- Detect when a term could resolve to multiple concepts without a clear default.
- Detect when a metric has multiple valid dimensions that the user didn't specify.
- Detect when time semantics are ambiguous (e.g., "last month" could mean calendar month, fiscal month, or trailing 30 days).
- When ambiguity is detected, generate a clarification question for the user rather than silently choosing.

### R3.6 — Semantic query plan generator
- Produce a structured semantic query plan from the resolved terms.
- The plan must include: `metric` (resolved metric name), `dimension` (resolved dimension name), `time_range` (resolved time range expression), `time_semantics` (which date column to use), `domain` (detected domain), `filters` (any additional filter conditions).
- The plan must use the semantic registry's resolved names, not the original user phrasing.
- The plan must be machine-readable JSON suitable for the next pipeline stage (metadata retrieval).

### R3.7 — Resolution order
- Implement the resolution order as specified in the architecture: (1) exact match → (2) synonym match → (3) domain-specific rule → (4) embedding retrieval → (5) LLM-based judge → (6) ask clarification.
- Each step must only be attempted if the previous step did not produce a definitive result.
- The LLM-based judge (step 5) should be a lightweight classification prompt, not a full generation call.

### R3.8 — Clarification response
- When clarification is needed, build a user-facing response that explains which terms are ambiguous and what the options are.
- The clarification must present specific choices (e.g., "Do you mean 'net revenue' or 'paid GMV' when you say 'revenue'?").
- The clarification must not reveal internal system details or raw table names.

---

## Exit Criteria

- Term extraction correctly identifies known business terms from sample questions.
- Semantic resolution accuracy is adequate on the internal eval set.
- Ambiguous terms trigger clarification instead of silent guessing.
- Semantic query plan is produced in the correct JSON format for downstream consumption.
- The six-step resolution order is fully implemented and testable.
