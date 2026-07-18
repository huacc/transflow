from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.diagram.tools.models import DiagramLayoutPlan, DiagramTemplate
from toolboxes.body.flow_text.multi.tools.models import MultiColumnLayoutPlan, MultiColumnTemplate
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutPlan


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
    role: str


@dataclass(frozen=True)
class CompositePageTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    flow_mode: str
    flow_template: SingleColumnTemplate | MultiColumnTemplate
    diagram_template: DiagramTemplate
    diagram_region: Rect
    containers: tuple[CompositeContainer, ...]
    ownerships: tuple[ObjectOwnership, ...]
    protected_object_ids: tuple[str, ...]
    topology_sha256: str
    structure_sha256: str


@dataclass(frozen=True)
class CompositeLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    flow_mode: str
    flow_plan: P4LayoutPlan | MultiColumnLayoutPlan
    diagram_plan: DiagramLayoutPlan
    render_plan: DiagramLayoutPlan


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
class P18RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    provider: str
    failure_owner: str | None
    flow_mode: str | None
    flow_container_count: int
    diagram_container_count: int
    shared_container_count: int
    protected_object_count: int
