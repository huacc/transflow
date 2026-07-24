"""Private immutable contracts for native chart text ownership and layout."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class ChartVisualRegion:
    region_id: str
    kind: str
    bbox: Rect
    object_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChartTextContainer:
    container_id: str
    role: str
    association_id: str
    source_object_ids: tuple[str, ...]
    semantic_object_id: str
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    anchor_object_ids: tuple[str, ...]
    anchor_relation: str
    reading_order: int
    required_literals: tuple[str, ...]
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class ChartTemplate:
    page_identity: str
    width: float
    height: float
    visual_regions: tuple[ChartVisualRegion, ...]
    containers: tuple[ChartTextContainer, ...]
    protected_object_ids: tuple[str, ...]
    locked_objects_hash: str
    structure_hash: str


@dataclass(frozen=True, slots=True)
class ChartPlacement:
    container_id: str
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


@dataclass(frozen=True, slots=True)
class ChartLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[ChartPlacement, ...]


@dataclass(frozen=True, slots=True)
class ChartFinding:
    code: str
    severity: str
    owner: str
    association_id: str | None
    container_id: str | None
    message: str
    evidence: dict[str, object]
