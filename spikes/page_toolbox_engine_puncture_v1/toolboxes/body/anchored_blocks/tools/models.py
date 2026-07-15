from __future__ import annotations

from dataclasses import dataclass


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class BlockOwner:
    owner_id: str
    boundary_bbox: Rect
    anchor: tuple[float, float]
    reading_order: int
    source_object_ids: tuple[str, ...]
    container_ids: tuple[str, ...]
    protected_object_ids: tuple[str, ...]
    background_object_ids: tuple[str, ...]
    boundary_source: str


@dataclass(frozen=True)
class AnchoredContainer:
    container_id: str
    block_owner_id: str
    source_object_ids: tuple[str, ...]
    source_text: str
    source_bbox: Rect
    slot_bbox: Rect
    allowed_bbox: Rect
    reading_order: int
    required_literals: tuple[str, ...]
    role: str
    font_name: str
    font_size: float
    color_srgb: int
    alignment: str


@dataclass(frozen=True)
class AnchoredBlocksTemplate:
    page_id: str
    toolbox_key: str
    width: float
    height: float
    block_owners: tuple[BlockOwner, ...]
    containers: tuple[AnchoredContainer, ...]
    protected_object_ids: tuple[str, ...]
    structure_sha256: str


@dataclass(frozen=True)
class AnchoredPlacement:
    container_id: str
    block_owner_id: str
    translated_text: str
    output_bbox: Rect
    font_file: str
    font_resource: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    profile: str
    fit: bool


@dataclass(frozen=True)
class BlockRepairAttempt:
    container_id: str
    block_owner_id: str
    profile: str
    accepted: bool
    reason: str


@dataclass(frozen=True)
class AnchoredLayoutPlan:
    page_id: str
    toolbox_key: str
    structure_sha256: str
    placements: tuple[AnchoredPlacement, ...]
    repair_attempts: tuple[BlockRepairAttempt, ...]


@dataclass(frozen=True)
class AnchoredFinding:
    code: str
    severity: str
    owner: str
    block_owner_id: str | None
    container_id: str | None
    message: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class AnchoredDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    terminal_state: str
    findings: tuple[AnchoredFinding, ...]
