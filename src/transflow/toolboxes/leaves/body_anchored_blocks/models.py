"""Private immutable contracts for the lifted anchored-block core."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class AnchoredBlockOwner:
    """Bind one independent text block to a source-derived safe region."""

    owner_id: str
    boundary_bbox: Rect
    anchor: tuple[float, float]
    reading_order: int
    source_object_ids: tuple[str, ...]
    container_ids: tuple[str, ...]
    protected_object_ids: tuple[str, ...]
    background_object_ids: tuple[str, ...]
    boundary_source: str


@dataclass(frozen=True, slots=True)
class AnchoredContainer:
    """Keep one translation unit inside its immutable owner and slot."""

    container_id: str
    block_owner_id: str
    source_object_ids: tuple[str, ...]
    translation_object_ids: tuple[str, ...]
    inline_keep_source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    slot_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    role: str
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str


@dataclass(frozen=True, slots=True)
class AnchoredBlocksTemplate:
    """Freeze every block owner before translated text is available."""

    page_id: str
    toolbox_key: str
    width: float
    height: float
    block_owners: tuple[AnchoredBlockOwner, ...]
    containers: tuple[AnchoredContainer, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str
    ambiguous_container_ids: tuple[str, ...] = ()

    @property
    def translatable_containers(self) -> tuple[AnchoredContainer, ...]:
        return tuple(item for item in self.containers if item.translation_object_ids)


@dataclass(frozen=True, slots=True)
class AnchoredRepairAttempt:
    """Record one bounded fit profile without a leaf-local retry loop."""

    container_id: str
    block_owner_id: str
    profile: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class AnchoredPlacement:
    """Describe one source-bound placement without writing a PDF."""

    container_id: str
    block_owner_id: str
    translated_text: str
    output_bbox: Rect
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    profile: str
    fit: bool


@dataclass(frozen=True, slots=True)
class AnchoredLayoutPlan:
    """Collect deterministic placements and finite fit evidence."""

    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[AnchoredPlacement, ...]
    repair_attempts: tuple[AnchoredRepairAttempt, ...]


@dataclass(frozen=True, slots=True)
class AnchoredFinding:
    """Keep leaf-private findings machine-readable until shared projection."""

    code: str
    severity: str
    container_id: str | None
