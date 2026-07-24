from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class EndTextRegion:
    region_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    role: str
    disposition: str
    protection_reason: str | None
    required_literals: tuple[str, ...]
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str

    @property
    def container_id(self) -> str:
        """Expose the production wrapper's stable container identity."""

        return self.region_id


@dataclass(frozen=True)
class EndTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    source_language: str
    target_language: str
    regions: tuple[EndTextRegion, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str

    @property
    def translatable_regions(self) -> tuple[EndTextRegion, ...]:
        return tuple(region for region in self.regions if region.disposition == "translate")

    @property
    def passthrough(self) -> bool:
        return not self.translatable_regions


@dataclass(frozen=True)
class EndPlacement:
    region_id: str
    translated_text: str
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    fit: bool


@dataclass(frozen=True)
class EndLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[EndPlacement, ...]


@dataclass(frozen=True)
class EndFinding:
    code: str
    severity: str
    owner: str
    region_id: str | None
    message: str
    evidence: dict[str, object]

    @property
    def container_id(self) -> str | None:
        """Expose the production wrapper's finding binding."""

        return self.region_id


@dataclass(frozen=True)
class EndDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[EndFinding, ...]
