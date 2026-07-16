from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class DiagramNode:
    node_id: str
    boundary_bbox: Rect
    safe_text_bbox: Rect
    source_drawing_ids: tuple[str, ...]
    container_ids: tuple[str, ...]


@dataclass(frozen=True)
class DiagramConnector:
    connector_id: str
    start: Point
    end: Point
    source_drawing_id: str
    start_node_id: str | None
    end_node_id: str | None
    direction: str


@dataclass(frozen=True)
class DiagramContainer:
    container_id: str
    owner_kind: str
    owner_id: str
    node_id: str | None
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    required_literals: tuple[str, ...]
    role: str
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str


@dataclass(frozen=True)
class DiagramTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    mode: str
    nodes: tuple[DiagramNode, ...]
    connectors: tuple[DiagramConnector, ...]
    containers: tuple[DiagramContainer, ...]
    protected_object_ids: tuple[str, ...]
    diagram_geometry_sha256: str
    topology_sha256: str
    structure_sha256: str
    layout_strategy: str = "OWNER_FIT"


@dataclass(frozen=True)
class DiagramPlacement:
    container_id: str
    owner_kind: str
    owner_id: str
    node_id: str | None
    translated_text: str
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    fit_profile: str
    fit: bool
    glyph_bbox: Rect | None = None


@dataclass(frozen=True)
class DiagramLayoutPlan:
    page_id: str
    toolbox_key: str
    topology_sha256: str
    placements: tuple[DiagramPlacement, ...]


@dataclass(frozen=True)
class DiagramFinding:
    code: str
    severity: str
    owner: str
    node_id: str | None
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class DiagramDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[DiagramFinding, ...]


@dataclass(frozen=True)
class P14RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    mode: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    node_count: int
    connector_count: int
    container_count: int
    protected_object_count: int
