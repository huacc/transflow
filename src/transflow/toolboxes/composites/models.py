"""Private immutable records for TBM2 root ownership and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class CompositeOwnership:
    """Expose one deterministic content owner without exposing child toolboxes."""

    object_id: str
    component: str
    container_id: str


@dataclass(frozen=True, slots=True)
class OwnedContainer:
    """Bind one root translation unit to a fixed internal layout component."""

    composite_id: str
    component: str
    internal_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    reading_order: int
