# Semantic-engine enhancement plan for Enterprise NL2SQL

## Purpose and success criterion

This POC should prove that semantic modeling improves BIRD execution accuracy over the existing raw-LLM baseline. It is not primarily a governance project. The architecture should therefore preserve the original NL2SQL pipeline as a reliable fallback, while using semantic models to (1) deterministically answer well-covered repeated analytics questions and (2) give the LLM stronger semantic context for every other question.

Target execution paths:

```text
Question
  -> semantic-engine analysis and route decision
  -> deterministic semantic SQL OR semantic-assisted LLM OR baseline LLM
  -> one shared validation and repair pipeline
  -> candidate selection
  -> semantic-aware LLM judge
  -> response and benchmark record
```

Success is measured by a reproducible, apples-to-apples BIRD run showing route-level and total execution accuracy versus the unchanged baseline prompt/model configuration.

## Current issues owned by this repository

1. The semantic engine is invoked early, but is not the authoritative orchestrator. On `CLARIFY`, the old resolver and retriever take over; on no semantic coverage, `BLOCKED` terminates instead of using the baseline.
2. A core `SEMANTIC_SQL` result is sent back through the SQL-generation LLM as a seed. This risks changing correct deterministic SQL.
3. The local LLM semantic router is a member resolver, not a route selector. It only runs after `CLARIFY` or no route and can only turn a question into `SEMANTIC_SQL`.
4. The router-compiled candidate is selected before the stage skip logic, which skips shared validation, repair, and selection. The core compiler path has the opposite problem: it unnecessarily invokes the LLM.
5. Guardrail contracts and engine resolution results are prompt additions, not first-class data passed through validation, repair, and judging.
6. The LLM judge receives the small legacy semantic plan rather than full semantic resolution, lineage, contract, and route rationale. A judge rejection of `SEMANTIC_SQL` is accepted as a warning.
7. Benchmark scripts, paths, dialect handling, and result artifacts are inconsistent. Existing committed semantic results do not demonstrate an uplift.
8. Local package/model discovery relies on developer-home paths and a Linux-only file dependency, which prevents reproducible POC runs.

## Target route contract

Replace the orchestration use of `CLARIFY`/`BLOCKED` for normal analytical questions with an explicit application-level route contract:

| Route | When used | SQL producer |
| --- | --- | --- |
| `SEMANTIC_SQL` | Fully modeled query with deterministic compiler support | Semantic compiler only |
| `SEMANTIC_ASSISTED_LLM` | Useful governed members/context exist, but composition is advanced or coverage is partial | Existing LLM generator with semantic context |
| `BASELINE_LLM` | No useful semantic coverage or semantic router confidence is low | Existing baseline generator, unchanged except optional neutral hints |
| `CLARIFY` | User intent is genuinely ambiguous and no safe interpretation is possible | No SQL |
| `REJECTED` | Write/destructive request or malformed request | No SQL |

`CLARIFY` and `REJECTED` are terminal. `BASELINE_LLM` is the expected route for ordinary BIRD questions outside the modeled surface.

## Workstream A: refactor the state machine around routes

### A1. Introduce a route-result adapter

Create one adapter between `SemanticPipeline.process()` and `PipelineContext`. It should preserve the complete semantic result rather than reduce it to `metric`, `dimension`, and a prompt string.

Add to `PipelineContext`:

- `semantic_result`: serialized route result, resolution, selected view, compiler lineage, snapshot/version, and gap report;
- `route_decision`: final application route and source (`rules`, `llm_router`, or `fallback`);
- `semantic_context`: compact LLM-facing context for assisted generation;
- `compiled_candidate`: the unmodified deterministic candidate, when available;
- `judge_history`: every judge decision and resulting route transition.

Acceptance criteria:

- no later stage reloads or reinterprets semantic facts from a different YAML format;
- every final result can identify the route and the semantic inputs used;
- tests cover conversion of every engine route.

### A2. Make routing explicit and non-terminal by default

Implement application-level mapping:

- engine `SEMANTIC_SQL` -> `SEMANTIC_SQL`;
- engine advanced/guarded result -> `SEMANTIC_ASSISTED_LLM`;
- engine partial/no coverage -> ask the LLM route selector; fall back to `BASELINE_LLM` when router confidence or validation is insufficient;
- genuine ambiguity -> `CLARIFY` only when the route selector also cannot make a supported interpretation;
- destructive/write intent -> `REJECTED`.

