from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutPlan, P4RepairAttempt
from toolboxes.body.table.tools.models import TableLayoutPlan, TableTemplate


Rect = tuple[float, float, float, float]


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
    relation: str
    allowed_bbox: Rect
    template: SingleColumnTemplate


@dataclass(frozen=True)
class TableRegionTransform:
    source_bbox: Rect
    target_bbox: Rect
    preceding_flow_region_id: str | None
    source_gap: float | None
    target_gap: float | None

    @property
    def moved(self) -> bool:
        return self.source_bbox != self.target_bbox


@dataclass(frozen=True)
class CompositePageTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    table_template: TableTemplate
    table_regions: tuple[Rect, ...]
    flow_regions: tuple[FlowRegionTemplate, ...]
    ownerships: tuple[ObjectOwnership, ...]
    container_ownerships: tuple[ContainerOwnership, ...]


@dataclass(frozen=True)
class CompositeLayoutPlan:
    page_id: str
    toolbox_key: str
    source_language: str
    target_language: str
    table_region_transforms: tuple[TableRegionTransform, ...]
    flow_plans: tuple[P4LayoutPlan, ...]
    table_plan: TableLayoutPlan


@dataclass(frozen=True)
class CompositeFinding:
    code: str
    severity: str
    owner: str
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
class CompositePlanEvidence:
    flow_attempts: tuple[tuple[P4RepairAttempt, ...], ...]
