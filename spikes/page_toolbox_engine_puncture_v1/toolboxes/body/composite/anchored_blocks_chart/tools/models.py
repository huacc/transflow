from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.anchored_blocks.tools.models import (
    AnchoredBlocksTemplate,
    AnchoredLayoutPlan,
)
from toolboxes.body.chart.tools.models import ChartLayoutPlan, ChartTemplate


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class ObjectOwnership:
    object_id: str
    owner: str
    container_id: str | None


@dataclass(frozen=True)
class CompositeContainer:
    composite_id: str
    owner: str
    base_container_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    required_literals: tuple[str, ...]


@dataclass(frozen=True)
class CompositePageTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    anchored_template: AnchoredBlocksTemplate | None
    chart_template: ChartTemplate | None
    containers: tuple[CompositeContainer, ...]
    ownerships: tuple[ObjectOwnership, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str


@dataclass(frozen=True)
class CompositePlacement:
    composite_id: str
    owner: str
    base_container_id: str
    translated_text: str
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    minimum_font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    profile: str
    fit: bool
    rotation: int = 0


@dataclass(frozen=True)
class CompositeLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[CompositePlacement, ...]
    anchored_plan: AnchoredLayoutPlan
    chart_plan: ChartLayoutPlan


@dataclass(frozen=True)
class CompositeFinding:
    code: str
    severity: str
    owner: str
    region_owner: str | None
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class CompositeDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[CompositeFinding, ...]


@dataclass(frozen=True)
class P15RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    provider: str
    failure_owner: str | None
