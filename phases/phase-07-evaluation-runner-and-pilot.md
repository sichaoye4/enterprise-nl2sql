# Phase 7 — Evaluation Runner and Controlled Pilot

**Duration:** 2 weeks

**Depends on:** All previous phases (Phase 0–6)

**Design references:** [05-Evaluation-and-Observability](../docs/01-product-design.md) (from supplementary docs), [02-Architecture §16 (Core APIs)](../docs/02-architecture.md#core-apis), [02-Architecture §19 (Evaluation design)](../docs/02-architecture.md#evaluation-and-observability)

---

## Overview

Build the offline evaluation runner, regression test suite, and controlled pilot infrastructure. This phase ensures every model/prompt/metadata change can be regression-tested, and enables safe onboarding of pilot users.

---

## Requirements

### R7.1 — Offline eval runner
- Build an evaluation runner that processes eval cases through the NL2SQL pipeline and compares outputs to expected results.
- The runner must support two eval levels:
  - **Semantic resolution eval**: runs the question through term extraction + semantic resolution and compares the semantic plan against the expected plan.
  - **SQL generation eval**: runs the full pipeline (minus execution) and compares generated SQL against gold SQL.
- The runner must compute all metrics defined in the evaluation design: business term extraction accuracy, concept resolution accuracy, ambiguity detection accuracy, metric selection accuracy, dimension selection accuracy, SQL parse success, static validation success, semantic validation success, correct table selection, correct metric selection, correct filter selection, unsafe SQL block rate.
- The runner must output a structured evaluation report.

### R7.2 — Eval case management
- Build a system to manage eval cases in `nl2sql_eval_case` table.
- Each eval case must have: question, domain, difficulty level, expected semantic plan (JSON), gold SQL, required tables list, required columns list, optional expected_result_hash.
- Provide a CLI or API to add, update, activate/deactivate, and delete eval cases.
- Support tagging eval cases by domain and difficulty for targeted test suites.
- Initial eval suite must have at least 200 cases covering the MVP domains.

### R7.3 — Regression test suite
- Define a regression test suite that runs the complete eval suite against the current pipeline version.
- The regression suite must be runnable on demand and as part of CI.
- The regression suite must compare current results against a baseline and report regressions.
- The regression suite must track: model version, metadata snapshot version, pipeline configuration, and eval results for reproducibility.

### R7.4 — Eval dashboard
- Build a dashboard that displays evaluation results over time.
- The dashboard must show: overall success rate by domain, success rate trend over time, failure category breakdown, top failing eval cases, top ambiguous terms, top corrected metrics, latency by pipeline stage.
- The dashboard must support filtering by domain, eval suite, model version, and date range.
- The dashboard must be accessible to both developers and the analytics engineer.

### R7.5 — Model version tracking
- Track which model version was used for each pipeline run and each eval run.
- Store the model version in the query log and eval result tables.
- Support comparing eval results across model versions to detect improvements or regressions.

### R7.6 — Metadata snapshot tracking
- Link each query and each eval run to the metadata snapshot version that was active at the time.
- When metadata changes (new tables, updated descriptions), the eval runner should flag cases whose results may be affected.
- Support re-running eval against an older metadata snapshot for backward compatibility testing.

### R7.7 — Evaluation API
- Build REST endpoints for evaluation:
  - `POST /api/v1/eval/run` — trigger an eval run with parameters: eval_suite, model_version, metadata_snapshot.
  - `GET /api/v1/eval/runs` — list past eval runs with summary results.
  - `GET /api/v1/eval/runs/{run_id}` — get detailed eval run results.
  - `GET /api/v1/eval/cases` — list eval cases with filters by domain, difficulty, active status.
  - `POST /api/v1/eval/cases` — add a new eval case.

### R7.8 — Pilot user onboarding
- Build the controlled pilot infrastructure.
- Pilot users must be able to: log in via SSO, ask questions in supported domains, see the full pipeline output, submit feedback.
- Pilot users must not be able to: run queries outside allowed domains, bypass security controls, access raw execution beyond preview.
- Support a whitelist of pilot user identities.
- Log all pilot user interactions for analysis.

### R7.9 — MVP release checklist
- Before pilot launch, verify all MVP release criteria:
  - All exposed tables are certified.
  - All exposed metrics have owners.
  - Semantic registry covers core terms for pilot domains.
  - SQL validator blocks unsafe queries.
  - Query executor uses read-only role.
  - Preview mode works reliably.
  - Eval suite has at least 200 cases.
  - Audit logging is complete.
  - User feedback is captured.
  - Governance team approves the safety model.

---

## Exit Criteria

- Every model/prompt/metadata change can be regression-tested through the eval runner.
- Eval dashboard is functional and shows per-domain success rates.
- At least 200 eval cases exist for the MVP domains.
- Pilot users can safely use the system on selected domains.
- All MVP release criteria are verified and passing.
- The system is ready for controlled pilot launch.
