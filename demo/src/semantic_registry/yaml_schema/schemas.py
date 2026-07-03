from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, field_validator, model_validator

from src.semantic_registry.models import (
    AmbiguityLevel,
    FanoutRisk,
    JoinRelationship,
    MetricType,
    SemanticStatus,
    SemanticType,
)


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str
    loc: tuple[str, ...] = ()
    type: str = "value_error"

    def __str__(self) -> str:
        location = ".".join(self.loc)
        suffix = f" ({location})" if location else ""
        return f"{self.path}: {self.message}{suffix}"


class StrictYamlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MeasureRef(StrictYamlModel):
    table: str
    column: str


class MetricRef(StrictYamlModel):
    metric: str


class TermYaml(StrictYamlModel):
    term: str
    description: str
    synonyms: list[str] = Field(default_factory=list)
    candidate_concepts: list[str] = Field(default_factory=list)
    default_concept_by_domain: dict[str, str] = Field(default_factory=dict)
    ambiguity_level: AmbiguityLevel = AmbiguityLevel.low
    clarification_required_when: list[str] = Field(default_factory=list)
    owner: str
    domain: str
    status: SemanticStatus = SemanticStatus.draft


class ConceptYaml(StrictYamlModel):
    concept: str
    display_name: str
    domain: str
    definition: str
    type: str = "metric_concept"
    owner: str
    related_but_different: dict[str, str] = Field(default_factory=dict)
    canonical_metric: str | None = None
    status: SemanticStatus = SemanticStatus.draft


