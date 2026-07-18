from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.chart.tools.models import ChartLayoutPlan, ChartTemplate, Rect
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutPlan


@dataclass(frozen=True)
class ObjectOwnership:
    object_id: str
    owner: str
    container_id: str | None


@dataclass(frozen=True)
class ContainerOwnership:
    container_id: str
    owner: str
    region_id: str


@dataclass(frozen=True)
class FlowRegionTemplate:
    region_id: str
    mode: str
    allowed_bbox: Rect
    template: SingleColumnTemplate


@dataclass(frozen=True)
class FlowTextChartTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    flow_regions: tuple[FlowRegionTemplate, ...]
    chart_template: ChartTemplate
    render_template: ChartTemplate
    chart_guard_regions: tuple[Rect, ...]
    ownerships: tuple[ObjectOwnership, ...]
    container_ownerships: tuple[ContainerOwnership, ...]
    structure_sha256: str


@dataclass(frozen=True)
class FlowRegionPlan:
    region_id: str
    mode: str
    allowed_bbox: Rect
    plan: P4LayoutPlan


@dataclass(frozen=True)
class FlowTextChartLayoutPlan:
    page_id: str
    toolbox_key: str
    source_language: str
    target_language: str
    flow_region_plans: tuple[FlowRegionPlan, ...]
    chart_plan: ChartLayoutPlan
    render_plan: ChartLayoutPlan
    structure_sha256: str
