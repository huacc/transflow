from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.flow_text.single.tools.models import Rect, TextContainer, ToolboxFinding
from toolboxes.body.flow_text.single.tools.p4_models import P4Placement


@dataclass(frozen=True)
class ColumnBand:
    column_id: str
    reading_order: int
    left: float
    right: float
    content_top: float
    content_bottom: float


@dataclass(frozen=True)
class ColumnAssignment:
    container_id: str
    column_id: str
    column_reading_order: int


@dataclass(frozen=True)
class StructuralAnchor:
    anchor_id: str
    anchor_kind: str
    bbox: Rect
    source: str


@dataclass(frozen=True)
class FlowBand:
    """页内从上到下的局部排版区段；每段只绑定一种回填策略。"""

    band_id: str
    reading_order: int
    mode: str
    top: float
    bottom: float
    column_count: int
    container_ids: tuple[str, ...]
    refill_strategy: str


@dataclass(frozen=True)
class MultiColumnTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    columns: tuple[ColumnBand, ...]
    containers: tuple[TextContainer, ...]
    assignments: tuple[ColumnAssignment, ...]
    ambiguous_spanning_container_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnLayoutSelection:
    column_id: str
    profile_id: str
    font_scale: float
    line_height: float
    gap_scale: float
    fit: bool


@dataclass(frozen=True)
class MultiColumnLayoutPlan:
    page_id: str
    toolbox_key: str
    source_language: str
    target_language: str
    font_file: str
    font_resource: str
    columns: tuple[ColumnBand, ...]
    column_selections: tuple[ColumnLayoutSelection, ...]
    placements: tuple[P4Placement, ...]
    structural_anchors: tuple[StructuralAnchor, ...] = ()
    flow_bands: tuple[FlowBand, ...] = ()


@dataclass(frozen=True)
class P5RepairAttempt:
    column_id: str
    profile_id: str
    font_scale: float
    line_height: float
    gap_scale: float
    fit: bool
    findings: tuple[ToolboxFinding, ...]
