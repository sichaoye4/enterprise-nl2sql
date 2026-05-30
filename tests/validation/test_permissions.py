from __future__ import annotations

from src.semantic_registry.validation.permissions import AllowAllPermissionChecker


def test_allow_all_permission_checker_always_grants() -> None:
    result = AllowAllPermissionChecker().check_permissions(
        "analyst",
        "SELECT paid_gmv_amt FROM orders",
        ["orders"],
        ["paid_gmv_amt"],
    )

    assert result.granted is True
    assert result.denied_tables == []
    assert result.denied_columns == []