Do not let a semantic-model gap alone produce a terminal error during BIRD evaluation.

### A3. Preserve deterministic compiler output

For `SEMANTIC_SQL`, create an `SQLCandidate` directly from compiler SQL plus parameters and lineage. Do not call `CandidateGenerator` to review or rewrite it. Send it through the shared validator and the judge unchanged.

If the judge or validator rejects it, transition deliberately to `SEMANTIC_ASSISTED_LLM` or `BASELINE_LLM`; record the reason. Do not silently accept a failed judge result.

## Workstream B: make semantic assistance useful outside modeled queries

### B1. Add a semantic-context builder

For `SEMANTIC_ASSISTED_LLM`, compose a bounded context from the engine result:

- resolved and near-match measures/dimensions/identifiers;
- selected or candidate views and relevant entities;
- safe join paths and physical table/column mappings;
- relevant metric expressions, default time semantics, and filters;
- unresolved terms and suggested substitutions;
- compiler-produced fragments or CTEs when available.

This context should augment the existing baseline schema/retrieval prompt; it must not replace the baseline metadata needed for BIRD’s unmodeled queries.

### B2. Keep the baseline comparable

Extract the current baseline prompt construction into a reusable `BaselineGenerationStrategy`. The baseline route must use exactly the prompt/model/few-shot configuration used for the benchmark control. The semantic-assisted route should reuse that strategy with a clearly delimited semantic-context section.

### B3. Retain multi-candidate generation where it helps

For LLM routes, continue direct and plan-first candidates. Optionally add a third semantic-plan-first candidate, but make it an experiment behind configuration so it cannot obscure comparison with the baseline.

## Workstream C: make the LLM router a real route selector

Replace the current "one measure plus filters" router response with a structured decision:

```json
{
  "route": "SEMANTIC_SQL | SEMANTIC_ASSISTED_LLM | BASELINE_LLM | CLARIFY",
  "confidence": 0.0,
  "resolved_members": ["entity.member"],
  "filters": [],
  "reason": "..."
}
```

Implementation requirements:

- run it whenever deterministic rule-based analysis is partial, uncertain, or lacks coverage—not only after `CLARIFY`;
- validate all selected members and filters against the compiled semantic snapshot;
- independently verify compiler eligibility in code; the router cannot self-authorize `SEMANTIC_SQL`;
- use calibrated thresholds determined from held-out BIRD development questions;
- send low-confidence/invalid selections to `BASELINE_LLM`, never to fabricated SQL;
- support router outputs with zero, one, or multiple measures where the engine can represent them.

Metrics: route accuracy, deterministic-route precision, assisted-route execution accuracy, confidence calibration, and fallback rate.

## Workstream D: one candidate, validation, repair, and selection lifecycle

Every route that produces SQL must follow the same lifecycle:

```text
produce candidate(s)
  -> parse and static checks
  -> route-aware semantic checks
  -> execution/dialect compatibility checks for BIRD
  -> repair candidate(s) when repairable
  -> select the best valid candidate
  -> LLM judge
```

### D1. Remove state-machine skip asymmetry

Do not skip `validate`, `repair`, or `select` merely because an SQL candidate was compiled by the router. The only stages that a deterministic candidate may skip are LLM generation and LLM repair when its failure is non-repairable.

### D2. Add a validation facade

Expose one `validate_candidate(candidate, route, semantic_result, metadata)` facade in this repo. It should delegate to:

- compiler/lineage consistency checks for `SEMANTIC_SQL`;
- the semantic engine contract validator for `SEMANTIC_ASSISTED_LLM` where a contract exists;
- current static/semantic/partition checks for all LLM SQL;
- SQLite dialect/execution preflight in BIRD mode.

Normalize all outputs into the existing candidate validation shape so the repair loop and selector do not need route-specific branches.

### D3. Route-aware repair

- `SEMANTIC_SQL`: do not ask an LLM to casually edit a compiler query. Route back to semantic compilation or an assisted LLM path with failure feedback.
- `SEMANTIC_ASSISTED_LLM`: include the complete semantic context and validator hints in the repair prompt.
- `BASELINE_LLM`: retain the existing repair approach for a fair baseline, with only standard validation errors.

