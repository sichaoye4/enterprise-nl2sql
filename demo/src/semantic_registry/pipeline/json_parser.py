from __future__ import annotations

import ast
import json
import re
from typing import Any

import yaml

from src.semantic_registry.pipeline.llm_gateway_types import LLMResponse


class StrictJSONParser:
    REQUIRED_FIELDS = {
        "sql",
        "assumptions",
        "tables_used",
        "columns_used",
        "confidence",
        "reasoning_summary",
    }

    def parse(self, raw: str) -> LLMResponse:
        data = self.extract_json(raw)
        missing = sorted(self.REQUIRED_FIELDS - set(data))
        if missing:
            raise ValueError(f"LLM response missing required fields: {', '.join(missing)}")
        return LLMResponse.model_validate(data)

    def extract_json(self, text: str) -> dict[str, Any]:
        candidate = self._extract_code_block(text) or self._extract_balanced_object(text)
        if candidate is None:
            raise ValueError("No JSON object found in LLM response")
        parsed = self._loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
        return parsed

    def _extract_code_block(self, text: str) -> str | None:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _extract_balanced_object(self, text: str) -> str | None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    in_string = False
                continue
            if char in ("'", '"'):
                in_string = True
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    def _loads(self, candidate: str) -> Any:
        cleaned = self._remove_trailing_commas(candidate.strip())
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        try:
            return ast.literal_eval(cleaned)
        except (SyntaxError, ValueError):
            pass
        try:
            return yaml.safe_load(cleaned)
        except yaml.YAMLError as exc:
            raise ValueError(f"Could not parse JSON from LLM response: {exc}") from exc

    def _remove_trailing_commas(self, text: str) -> str:
        return re.sub(r",\s*([}\]])", r"\1", text)
