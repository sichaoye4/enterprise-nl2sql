from __future__ import annotations

from abc import ABC, abstractmethod

from src.semantic_registry.metadata.models import ExampleQuery, JoinPath, TableMetadata, ColumnMetadata


class MetadataProvider(ABC):
    @abstractmethod
    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        raise NotImplementedError

    @abstractmethod
    def get_table(self, table_name: str) -> TableMetadata | None:
        raise NotImplementedError

    @abstractmethod
    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        raise NotImplementedError

    @abstractmethod
    def get_join_paths(self, tables: list[str]) -> list[JoinPath]:
        raise NotImplementedError

    @abstractmethod
    def get_example_queries(self, query: str) -> list[ExampleQuery]:
        raise NotImplementedError

    def list_tables(self, domain: str | None = None) -> list[TableMetadata]:
        return self.search_tables("", domain=domain)


__all__ = ["MetadataProvider"]
