from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.metadata.snapshot import create_snapshot, get_active_snapshot, list_snapshots, restore_snapshot
from src.semantic_registry.models import SemanticStatus, SemanticTerm


class SnapshotProvider(MetadataProvider):
    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        return self.list_tables(domain=domain)

    def list_tables(self, domain: str | None = None) -> list[TableMetadata]:
        return [
            TableMetadata(
                table_name="public.orders",
                description="Orders.",
                domain="sales",
                certified=True,
                grain=["order_id"],
                partition_column="order_date",
                owner="analytics",
                columns=[ColumnMetadata(column_name="order_id", is_pii=False)],
            )
        ]

    def get_table(self, table_name: str) -> TableMetadata | None:
        return self.list_tables()[0] if table_name == "public.orders" else None

    def get_columns(self, table_name: str):
        table = self.get_table(table_name)
        return table.columns if table else []

    def get_join_paths(self, tables: list[str]):
        return []

    def get_example_queries(self, query: str):
        return []


async def test_snapshot_creation_listing_and_restore(in_memory_session) -> None:
    in_memory_session.add(
        SemanticTerm(
            term="orders",
            description="Orders.",
            owner="analytics",
            domain="sales",
            status=SemanticStatus.certified,
        )
    )
    await in_memory_session.commit()

    snapshot = await create_snapshot(in_memory_session, SnapshotProvider())
    active = await get_active_snapshot(in_memory_session)
    snapshots = await list_snapshots(in_memory_session)
    restored = await restore_snapshot(in_memory_session, snapshot.id)

    assert active is not None
    assert active.id == snapshot.id
    assert snapshots[0].id == snapshot.id
    assert restored["metadata"]["tables"][0]["table_name"] == "public.orders"
    assert restored["semantic_registry"]["terms"][0]["term"] == "orders"
