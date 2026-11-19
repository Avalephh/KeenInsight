from __future__ import annotations

from .scenarios import DEMO_DDL


def provision(connection) -> None:
    """Create the namespaced demo relations and gather fresh statistics."""
    with connection.transaction():
        for statement in DEMO_DDL:
            connection.execute(statement)

