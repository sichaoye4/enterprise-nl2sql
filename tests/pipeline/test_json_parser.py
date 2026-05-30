from __future__ import annotations

import pytest

from src.semantic_registry.pipeline.json_parser import StrictJSONParser


def response_json(sql: str = "SELECT paid_gmv_amt AS paid_gmv FROM orders") -> str:
    return f"""{{
  "sql": "{sql}",
  "assumptions": ["read only"],
  "tables_used": ["orders"],
  "columns_used": ["paid_gmv_amt"],
  "confidence": "high",
  "reasoning_summary": "deterministic test response"
}}"""


def test_parse_from_markdown_json_code_block() -> None:
    parser = StrictJSONParser()

    response = parser.parse(f"```json\n{response_json()}\n```")

    assert response.sql.startswith("SELECT")
    assert response.tables_used == ["orders"]


def test_parse_tolerates_trailing_commas() -> None:
    parser = StrictJSONParser()
    raw = """{
      "sql": "SELECT paid_gmv_amt AS paid_gmv FROM orders",
      "assumptions": ["read only",],
      "tables_used": ["orders",],
      "columns_used": ["paid_gmv_amt",],
      "confidence": "high",
      "reasoning_summary": "deterministic test response",
    }"""

    response = parser.parse(raw)

    assert response.confidence == "high"


def test_parse_missing_field_raises_error() -> None:
    parser = StrictJSONParser()
    raw = """{
      "sql": "SELECT paid_gmv_amt AS paid_gmv FROM orders",
      "assumptions": [],
      "tables_used": [],
      "columns_used": [],
      "confidence": "high"
    }"""

    with pytest.raises(ValueError, match="missing required fields"):
        parser.parse(raw)


def test_extract_json_from_wrapped_markdown() -> None:
    parser = StrictJSONParser()

    extracted = parser.extract_json(f"Here is the result:\n```json\n{response_json()}\n```\nDone.")

    assert extracted["sql"].startswith("SELECT")
    assert extracted["tables_used"] == ["orders"]
