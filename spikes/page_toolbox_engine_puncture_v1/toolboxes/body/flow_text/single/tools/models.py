from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class TextContainer:
    container_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    reading_order: int
    role: str
    source_bbox: Rect
    anchor: tuple[float, float]
    font_size: float
    color_srgb: int
    font_weight: str = "regular"
    preserved_prefix: str | None = None


@dataclass(frozen=True)
class SingleColumnTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    containers: tuple[TextContainer, ...]


@dataclass(frozen=True)
class LayoutPlacement:
    container_id: str
    translated_text: str
    output_bbox: Rect
    anchor: tuple[float, float]
    font_size: float
    line_height: float
    color_srgb: int
    fit: bool


@dataclass(frozen=True)
class SingleColumnLayoutPlan:
    page_id: str
    toolbox_key: str
    font_file: str
    font_resource: str
    placements: tuple[LayoutPlacement, ...]


@dataclass(frozen=True)
class ToolboxFinding:
    code: str
    severity: str
    owner: str
    container_id: str | None
    message: str


@dataclass(frozen=True)
class ToolboxDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[ToolboxFinding, ...]
