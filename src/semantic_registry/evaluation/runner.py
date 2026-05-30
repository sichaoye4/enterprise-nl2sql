from __future__ import annotations

from typing import Any

import sqlglot

from src.semantic_registry.evaluation.compare import compare_plans, compare_sql
from src.semantic_registry.evaluation.models import CaseResult, EvalCase, EvalResult
from src.semantic_registry.pipeline import NL2SQLPipeline
from src.semantic_registry.pipeline.state_machine import PipelineContext
from src.semantic_registry.validation.parser import extract_tables, parse_sql


class EvalRunner:
    def run_semantic_eval(self, cases: list[EvalCase], pipeline: NL2SQLPipeline) -> EvalResult:
        case_results: list[CaseResult] = []
        term_scores: list[float] = []
        concept_scores: list[float] = []
        ambiguity_scores: list[float] = []

        for case in cases:
            context = PipelineContext(question=case.question, domain=case.domain)
            context = pipeline.classify(context)
            if context.error is None:
                context = pipeline.extract_terms(context)
                context = pipeline.resolve_semantics(context)

            generated_plan = context.semantic_plan.model_dump(mode="json") if context.semantic_plan else {}
            plan_comparison = compare_plans(generated_plan, case.expected_semantic_plan)
            term_score = self._term_extraction_score(context, case)
            concept_score = self._concept_resolution_score(generated_plan, case.expected_semantic_plan)
            ambiguity_score = self._ambiguity_score(generated_plan, case.expected_semantic_plan, context)
            term_scores.append(term_score)
            concept_scores.append(concept_score)
            ambiguity_scores.append(ambiguity_score)

            errors = list(plan_comparison.differences)
            if context.error:
                errors.append(context.error)
            passed = plan_comparison.exact_match and not context.error
            case_results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    errors=errors,
                    generated_sql=None,
                    generated_plan=generated_plan,
                    expected_plan=case.expected_semantic_plan,
                    gold_sql=case.gold_sql,
                    comparison_details={
                        "plan": plan_comparison.model_dump(mode="json"),
                        "term_extraction_accuracy": term_score,
                        "concept_resolution_accuracy": concept_score,
                        "ambiguity_detection_accuracy": ambiguity_score,
                    },
                )
            )

        return self._result(
            case_results,
            {
                "term_extraction_accuracy": self._average(term_scores),
                "concept_resolution_accuracy": self._average(concept_scores),
                "ambiguity_detection_accuracy": self._average(ambiguity_scores),
            },
        )

    def run_sql_eval(self, cases: list[EvalCase], pipeline: NL2SQLPipeline) -> EvalResult:
        case_results: list[CaseResult] = []
        metrics: dict[str, list[float]] = {
            "sql_parse_success": [],
            "static_validation_success": [],
            "semantic_validation_success": [],
            "correct_table_selection": [],
            "correct_metric_selection": [],
            "unsafe_sql_block_rate": [],
        }

        for case in cases:
            context = pipeline.run(case.question, domain=case.domain)
            generated_sql = context.response.generated_sql if context.response else ""
            generated_plan = context.semantic_plan.model_dump(mode="json") if context.semantic_plan else {}
            sql_comparison = compare_sql(generated_sql, case.gold_sql) if generated_sql or case.gold_sql else None
            errors: list[str] = []
            if context.error:
                errors.append(context.error)
            if sql_comparison and not sql_comparison.structurally_similar:
                errors.extend(sql_comparison.differences)

            parse_success = self._sql_parses(generated_sql)
            static_success = self._validation_check(context, "static")
            semantic_success = self._validation_check(context, "semantic")
            table_success = self._table_selection_matches(generated_sql, case.required_tables)
            metric_success = generated_plan.get("metric") == case.expected_semantic_plan.get("metric")
            unsafe_blocked = self._unsafe_case(case) and bool(context.error) and not generated_sql

            metrics["sql_parse_success"].append(float(parse_success))
            metrics["static_validation_success"].append(float(static_success))
            metrics["semantic_validation_success"].append(float(semantic_success))
            metrics["correct_table_selection"].append(float(table_success))
            metrics["correct_metric_selection"].append(float(metric_success))
            if self._unsafe_case(case):
                metrics["unsafe_sql_block_rate"].append(float(unsafe_blocked))

            passed = bool(sql_comparison and sql_comparison.structurally_similar) and not context.error
            if self._unsafe_case(case):
                passed = unsafe_blocked
            case_results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    errors=errors,
                    generated_sql=generated_sql or None,
                    generated_plan=generated_plan,
                    expected_plan=case.expected_semantic_plan,
                    gold_sql=case.gold_sql,
                    comparison_details={
                        "sql": sql_comparison.model_dump(mode="json") if sql_comparison else None,
                        "sql_parse_success": parse_success,
                        "static_validation_success": static_success,
                        "semantic_validation_success": semantic_success,
                        "correct_table_selection": table_success,
                        "correct_metric_selection": metric_success,
                        "unsafe_sql_blocked": unsafe_blocked,
                    },
                )
            )

        return self._result(case_results, {key: self._average(values) for key, values in metrics.items()})

    def _result(self, case_results: list[CaseResult], metrics: dict[str, float]) -> EvalResult:
        passed = sum(1 for result in case_results if result.passed)
        total = len(case_results)
        return EvalResult(
            total_cases=total,
            passed=passed,
            failed=total - passed,
            success_rate=(passed / total) if total else 0.0,
            case_results=case_results,
            metrics=metrics,
        )

    def _term_extraction_score(self, context: PipelineContext, case: EvalCase) -> float:
        expected_terms = {
            value
            for value in (
                case.expected_semantic_plan.get("metric"),
                case.expected_semantic_plan.get("dimension"),
            )
            if value
        }
        if not expected_terms:
            return 1.0
        extracted = {term.term for term in context.extracted_terms}
        return len(expected_terms & extracted) / len(expected_terms)

    def _concept_resolution_score(self, generated: dict[str, Any], expected: dict[str, Any]) -> float:
        fields = ["metric", "dimension", "domain"]
        expected_fields = [field for field in fields if expected.get(field) is not None]
        if not expected_fields:
            return 1.0
        return sum(1 for field in expected_fields if generated.get(field) == expected.get(field)) / len(expected_fields)

    def _ambiguity_score(self, generated: dict[str, Any], expected: dict[str, Any], context: PipelineContext) -> float:
        generated_value = bool(generated.get("requires_clarification") or context.requires_clarification)
        expected_value = bool(expected.get("requires_clarification"))
        return float(generated_value == expected_value)

    def _sql_parses(self, sql: str) -> bool:
        if not sql:
            return False
        try:
            sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            return False
        return True

    def _validation_check(self, context: PipelineContext, section: str) -> bool:
        selected = context.selected_sql
        if selected is None or not selected.validation_results:
            return False
        result = selected.validation_results.get(section)
        return bool(result and result.get("passed"))

    def _table_selection_matches(self, generated_sql: str, required_tables: list[str]) -> bool:
        if not required_tables:
            return True
        if not generated_sql:
            return False
        try:
            tables = set(extract_tables(parse_sql(generated_sql)))
        except Exception:
            return False
        required = set(required_tables)
        unqualified = {table.rsplit(".", 1)[-1] for table in tables}
        return required.issubset(tables) or required.issubset(unqualified)

    def _unsafe_case(self, case: EvalCase) -> bool:
        return any(tag in case.tags for tag in ["unsafe", "write_intent", "pii", "sensitive"]) or case.gold_sql == ""

    def _average(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0
