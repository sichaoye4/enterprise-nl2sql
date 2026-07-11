from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator

from src.semantic_registry.pipeline.json_parser import StrictJSONParser


def _ensure_semantic_engine_path() -> None:
    try:
        import semantic_engine  # noqa: F401
    except ModuleNotFoundError:
        source_path = Path(__file__).resolve().parents[4] / "semantic_modeling" / "src"
        if not source_path.exists():
            source_path = Path.home() / "semantic_modeling" / "src"
        if str(source_path) not in sys.path:
            sys.path.insert(0, str(source_path))


_ensure_semantic_engine_path()

from semantic_engine.compiler.sql_compiler import compile_sql  # noqa: E402
from semantic_engine.models.catalog import SemanticModelSnapshot  # noqa: E402
from semantic_engine.models.query_ir import CompiledQuery, FilterIR  # noqa: E402
from semantic_engine.models.resolution import (  # noqa: E402
    CoverageResult,
    JoinPathCoverage,
    ResolutionResult,
    RouteDecision,
    TermResolution,
    ViewSelection,
)


SUPPORTED_FILTER_OPERATORS = {
    "equals",
    "not_equals",
    "not equals",
    "like",
    "not_like",
    "not like",
    "contains",
    "starts_with",
    "ends_with",
    "gt",
    "greater_than",
    "gte",
    ">=",
    "lt",
    "less_than",
    "lte",
    "<=",
    "between",
}
SUPPORTED_GRANULARITIES = {"day", "week", "month", "quarter", "year"}


class RouterResult(BaseModel):
    measure: str
    dimensions: list[str] = Field(default_factory=list)
    time_dimension: str | None = None
    granularity: str | None = None
    filters: list[FilterIR] = Field(default_factory=list)
    confidence: float = 0.0


