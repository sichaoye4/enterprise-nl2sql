from __future__ import annotations

from pathlib import Path

from src.semantic_registry.evaluation.cases import EvalCaseStore
from src.semantic_registry.evaluation.models import EvalCase


def make_case(case_id: str = "case_1", domain: str = "commerce", tags: list[str] | None = None) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question="show paid GMV",
        domain=domain,
        difficulty="easy",
        expected_semantic_plan={
            "metric": "paid_gmv",
            "dimension": None,
            "time_range": None,
            "time_semantics": "payment_date",
            "domain": domain,
            "filters": [],
        },
        gold_sql="SELECT SUM(paid_gmv_amt) AS paid_gmv FROM ads_order_channel_daily",
        required_tables=["ads_order_channel_daily"],
        required_columns=["paid_gmv_amt"],
        active=True,
        tags=tags or [],
    )


def test_add_list_get_update_delete_cases() -> None:
    store = EvalCaseStore()
    case = make_case()

    store.add_case(case)
    assert store.get_case("case_1") == case
    assert store.list_cases() == [case]

    updated = store.update_case("case_1", {"difficulty": "medium"})
    assert updated is not None
    assert updated.difficulty == "medium"

    assert store.delete_case("case_1") is True
    assert store.get_case("case_1") is None


def test_filter_by_domain() -> None:
    store = EvalCaseStore([make_case("commerce_case", "commerce"), make_case("finance_case", "finance")])

    cases = store.list_cases(domain="finance")

    assert [case.case_id for case in cases] == ["finance_case"]


def test_load_cases_from_yaml(tmp_path: Path) -> None:
    yaml_file = tmp_path / "cases.yaml"
    yaml_file.write_text(
        """
cases:
  - case_id: yaml_case
    question: show paid GMV
    domain: commerce
    difficulty: easy
    expected_semantic_plan:
      metric: paid_gmv
      dimension:
      time_range:
      time_semantics: payment_date
      domain: commerce
      filters: []
    gold_sql: SELECT SUM(paid_gmv_amt) AS paid_gmv FROM ads_order_channel_daily
    required_tables: [ads_order_channel_daily]
    required_columns: [paid_gmv_amt]
    active: true
    tags: [yaml]
    created_at: "2026-05-29T00:00:00+00:00"
""",
    )

    cases = EvalCaseStore.load_cases_from_yaml(str(tmp_path))

    assert len(cases) == 1
    assert cases[0].case_id == "yaml_case"

