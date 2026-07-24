"""Build TBM2 factories only for run-private Catalog overlays."""

from __future__ import annotations

from pathlib import Path

from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.toolboxes.catalog import ToolboxFactory
from transflow.toolboxes.composites.toolbox import (
    FlowTextChartToolbox,
    FlowTextDiagramToolbox,
    FreeformToolbox,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy


def build_tbm2_toolbox_factories(
    policy_path: Path,
    font_manifest_path: Path,
    repository_root: Path,
    source_pdf: Path,
) -> dict[str, ToolboxFactory]:
    """Return ready roots; dependency-blocked composites remain unregistered."""

    policy = load_p8_toolbox_policy(policy_path)
    fonts = ControlledFontRegistry(font_manifest_path, repository_root)
    font_path = fonts.resolve(policy.font_id).path
    source = source_pdf.resolve()
    return {
        "body.composite.flow_text_chart": lambda: FlowTextChartToolbox(
            policy,
            font_path,
        ),
        "body.composite.flow_text_diagram": lambda: FlowTextDiagramToolbox(
            policy,
            font_path,
            source,
        ),
        "body.freeform": lambda: FreeformToolbox(
            policy,
            font_path,
            source,
        ),
    }
