"""Private immutable contracts for the lifted multi-column text core."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class MultiTextContainer:
    """One source-bound semantic container owned by the multi-column leaf."""

    container_id: str
    source_object_ids: tuple[str, ...]
    translation_object_ids: tuple[str, ...]
    inline_keep_source_object_ids: tuple[str, ...]
    source_rects: tuple[Rect, ...]
    source_text: str
    reading_order: int
    role: str
    source_bbox: Rect
    font_size: float
    color_srgb: int
    preferred_line_height: float
    preserved_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class ColumnBand:
    """A source-derived column boundary and its safe vertical flow range."""

    column_id: str
    ordinal: int
    left: float
    right: float
    content_top: float
    content_bottom: float


@dataclass(frozen=True, slots=True)
class ColumnAssignment:
    """Bind one container to a column, page span, fixed overlay, or margin."""

    container_id: str
    column_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class MultiColumnTemplate:
    """Preserve the Spike column-band and assignment model in production."""

    page_id: str
    toolbox_key: str
    width: float
    height: float
    columns: tuple[ColumnBand, ...]
    containers: tuple[MultiTextContainer, ...]
    assignments: tuple[ColumnAssignment, ...]
    ambiguous_container_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MultiPlacement:
    """One deterministic translated-text placement."""

    container_id: str
    translated_text: str
    role: str
    source_bbox: Rect
    output_bbox: Rect
    font_size: float
    line_height: float
    color_srgb: int
    fit: bool


@dataclass(frozen=True, slots=True)
class MultiColumnLayoutPlan:
    """The selected page-wide fit profile and ordered placements."""

    page_id: str
    toolbox_key: str
    profile_id: str
    placements: tuple[MultiPlacement, ...]


@dataclass(frozen=True, slots=True)
class MultiFinding:
    """Leaf-private finding projected by the shared production wrapper."""

    code: str
    severity: str
    container_id: str | None
