"""Private immutable contracts for the lifted visual-anchored core."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]
Rgb = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class VisualTextSlot:
    """Bind one native text flow to a source-derived visual region."""

    slot_id: str
    semantic_owner: str
    hard_boundary_bbox: Rect
    layout_search_bbox: Rect
    anchor_x: float
    source_object_ids: tuple[str, ...]
    background_object_ids: tuple[str, ...]
    anchor_object_ids: tuple[str, ...]
    background_evidence: str
    background_rgb: Rgb | None
    source_contrast_ratio: float | None
    z_order: str
    alignment: str
    reading_order: int


@dataclass(frozen=True, slots=True)
class VisualAnchoredContainer:
    """Describe one translation unit without moving its visual owner."""

    container_id: str
    slot_id: str
    semantic_owner: str
    source_object_ids: tuple[str, ...]
    translation_object_ids: tuple[str, ...]
    inline_keep_source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    hard_boundary_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    role: str
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str


@dataclass(frozen=True, slots=True)
class VisualBilingualCandidate:
    """Record a structural candidate without authorizing semantic de-dup."""

    source_container_id: str
    target_container_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VisualAnchoredTemplate:
    """Freeze visual slots, owners, and text disposition before translation."""

    page_id: str
    toolbox_key: str
    width: float
    height: float
    visual_slots: tuple[VisualTextSlot, ...]
    containers: tuple[VisualAnchoredContainer, ...]
    protected_object_ids: tuple[str, ...]
    locked_visual_object_ids: tuple[str, ...]
    structure_sha256: str
    capability_codes: tuple[str, ...] = ()
    ambiguous_container_ids: tuple[str, ...] = ()
    bilingual_candidates: tuple[VisualBilingualCandidate, ...] = ()

    @property
    def translatable_containers(
        self,
    ) -> tuple[VisualAnchoredContainer, ...]:
        """Return only containers with pre-authorized source translation."""

        return tuple(item for item in self.containers if item.translation_object_ids)


@dataclass(frozen=True, slots=True)
class VisualAnchoredRepairAttempt:
    """Record one bounded fit profile without a Provider retry."""

    container_id: str
    profile: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class VisualAnchoredPlacement:
    """Describe one declarative, source-bound text placement."""

    container_id: str
    slot_id: str
    translated_text: str
    output_bbox: Rect
    measured_glyph_bbox: Rect | None
    font_size: float
    minimum_font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    profile: str
    fit: bool


@dataclass(frozen=True, slots=True)
class VisualAnchoredLayoutPlan:
    """Collect deterministic placements and bounded repair evidence."""

    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[VisualAnchoredPlacement, ...]
    repair_attempts: tuple[VisualAnchoredRepairAttempt, ...]


@dataclass(frozen=True, slots=True)
class VisualAnchoredFinding:
    """Keep leaf-private findings machine-readable until projection."""

    code: str
    severity: str
    container_id: str | None
