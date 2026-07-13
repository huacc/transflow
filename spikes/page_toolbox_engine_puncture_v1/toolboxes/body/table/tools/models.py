from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class TableStructure:
    table_id: str
    bbox: Rect
    column_boundaries: tuple[float, ...]
    row_boundaries: tuple[float, ...]
    direct_evidence: tuple[str, ...]
    structure_sha256: str

    @property
    def column_count(self) -> int:
        return len(self.column_boundaries) - 1

    @property
    def row_count(self) -> int:
        return len(self.row_boundaries) - 1


@dataclass(frozen=True)
class TableCell:
    container_id: str
    table_id: str
    row_index: int
    column_index: int
    row_span: int
    column_span: int
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    cell_bbox: Rect
    reading_order: int
    role: str
    translatable: bool
    protected_tokens: tuple[str, ...]
    font_size: float
    color_srgb: int
    font_weight: str
    alignment: str


@dataclass(frozen=True)
class TableTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    structure: TableStructure
    cells: tuple[TableCell, ...]
    protected_object_ids: tuple[str, ...]

    @property
    def translatable_cells(self) -> tuple[TableCell, ...]:
        return tuple(cell for cell in self.cells if cell.translatable)


@dataclass(frozen=True)
class TablePlacement:
    container_id: str
    translated_text: str
    cell_bbox: Rect
    allowed_bbox: Rect
    output_bbox: Rect
    anchor: tuple[float, float]
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    fit: bool


@dataclass(frozen=True)
class TableLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[TablePlacement, ...]


@dataclass(frozen=True)
class TableFinding:
    code: str
    severity: str
    owner: str
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class TableDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[TableFinding, ...]