class MetricYaml(StrictYamlModel):
    metric: str
    concept: str
    description: str
    type: MetricType
    measure: MeasureRef | None = None
    aggregation: str | None = None
    unit: str | None = None
    default_time_dimension: str | None = None
    physical_time_column: str | None = None
    allowed_dimensions: list[str] = Field(default_factory=list)
    numerator: MetricRef | None = None
    denominator: MetricRef | None = None
    expression: str | None = None
    owner: str
    status: SemanticStatus = SemanticStatus.draft

    @model_validator(mode="after")
    def validate_metric_shape(self) -> MetricYaml:
        if self.type == MetricType.ratio:
            missing = [
                name
                for name in ("numerator", "denominator", "expression")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                raise ValueError(f"ratio metrics require {', '.join(missing)}")
        elif self.type != MetricType.advanced and self.measure is None:
            raise ValueError("non-ratio metrics require measure")
        return self


class PhysicalMappingRef(StrictYamlModel):
    table: str
    column: str


class DimensionYaml(StrictYamlModel):
    dimension: str
    description: str
    entity: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    physical_mappings: list[PhysicalMappingRef] = Field(default_factory=list)
    status: SemanticStatus = SemanticStatus.draft


class EntityYaml(StrictYamlModel):
    entity: str
    description: str
    primary_keys: list[str] = Field(default_factory=list)
    related_entities: list[str] = Field(default_factory=list)
    ambiguity_notes: str | None = None
    status: SemanticStatus = SemanticStatus.draft

    @field_validator("ambiguity_notes", mode="before")
    @classmethod
    def join_note_lists(cls, value: Any) -> str | None:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return str(value)


class JoinPathInnerYaml(StrictYamlModel):
    name: str | None = None
    from_: str = Field(alias="from")
    to: str
    relationship: JoinRelationship
    join_condition: str
    safe_for_metrics: list[str] = Field(default_factory=list)
    fanout_risk: FanoutRisk = FanoutRisk.low
    notes: str | None = None
    status: SemanticStatus = SemanticStatus.draft


class JoinPathYaml(StrictYamlModel):
    join_path_name: str
    from_table: str
    to_table: str
    relationship: JoinRelationship
    join_condition: str
    safe_for_metrics: list[str] = Field(default_factory=list)
    fanout_risk: FanoutRisk = FanoutRisk.low
    notes: str | None = None
    status: SemanticStatus = SemanticStatus.draft

    @model_validator(mode="before")
    @classmethod
    def accept_architecture_shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and "join_path" in data:
            inner = JoinPathInnerYaml.model_validate(data["join_path"])
            return {
                "join_path_name": inner.name or "",
                "from_table": inner.from_,
                "to_table": inner.to,
                "relationship": inner.relationship,
                "join_condition": inner.join_condition,
                "safe_for_metrics": inner.safe_for_metrics,
                "fanout_risk": inner.fanout_risk,
                "notes": inner.notes,
                "status": inner.status,
            }
        if isinstance(data, dict):
            mapped = dict(data)
            if "from" in mapped:
                mapped["from_table"] = mapped.pop("from")
            if "to" in mapped:
                mapped["to_table"] = mapped.pop("to")
            return mapped
        return data

    @model_validator(mode="after")
    def require_name(self) -> JoinPathYaml:
        if not self.join_path_name:
            raise ValueError("join_path_name is required")
        return self


class PhysicalMappingYaml(StrictYamlModel):
    semantic_type: SemanticType
    semantic_name: str
    physical_table: str
    physical_column: str
    transformation: str | None = None
    granularity: str | None = None
    status: SemanticStatus = SemanticStatus.draft


YamlModel = TermYaml | ConceptYaml | MetricYaml | DimensionYaml | EntityYaml | JoinPathYaml | PhysicalMappingYaml

MODEL_BY_DIR: dict[str, type[StrictYamlModel]] = {
    "terms": TermYaml,
    "concepts": ConceptYaml,
    "metrics": MetricYaml,
    "dimensions": DimensionYaml,
    "entities": EntityYaml,
    "join_paths": JoinPathYaml,
    "physical_mappings": PhysicalMappingYaml,
}

KEY_BY_DIR: dict[str, str] = {
    "terms": "term",
    "concepts": "concept",
    "metrics": "metric",
    "dimensions": "dimension",
    "entities": "entity",
    "join_paths": "join_path_name",
    "physical_mappings": "semantic_name",
}


def _yaml_dir(path: Path) -> str:
    for part in reversed(path.parts):
        if part in MODEL_BY_DIR:
            return part
    raise ValueError(f"cannot infer semantic object type from path: {path}")


def _semantic_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in [resolved.parent, *resolved.parents]:
        if parent.name == "semantic":
            return parent
    return resolved.parents[1] / "semantic" if len(resolved.parents) > 1 else Path("semantic")


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _model_errors(path: Path, exc: PydanticValidationError | ValueError) -> list[ValidationError]:
    if isinstance(exc, PydanticValidationError):
        return [
            ValidationError(
                path=str(path),
                message=str(error["msg"]),
                loc=tuple(str(part) for part in error["loc"]),
                type=str(error["type"]),
            )
            for error in exc.errors()
        ]
    return [ValidationError(path=str(path), message=str(exc))]


def parse_yaml_file(path: str | Path) -> YamlModel:
    file_path = Path(path)
    model = MODEL_BY_DIR[_yaml_dir(file_path)]
    data = _load_yaml(file_path)
    return model.model_validate(data)  # type: ignore[return-value]


def _file_name_error(path: Path, model: YamlModel, directory: str) -> ValidationError | None:
    if directory == "physical_mappings":
        return None
    key = KEY_BY_DIR[directory]
    value = getattr(model, key)
    if value != path.stem:
        return ValidationError(
            path=str(path),
            message=f"{key} '{value}' must match file name '{path.stem}'",
            loc=(key,),
            type="reference_error",
        )
    return None


def _iter_yaml_files(directory: Path) -> Iterable[Path]:
    if directory.is_file():
        yield directory
        return
    yield from sorted(directory.glob("**/*.yaml"))
    yield from sorted(directory.glob("**/*.yml"))


def _collect_context(directory: Path) -> tuple[dict[str, set[str]], dict[str, YamlModel], dict[str, list[ValidationError]]]:
    parsed: dict[str, YamlModel] = {}
    errors: dict[str, list[ValidationError]] = {}
    refs = {key: set() for key in MODEL_BY_DIR}
    for path in _iter_yaml_files(directory):
        try:
            semantic_dir = _yaml_dir(path)
            model = parse_yaml_file(path)
            parsed[str(path)] = model
            refs[semantic_dir].add(getattr(model, KEY_BY_DIR[semantic_dir]))
            name_error = _file_name_error(path, model, semantic_dir)
            if name_error:
                errors.setdefault(str(path), []).append(name_error)
        except (PydanticValidationError, ValueError, yaml.YAMLError) as exc:
            errors.setdefault(str(path), []).extend(_model_errors(path, exc))  # type: ignore[arg-type]
    return refs, parsed, errors


def _reference_error(path: Path, field: str, value: str, target: Literal["concepts", "metrics", "dimensions", "entities"]) -> ValidationError:
    return ValidationError(
        path=str(path),
        message=f"{field} references unknown {target[:-1]} '{value}'",
        loc=tuple(field.split(".")),
        type="reference_error",
    )


def _cross_reference_errors(path: Path, model: YamlModel, refs: dict[str, set[str]]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if isinstance(model, TermYaml):
        for concept in model.candidate_concepts:
            if concept not in refs["concepts"]:
                errors.append(_reference_error(path, "candidate_concepts", concept, "concepts"))
        for domain, concept in model.default_concept_by_domain.items():
            if concept not in refs["concepts"]:
                errors.append(_reference_error(path, f"default_concept_by_domain.{domain}", concept, "concepts"))
    elif isinstance(model, ConceptYaml):
        if model.canonical_metric and model.canonical_metric not in refs["metrics"]:
            errors.append(_reference_error(path, "canonical_metric", model.canonical_metric, "metrics"))
    elif isinstance(model, MetricYaml):
        if model.concept not in refs["concepts"]:
            errors.append(_reference_error(path, "concept", model.concept, "concepts"))
        for ref_name, ref in (("numerator.metric", model.numerator), ("denominator.metric", model.denominator)):
            if ref and ref.metric not in refs["metrics"]:
                errors.append(_reference_error(path, ref_name, ref.metric, "metrics"))
        for dimension in model.allowed_dimensions:
            if dimension not in refs["dimensions"]:
                errors.append(_reference_error(path, "allowed_dimensions", dimension, "dimensions"))
    elif isinstance(model, DimensionYaml):
        if model.entity and model.entity not in refs["entities"]:
            errors.append(_reference_error(path, "entity", model.entity, "entities"))
    elif isinstance(model, JoinPathYaml):
        for metric in model.safe_for_metrics:
            if metric not in refs["metrics"]:
                errors.append(_reference_error(path, "safe_for_metrics", metric, "metrics"))
    elif isinstance(model, PhysicalMappingYaml):
        target = f"{model.semantic_type}s"
        if target in refs and model.semantic_name not in refs[target]:
            errors.append(
                ValidationError(
                    path=str(path),
                    message=f"semantic_name references unknown {model.semantic_type} '{model.semantic_name}'",
                    loc=("semantic_name",),
                    type="reference_error",
                )
            )
    return errors


def validate_yaml_file(path: str | Path) -> list[ValidationError]:
    file_path = Path(path)
    root = _semantic_root(file_path)
    refs, parsed, all_errors = _collect_context(root)
    errors = list(all_errors.get(str(file_path), []))
    model = parsed.get(str(file_path))
    if model is not None:
        errors.extend(_cross_reference_errors(file_path, model, refs))
    elif str(file_path) not in all_errors:
        try:
            model = parse_yaml_file(file_path)
            refs[_yaml_dir(file_path)].add(getattr(model, KEY_BY_DIR[_yaml_dir(file_path)]))
            name_error = _file_name_error(file_path, model, _yaml_dir(file_path))
            if name_error:
                errors.append(name_error)
        except (PydanticValidationError, ValueError, yaml.YAMLError) as exc:
            errors.extend(_model_errors(file_path, exc))  # type: ignore[arg-type]
    return errors


def validate_all_yaml_files(directory: str | Path = "semantic/") -> dict[str, list[ValidationError]]:
    root = Path(directory)
    refs, parsed, errors = _collect_context(root)
    for file_name, model in parsed.items():
        path = Path(file_name)
        errors.setdefault(file_name, []).extend(_cross_reference_errors(path, model, refs))
        if not errors[file_name]:
            errors.pop(file_name)
    return errors