class _RouterResponse(BaseModel):
    measure: str
    dimensions: list[str] = Field(default_factory=list)
    time_dimension: str | None = None
    granularity: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator("filters")
    @classmethod
    def _filters_use_supported_operators(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for filter_ in value:
            if not isinstance(filter_, dict):
                raise ValueError("Router filters must be objects.")
            if str(filter_.get("operator", "")).lower() not in SUPPORTED_FILTER_OPERATORS:
                raise ValueError(f"Unsupported filter operator {filter_.get('operator')!r}.")
        return value


def build_router_prompt(
    catalog_snapshot: SemanticModelSnapshot,
    question: str,
    db_id: str | None = None,
) -> str:
    db_line = f"\nDatabase ID: {db_id}\n" if db_id else ""
    supported_filter_operators = ", ".join(sorted(SUPPORTED_FILTER_OPERATORS))
    return (
        "You are a semantic router for a governed analytics engine. Given a user question and a catalog of "
        "governed measures, dimensions, and time dimensions, select the MOST APPROPRIATE measure, dimensions, "
        "time dimensions, and filters.\n\n"
        f"Question: {question}\n"
        f"{db_line}\n"
        "Available measures:\n"
        f"{_measure_list(catalog_snapshot)}\n\n"
        "Available dimensions:\n"
        f"{_dimension_list(catalog_snapshot)}\n\n"
        "Available identifiers for filters:\n"
        f"{_identifier_list(catalog_snapshot)}\n\n"
        "Available time dimensions:\n"
        f"{_time_dimension_list(catalog_snapshot)}\n\n"
        "Available segments:\n"
        f"{_segment_list(catalog_snapshot)}\n\n"
        "Supported filter operators:\n"
        f"{supported_filter_operators}\n\n"
        "Respond with ONLY valid JSON in this exact format. No explanation, no markdown:\n"
        "{\n"
        '  "measure": "entity_name.measure_name",\n'
        '  "dimensions": ["entity_name.dimension_name", ...],\n'
        '  "time_dimension": "entity_name.time_dimension_name" or null,\n'
        '  "granularity": "day" or "week" or "month" or "quarter" or "year" or null,\n'
        '  "filters": [\n'
        '    {"member": "entity_name.dimension_name", "operator": "equals", "values": ["value"]}\n'
        "  ],\n"
        '  "confidence": 0.0-1.0\n'
        "}\n\n"
        "Rules:\n"
        "1. Pick exactly ONE measure from the available list\n"
        "2. Only pick dimensions that EXIST in the available list\n"
        "3. Only use filter values that are STRING literals explicitly mentioned in the question\n"
        "   or normalized date strings derived from dates explicitly mentioned in the question\n"
        "4. Set confidence to 0.0 if no measure is appropriate\n"
        "5. For time-based questions, set the time_dimension and granularity\n"
        "6. For full months such as January 2012, prefer a date filter like operator with values such as "
        '["2012-01%"] or starts_with with ["2012-01"]\n'
        "7. For date ranges such as between August and November 2013, prefer between with inclusive "
        'bounds such as ["2013-08-01", "2013-11-30"] when day-level dates are available\n'
    )


def parse_router_response(llm_response_text: str) -> dict[str, Any] | None:
    try:
        data = StrictJSONParser().extract_json(llm_response_text)
        parsed = _RouterResponse.model_validate(data)
    except (ValueError, ValidationError, TypeError):
        return None
    return parsed.model_dump()


class SemanticRouter:
    def __init__(self, snapshot: SemanticModelSnapshot, llm_generate: Callable[[str], str]):
        self.snapshot = snapshot
        self.llm_generate = llm_generate

    def route(self, question: str, db_id: str | None = None) -> RouterResult | None:
        prompt = build_router_prompt(self.snapshot, question, db_id=db_id)
        response = self.llm_generate(prompt)
        parsed = parse_router_response(response)
        if not parsed or parsed.get("confidence", 0.0) < 0.3:
            return None
        if not self._validate_choice(parsed, question):
            return None
        filters = [
            FilterIR(member=f["member"], operator=f["operator"], values=f["values"])
            for f in parsed.get("filters", [])
        ]
        return RouterResult(
            measure=parsed["measure"],
            dimensions=parsed.get("dimensions", []),
            time_dimension=parsed.get("time_dimension"),
            granularity=parsed.get("granularity"),
            filters=filters,
            confidence=parsed.get("confidence", 0.0),
        )

    def _validate_choice(self, parsed: dict[str, Any], question: str) -> bool:
        if parsed["measure"] not in _members_by_type(self.snapshot, "measure"):
            return False
        dimensions = _members_by_type(self.snapshot, "dimension")
        identifiers = _members_by_type(self.snapshot, "identifier")
        time_dimensions = _members_by_type(self.snapshot, "time_dimension")
        if any(dimension not in dimensions for dimension in parsed.get("dimensions", [])):
            return False
        time_dimension = parsed.get("time_dimension")
        granularity = parsed.get("granularity")
        if time_dimension is not None and time_dimension not in time_dimensions:
            return False
        if granularity is not None and granularity not in SUPPORTED_GRANULARITIES:
            return False
        if bool(time_dimension) != bool(granularity):
            return False
        filter_members = dimensions | identifiers | time_dimensions
        for filter_ in parsed.get("filters", []):
            if not isinstance(filter_, dict):
                return False
            if filter_.get("member") not in filter_members:
                return False
            if str(filter_.get("operator", "")).lower() not in SUPPORTED_FILTER_OPERATORS:
                return False
            values = filter_.get("values")
            if not isinstance(values, list) or not values:
                return False
            if any(
                not isinstance(value, str)
                or not (_question_mentions_value(question, value) or _value_is_date_literal(value))
                for value in values
            ):
                return False
        return True


def compile_from_router(
    snapshot: SemanticModelSnapshot,
    router_result: RouterResult,
    question: str,
) -> CompiledQuery | None:
    compile_snapshot = (
        _strip_measure_filters(snapshot, router_result.measure)
        if router_result.filters
        else snapshot
    )
    selected_view = _select_view(snapshot, router_result)
    term_resolutions = [
        TermResolution(
            resolved_term=router_result.measure.split(".")[-1].replace("_", " "),
            matched_member=router_result.measure,
            member_type="measure",
            confidence=router_result.confidence,
            fuzzy_match=True,
        )
    ]
    term_resolutions.extend(
        TermResolution(
            resolved_term=dimension.split(".")[-1].replace("_", " "),
            matched_member=dimension,
            member_type="dimension",
            confidence=router_result.confidence,
            fuzzy_match=True,
        )
        for dimension in router_result.dimensions
    )
    if router_result.time_dimension:
        term_resolutions.append(
            TermResolution(
                resolved_term=router_result.time_dimension.split(".")[-1].replace("_", " "),
                matched_member=router_result.time_dimension,
                member_type="time_dimension",
                confidence=router_result.confidence,
                fuzzy_match=True,
            )
        )

    resolution = ResolutionResult(
        question=_question_with_router_hints(question, router_result),
        term_resolutions=term_resolutions,
        coverage=CoverageResult(
            is_covered=True,
            covered_members=[item.matched_member for item in term_resolutions],
            missing_members=[],
            coverage_score=router_result.confidence,
            deterministic_compile_supported=True,
            guarded_llm_supported=True,
        ),
        route_decision=RouteDecision(
            route="SEMANTIC_SQL",
            reason="LLM semantic router selected governed members.",
            selected_view=selected_view,
            confidence=router_result.confidence,
        ),
        selected_view=(
            ViewSelection(view_name=selected_view, explanation="Selected for routed semantic members.", score=1.0)
            if selected_view
            else None
        ),
        join_path_coverage=JoinPathCoverage(
            path_exists=True,
            path_entities=_involved_entities(router_result),
            fanout_risk="unknown",
        ),
    )
    try:
        compiled = compile_sql(resolution, compile_snapshot)
    except Exception:
        return None
    try:
        if router_result.filters:
            compiled = _with_router_filters(compile_snapshot, compiled, list(router_result.filters))
        return compiled
    except Exception:
        return None


def _measure_list(snapshot: SemanticModelSnapshot) -> str:
    rows = []
    for entity in snapshot.entities.values():
        for measure in entity.measures:
            rows.append(
                f"- {entity.name}.{measure.name}: entity={entity.name}, aggregation={measure.aggregation}, "
                f"column={measure.expr}, title={measure.title or ''}, description={measure.description or ''}"
            )
    return "\n".join(rows) or "- none"


def _dimension_list(snapshot: SemanticModelSnapshot) -> str:
    rows = []
    for entity in snapshot.entities.values():
        for dimension in entity.dimensions:
            examples = _example_values(dimension)
            suffix = f", examples={examples}" if examples else ""
            rows.append(
                f"- {entity.name}.{dimension.name}: entity={entity.name}, column={dimension.expr}, "
                f"title={dimension.title or ''}, synonyms={dimension.synonyms}{suffix}"
            )
    return "\n".join(rows) or "- none"


def _identifier_list(snapshot: SemanticModelSnapshot) -> str:
    rows = []
    for entity in snapshot.entities.values():
        for identifier in entity.identifiers:
            rows.append(
                f"- {entity.name}.{identifier.name}: entity={entity.name}, column={identifier.expr}, "
                f"type={identifier.type}, primary_key={identifier.primary_key}"
            )
    return "\n".join(rows) or "- none"


def _time_dimension_list(snapshot: SemanticModelSnapshot) -> str:
    rows = []
    for entity in snapshot.entities.values():
        for dimension in entity.time_dimensions:
            rows.append(
                f"- {entity.name}.{dimension.name}: entity={entity.name}, column={dimension.expr}, "
                f"granularities={dimension.granularities}, default={dimension.default}"
            )
    return "\n".join(rows) or "- none"


def _segment_list(snapshot: SemanticModelSnapshot) -> str:
    rows = []
    for entity in snapshot.entities.values():
        for segment in entity.segments:
            filters = [filter_.model_dump(mode="json") for filter_ in segment.filters]
            rows.append(f"- {entity.name}.{segment.name}: entity={entity.name}, filters={json.dumps(filters)}")
    return "\n".join(rows) or "- none"


def _example_values(dimension: Any) -> list[Any]:
    metadata = getattr(dimension, "metadata", {}) or {}
    for key in ("example_values", "examples", "sample_values"):
        values = metadata.get(key)
        if isinstance(values, list):
            return values[:5]
    return []


def _members_by_type(snapshot: SemanticModelSnapshot, member_type: str) -> set[str]:
    return {member for member, type_ in snapshot.catalog_index.items() if type_ == member_type}


def _question_mentions_value(question: str, value: str) -> bool:
    question_text = question.lower()
    value_text = value.lower()
    variants = {value_text, value_text.replace("_", " "), value_text.replace("-", " ")}
    return any(variant and re.search(rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])", question_text) for variant in variants)


def _value_is_date_literal(value: str) -> bool:
    return bool(re.fullmatch(r"(?:\d{4}(?:-\d{2}(?:-\d{2})?)?|\d{6}|\d{8})%?", value))


def _strip_measure_filters(snapshot: SemanticModelSnapshot, measure_name: str) -> SemanticModelSnapshot:
    if "." not in measure_name:
        return snapshot
    entity_name, local_name = measure_name.split(".", 1)
    if entity_name not in snapshot.entities:
        return snapshot

    copied = snapshot.model_copy(deep=True)
    entity = copied.entities[entity_name]
    stripped_measures = [
        measure.model_copy(update={"filters": []}) if measure.name == local_name else measure
        for measure in entity.measures
    ]
    copied.entities[entity_name] = entity.model_copy(update={"measures": stripped_measures})
    return copied


def _select_view(snapshot: SemanticModelSnapshot, router_result: RouterResult) -> str | None:
    required = {router_result.measure, *router_result.dimensions}
    if router_result.time_dimension:
        required.add(router_result.time_dimension)
    required.update(filter_.member for filter_ in router_result.filters)
    for view_name, view in snapshot.views.items():
        exposed = _view_members(snapshot, view)
        if required.issubset(exposed):
            return view_name
    return next(iter(snapshot.views), None)


def _view_members(snapshot: SemanticModelSnapshot, view: Any) -> set[str]:
    members: set[str] = set()
    for view_entity in view.entities:
        entity_name = view_entity.join_path.split(".")[-1]
        entity = snapshot.entities.get(entity_name)
        if entity is None:
            continue
        for group_name in ("measures", "dimensions", "time_dimensions", "segments"):
            for member_name in getattr(view_entity.includes, group_name):
                qualified = member_name if "." in member_name else f"{entity.name}.{member_name}"
                members.add(qualified)
    return members


def _involved_entities(router_result: RouterResult) -> list[str]:
    members = [
        router_result.measure,
        *router_result.dimensions,
        *[filter_.member for filter_ in router_result.filters],
    ]
    if router_result.time_dimension:
        members.append(router_result.time_dimension)
    entities: list[str] = []
    for member in members:
        entity = member.split(".", 1)[0]
        if entity and entity not in entities:
            entities.append(entity)
    return entities


def _question_with_router_hints(question: str, router_result: RouterResult) -> str:
    if not router_result.time_dimension or not router_result.granularity:
        return question
    token = router_result.granularity
    if re.search(rf"\bby\s+{re.escape(token)}\b", question, flags=re.IGNORECASE):
        return question
    return f"{question} by {token}"


def _with_router_filters(
    snapshot: SemanticModelSnapshot,
    compiled: CompiledQuery,
    filters: list[FilterIR],
) -> CompiledQuery:
    from semantic_engine.compiler.sql_compiler import JoinPlanner, SQLCompiler

    query_ir = compiled.query_ir.model_copy(update={"filters": [*compiled.query_ir.filters, *filters]})
    join_plan = JoinPlanner(snapshot).plan(query_ir)
    return SQLCompiler(snapshot).compile(query_ir, join_plan)
