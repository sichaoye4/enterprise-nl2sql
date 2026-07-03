from src.semantic_registry.evaluation.cases import EvalCaseStore
from src.semantic_registry.evaluation.compare import ComparisonResult, PlanComparisonResult, compare_plans, compare_sql
from src.semantic_registry.evaluation.models import CaseResult, EvalCase, EvalResult
from src.semantic_registry.evaluation.pilot import PilotManager


def __getattr__(name: str):
    if name == "EvalRunner":
        from src.semantic_registry.evaluation.runner import EvalRunner

        return EvalRunner
    raise AttributeError(name)

__all__ = [
    "CaseResult",
    "ComparisonResult",
    "EvalCase",
    "EvalCaseStore",
    "EvalResult",
    "EvalRunner",
    "PilotManager",
    "PlanComparisonResult",
    "compare_plans",
    "compare_sql",
]
