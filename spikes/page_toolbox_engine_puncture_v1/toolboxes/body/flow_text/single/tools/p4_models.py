from __future__ import annotations

from dataclasses import dataclass

from .models import Rect, ToolboxFinding


@dataclass(frozen=True)
class P4LayoutProfile:
    profile_id: str
    font_scale: float
    line_height: float
    gap_scale: float


@dataclass(frozen=True)
class P4Placement:
    container_id: str
    translated_text: str
    role: str
    source_bbox: Rect
    output_bbox: Rect
    horizontal_policy: str
    source_font_size: float
    font_size: float
    line_height: float
    vertical_policy: str
    source_gap: float
    target_gap: float
    color_srgb: int
    font_weight: str
    fit: bool


@dataclass(frozen=True)
class P4LayoutPlan:
    page_id: str
    toolbox_key: str
    source_language: str
    target_language: str
    profile_id: str
    font_file: str
    font_resource: str
    column_left: float
    column_right: float
    content_top: float
    content_bottom: float
    placements: tuple[P4Placement, ...]


@dataclass(frozen=True)
class P4RepairAttempt:
    attempt_index: int
    profile_id: str
    font_scale: float
    line_height: float
    gap_scale: float
    fit: bool
    findings: tuple[ToolboxFinding, ...]