## Workstream E: strengthen the final LLM judge for accuracy

Pass the judge a compact but complete semantic payload: route decision/reason, resolved members, selected view, compiler lineage, model-derived filters, join path, and contract/semantic context. Keep the question and final SQL.

Judge outcomes:

- pass -> response;
- reject a compiler query -> semantic recompile if possible, otherwise assisted/baseline fallback;
- reject an assisted/baseline query -> repair/re-generate up to configured limit;
- unavailable judge -> mark verdict unavailable and retain validated SQL; do not claim it passed the judge.

Start with a single judge model but add a configuration switch for holdout evaluation with a second judge or deterministic execution comparison. Track judge precision/recall against BIRD execution results to ensure it helps rather than merely adds latency.

## Workstream F: make BIRD evaluation the control loop

### F1. One benchmark harness

Consolidate semantic-only, router, and full-pipeline scripts into one command with modes:

- `baseline` — original raw LLM prompt/model settings;
- `semantic_only` — compiler coverage and compiler execution accuracy;
- `semantic_assisted` — semantic context plus LLM;
- `full` — routing, validation, repair, selection, and judge.

Use one frozen question-index manifest, BIRD database version, model versions, temperature/reasoning settings, timeout, retry policy, and SQLite execution adapter.

### F2. Report by route and error class

Persist JSONL case records and a summary containing:

- total EX and per-database/difficulty EX;
- route count, route accuracy, and route-level EX;
- compiler coverage and compiler EX;
- baseline vs assisted delta on the same cases;
- validation failures, repair outcomes, judge accept/reject outcomes;
- SQL dialect/parameter conversion failures separately from semantic errors.

### F3. Establish a progressive target

1. Match baseline EX with fallback enabled.
2. Demonstrate higher EX on the subset selected for `SEMANTIC_SQL` without reducing total EX.
3. Demonstrate semantic-assisted uplift on partial-coverage questions.
4. Expand semantic models only where error analysis shows repeatable opportunity.

## Workstream G: reproducible local integration

1. Replace developer-home path discovery with explicit settings: `SEMANTIC_ENGINE_MODEL_ROOT` and normal installed-package imports.
2. Replace the Linux-only `file:///home/teddy/semantic_modeling` dependency with a workspace/editable install documented for Windows and Linux.
3. Provide a Python 3.11+ lock/install workflow and a single `run_bird` command.
4. Add a smoke test that loads a BIRD model, routes a question, compiles a query, validates it, and executes it against SQLite.

## Delivery sequence

| Phase | Deliverable | Exit test |
| --- | --- | --- |
| 0 | Reproducible environment and unified harness | Baseline BIRD result reproduced within agreed tolerance |
| 1 | Route-result adapter and explicit fallback routes | No normal unmodeled BIRD question terminates as `BLOCKED` |
| 2 | Deterministic candidate lifecycle | Compiler SQL reaches validation/judge without LLM rewrite |
| 3 | Semantic-assisted context and validation facade | Assisted candidate carries structured semantic context through repair |
| 4 | LLM route selector | Held-out route metrics and calibrated fallback threshold reported |
| 5 | Judge feedback transitions | Rejected SQL is rerouted/repaired, never silently accepted as pass |
| 6 | Controlled BIRD comparison | Route-level and total EX comparison is reproducible |

## Files likely to change

- `src/semantic_registry/pipeline/state_machine.py`
- `src/semantic_registry/pipeline/semantic_router.py`
- `src/semantic_registry/pipeline/context_builder.py`
- `src/semantic_registry/pipeline/candidate_generator.py`
- `src/semantic_registry/pipeline/semantic_judge.py`
- `src/semantic_registry/repair/repair_loop.py`
- `src/semantic_registry/validation/orchestrator.py`
- `scripts/run_bird_full_eval.py` and the consolidated benchmark command
- pipeline, router, validation, repair, judge, and end-to-end benchmark tests

## Dependencies on semantic_modeling

This repository needs the semantic engine to provide a stable route result, compact semantic context, a compiler candidate with SQL/parameters/lineage, and a route-aware validation interface. The corresponding engine plan is in the sibling repository’s `docs/enterprise-nl2sql-integration-enhancement-plan.md`.
