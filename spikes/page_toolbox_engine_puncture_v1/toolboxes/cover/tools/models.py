from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class CoverContainer:
    container_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    translatable: bool
    role: str
    hierarchy_level: int
    anchor: str
    font_name: str
    font_size: float
    color_srgb: int


@dataclass(frozen=True)
class CoverTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    containers: tuple[CoverContainer, ...]
    protected_object_ids: tuple[str, ...]
    visual_only: bool
    visual_only_reason: str | None
    structure_sha256: str
    occluded_object_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverPlacement:
    container_id: str
    translated_text: str
    render_text: bool
    deduplicated_against_container_ids: tuple[str, ...]
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    fit: bool


@dataclass(frozen=True)
class CoverLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[CoverPlacement, ...]


@dataclass(frozen=True)
class CoverFinding:
    code: str
    severity: str
    owner: str
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class CoverDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[CoverFinding, ...]
