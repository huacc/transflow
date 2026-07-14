from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class ContentsColumnBand:
    column_index: int
    bbox: Rect
    page_anchor_x: float
    anchor_side: str


@dataclass(frozen=True)
class ContentsContainer:
    container_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    role: str
    hierarchy_level: int
    column_index: int
    entry_id: str | None
    font_name: str
    font_size: float
    color_srgb: int


@dataclass(frozen=True)
class ContentsEntry:
    entry_id: str
    order: int
    column_index: int
    hierarchy_level: int
    container_ids: tuple[str, ...]
    page_anchor_object_ids: tuple[str, ...]
    page_number_text: str
    page_anchor_bbox: Rect
    row_bbox: Rect


@dataclass(frozen=True)
class ContentsTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    column_bands: tuple[ContentsColumnBand, ...]
    containers: tuple[ContentsContainer, ...]
    entries: tuple[ContentsEntry, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str


@dataclass(frozen=True)
class ContentsPlacement:
    container_id: str
    translated_text: str
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    fit: bool


@dataclass(frozen=True)
class ContentsLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[ContentsPlacement, ...]


@dataclass(frozen=True)
class ContentsFinding:
    code: str
    severity: str
    owner: str
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class ContentsDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[ContentsFinding, ...]
