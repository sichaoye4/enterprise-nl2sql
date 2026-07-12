from __future__ import annotations

import re

from pydantic import BaseModel


class QuestionClassification(BaseModel):
    domain: str | None = None
    query_type: str
    risk_level: str
    write_intent: bool
    sensitive_data_intent: bool
    requires_time_range: bool
    reasoning: str


class QuestionClassifier:
    WRITE_KEYWORDS = (
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "modify",
        "remove",
        "add",
    )
    PII_KEYWORDS = (
        "email",
        "ssn",
        "social security",
        "password",
        "credit card number",
        "card number",
    )
    TIME_KEYWORDS = (
        "yesterday",
        "today",
        "last",
        "this",
        "previous",
        "month",
        "week",
        "quarter",
        "year",
        "daily",
        "monthly",
    )

    def classify(self, question: str) -> QuestionClassification:
        normalized = question.lower()
        write_intent = self._contains_any(normalized, self.WRITE_KEYWORDS)
        sensitive_intent = self._contains_any(normalized, self.PII_KEYWORDS)
        has_time = self._contains_any(normalized, self.TIME_KEYWORDS)
        query_type = self._query_type(normalized, has_time)
        domain = self._domain(normalized)
        risk_level = self._risk_level(write_intent, sensitive_intent)
        reasoning = self._reasoning(query_type, risk_level, write_intent, sensitive_intent, has_time)
        return QuestionClassification(
            domain=domain,
            query_type=query_type,
            risk_level=risk_level,
            write_intent=write_intent,
            sensitive_data_intent=sensitive_intent,
            requires_time_range=has_time,
            reasoning=reasoning,
        )

    def _contains_any(self, normalized: str, keywords: tuple[str, ...]) -> bool:
        return any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords)

    def _query_type(self, normalized: str, has_time: bool) -> str:
        if self._contains_any(normalized, ("compare", "vs", "versus")):
            return "comparison"
        if re.search(r"\btop\s+\d+\b|\btop\b", normalized):
            return "top_N"
        if self._contains_any(normalized, ("trend", "over time", "daily", "monthly", "weekly", "yearly")):
            return "time_series"
        if self._contains_any(normalized, ("list", "show all")):
            return "list"
        if self._contains_any(normalized, ("by", "per", "grouped by")):
            return "metric_by_dimension"
        if has_time and self._contains_any(normalized, ("day", "week", "month", "quarter", "year")):
            return "time_series"
        return "unknown"

    def _domain(self, normalized: str) -> str | None:
        domain_keywords = {
            "finance": ("finance", "net revenue", "settlement", "refund", "commission"),
            "commerce": ("commerce", "gmv", "order", "paid sales", "buyer"),
            "marketing": ("marketing", "campaign", "click", "impression", "conversion"),
            "growth": ("growth", "active user", "new user", "registration"),
        }
        for domain, keywords in domain_keywords.items():
            if self._contains_any(normalized, keywords):
                return domain
        return None

    def _risk_level(self, write_intent: bool, sensitive_intent: bool) -> str:
        if write_intent:
            return "high"
        if sensitive_intent:
            return "medium"
        return "low"

    def _reasoning(
        self,
        query_type: str,
        risk_level: str,
        write_intent: bool,
        sensitive_intent: bool,
        has_time: bool,
    ) -> str:
        signals = [f"query_type={query_type}", f"risk_level={risk_level}"]
        if write_intent:
            signals.append("write keyword detected")
        if sensitive_intent:
            signals.append("sensitive data keyword detected")
        if has_time:
            signals.append("time keyword detected")
        return "; ".join(signals)
