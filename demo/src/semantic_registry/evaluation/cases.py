from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.semantic_registry.evaluation.models import EvalCase


class EvalCaseStore:
    def __init__(self, cases: list[EvalCase] | None = None) -> None:
        self._cases: dict[str, EvalCase] = {}
        for case in cases or []:
            self.add_case(case)

    def add_case(self, case: EvalCase) -> None:
        self._cases[case.case_id] = case

    def update_case(self, case_id: str, updates: dict[str, Any]) -> EvalCase | None:
        case = self.get_case(case_id)
        if case is None:
            return None
        updated = case.model_copy(update=updates)
        self._cases[case_id] = updated
        return updated

    def delete_case(self, case_id: str) -> bool:
        return self._cases.pop(case_id, None) is not None

    def get_case(self, case_id: str) -> EvalCase | None:
        return self._cases.get(case_id)

    def list_cases(
        self,
        domain: str | None = None,
        difficulty: str | None = None,
        active_only: bool = True,
        tags: list[str] | None = None,
    ) -> list[EvalCase]:
        required_tags = set(tags or [])
        cases = list(self._cases.values())
        if active_only:
            cases = [case for case in cases if case.active]
        if domain is not None:
            cases = [case for case in cases if case.domain == domain]
        if difficulty is not None:
            cases = [case for case in cases if case.difficulty == difficulty]
        if required_tags:
            cases = [case for case in cases if required_tags.issubset(set(case.tags))]
        return sorted(cases, key=lambda case: case.case_id)

    @staticmethod
    def load_cases_from_yaml(directory: str) -> list[EvalCase]:
        cases: list[EvalCase] = []
        path = Path(directory)
        if not path.exists():
            return cases
        for yaml_file in sorted([*path.glob("*.yaml"), *path.glob("*.yml")]):
            data = yaml.safe_load(yaml_file.read_text()) or {}
            raw_cases = data.get("cases", data if isinstance(data, list) else [data])
            for raw_case in raw_cases:
                if raw_case:
                    cases.append(EvalCase.model_validate(raw_case))
        return cases

