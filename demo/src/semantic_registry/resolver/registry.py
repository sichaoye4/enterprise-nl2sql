from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, TypeVar

from src.semantic_registry.yaml_schema.schemas import ConceptYaml, DimensionYaml, JoinPathYaml, MetricYaml, TermYaml, parse_yaml_file


T = TypeVar("T", TermYaml, ConceptYaml, MetricYaml, DimensionYaml, JoinPathYaml)


@dataclass(frozen=True)
class SemanticRegistryData:
    terms: list[TermYaml] = field(default_factory=list)
    concepts: list[ConceptYaml] = field(default_factory=list)
    metrics: list[MetricYaml] = field(default_factory=list)
    dimensions: list[DimensionYaml] = field(default_factory=list)
    join_paths: list[JoinPathYaml] = field(default_factory=list)

    def as_retrieval_data(self) -> dict[str, list[object]]:
        return {
            "terms": list(self.terms),
            "concepts": list(self.concepts),
            "metrics": list(self.metrics),
            "dimensions": list(self.dimensions),
            "join_paths": list(self.join_paths),
        }


def _yaml_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return [*sorted(directory.glob("**/*.yaml")), *sorted(directory.glob("**/*.yml"))]


def _load_category(root: Path, category: str, model_type: type[T]) -> list[T]:
    items: list[T] = []
    for path in _yaml_files(root / category):
        parsed = parse_yaml_file(path)
        if isinstance(parsed, model_type):
            items.append(parsed)
    return items


def load_semantic_registry(semantic_dir: str | Path) -> SemanticRegistryData:
    root = Path(semantic_dir)
    return SemanticRegistryData(
        terms=_load_category(root, "terms", TermYaml),
        concepts=_load_category(root, "concepts", ConceptYaml),
        metrics=_load_category(root, "metrics", MetricYaml),
        dimensions=_load_category(root, "dimensions", DimensionYaml),
        join_paths=_load_category(root, "join_paths", JoinPathYaml),
    )
