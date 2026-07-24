"""Provide the TM3 body.chart run-private overlay and explicit migration driver."""

from __future__ import annotations

import copy
from typing import Any

from scripts.toolbox_leaf_migration_drivers import LeafMigrationRunContext
from transflow.domain.common import content_sha256
from transflow.toolboxes.catalog import catalog_entry_fingerprint

ROUTE = "body.chart"
TOOLBOX_VERSION = "1.0.0-tm3-review"


def build_chart_catalog_overlay(catalog: dict[str, Any]) -> dict[str, Any]:
    """Enable only body.chart in an in-memory/run-private Catalog payload."""

    overlay = copy.deepcopy(catalog)
    matched = [item for item in overlay["entries"] if item["route"] == ROUTE]
    if len(matched) != 1:
        raise ValueError("body.chart catalog binding must be unique")
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
                    "stage": "TM3",
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


class ChartMigrationDriver:
    """Execute the TM3 artifact-producing validation chain."""

    def execute(self, context: LeafMigrationRunContext) -> dict[str, Any]:
        from scripts.toolbox_leaf_migration_chart_run import execute_chart_migration

        return execute_chart_migration(context)


def main() -> int:
    print("TM3_CHART_DRIVER route=body.chart registration=explicit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
