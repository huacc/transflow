from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]
Rgb = tuple[int, int, int]


@dataclass(frozen=True)
class VisualTextSlot:
    slot_id: str
    boundary_bbox: Rect
    allowed_bbox: Rect
    safe_padding: Rect
    source_object_ids: tuple[str, ...]
    background_object_ids: tuple[str, ...]
    anchor_object_ids: tuple[str, ...]
    background_rgb: Rgb
    source_contrast_ratio: float
    z_order: str
    alignment: str
    reading_order: int


@dataclass(frozen=True)
class VisualAnchoredContainer:
    container_id: str
    slot_id: str
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
class VisualAnchoredTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    visual_slots: tuple[VisualTextSlot, ...]
    containers: tuple[VisualAnchoredContainer, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str


@dataclass(frozen=True)
class VisualAnchoredPlacement:
    container_id: str
    slot_id: str
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
    render_text: bool = True
    deduplicated_against_container_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualAnchoredLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[VisualAnchoredPlacement, ...]


@dataclass(frozen=True)
class VisualAnchoredFinding:
    code: str
    severity: str
    owner: str
    slot_id: str | None
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class VisualAnchoredDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[VisualAnchoredFinding, ...]
