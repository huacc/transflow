"""Provide the TM4 body.diagram run-private overlay and explicit migration driver."""

from __future__ import annotations

import copy
from typing import Any

from scripts.toolbox_leaf_migration_drivers import LeafMigrationRunContext
from transflow.domain.common import content_sha256
from transflow.toolboxes.catalog import catalog_entry_fingerprint

ROUTE = "body.diagram"
TOOLBOX_VERSION = "1.0.0-tm4-review"


def build_diagram_catalog_overlay(catalog: dict[str, Any]) -> dict[str, Any]:
    """Enable only body.diagram in an in-memory/run-private Catalog payload."""

    overlay = copy.deepcopy(catalog)
    matched = [item for item in overlay["entries"] if item["route"] == ROUTE]
    if len(matched) != 1:
        raise ValueError("body.diagram catalog binding must be unique")
    entry = matched[0]
    entry.update(
        {
            "toolbox_version": TOOLBOX_VERSION,
            "fingerprint": catalog_entry_fingerprint(
                ROUTE,
                ROUTE,
                TOOLBOX_VERSION,
                str(entry["contract_version"]),
            ),
            "evidence_state": "PASS_ENABLE",
            "evidence_attestation_hash": content_sha256(
                {
                    "stage": "TM4",
                    "route": ROUTE,
                    "scope": "RUN_PRIVATE_TECHNICAL_OVERLAY",
                    "default_catalog_mutated": False,
                }
            ),
            "enabled": True,
            "disabled_reason": None,
        }
    )
    return overlay


class DiagramMigrationDriver:
    """Execute the TM4 artifact-producing validation chain."""

    def execute(self, context: LeafMigrationRunContext) -> dict[str, Any]:
        from scripts.toolbox_leaf_migration_diagram_run import (
            execute_diagram_migration,
        )

        return execute_diagram_migration(context)


def main() -> int:
    print("TM4_DIAGRAM_DRIVER route=body.diagram registration=explicit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
