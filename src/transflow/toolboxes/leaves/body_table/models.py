"""Private immutable contracts for the lifted table core."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class TableStructure:
    """Keep one Kernel-proven table and its immutable cell boundaries."""

    table_id: str
    bbox: Rect
    cell_bboxes: tuple[Rect, ...]
    direct_evidence: tuple[str, ...]
    structure_sha256: str


@dataclass(frozen=True, slots=True)
class TableCell:
    """Bind one logical translation unit to one table cell or page context."""

    container_id: str
    table_id: str
    source_object_ids: tuple[str, ...]
    translation_object_ids: tuple[str, ...]
    inline_keep_source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    hard_legal_boundary: Rect
    reading_order: int
    role: str
    font_size: float
    color_srgb: int
    alignment: str
    ownership_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class TableTemplate:
    """Freeze table ownership before any translated text is available."""

    page_id: str
    toolbox_key: str
    width: float
    height: float
    structures: tuple[TableStructure, ...]
    cells: tuple[TableCell, ...]
    protected_object_ids: tuple[str, ...]

    @property
    def translatable_cells(self) -> tuple[TableCell, ...]:
        return tuple(item for item in self.cells if item.translation_object_ids)


@dataclass(frozen=True, slots=True)
class TablePlacement:
    """Describe one cell-bound placement without writing a PDF."""

    container_id: str
    translated_text: str
    hard_legal_boundary: Rect
    output_bbox: Rect
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    fit: bool


@dataclass(frozen=True, slots=True)
class TableLayoutPlan:
    """Collect deterministic placements selected by the finite fit ladder."""

    page_id: str
    toolbox_key: str
    placements: tuple[TablePlacement, ...]


@dataclass(frozen=True, slots=True)
class TableFinding:
    """Keep table-private evidence machine-readable until shared projection."""

    code: str
    severity: str
    container_id: str | None
