from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConstraintFinding:
    code: str
    severity: str
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class FontProbe:
    font_file: str
    exists: bool
    loadable: bool
    glyph_count: int | None
    missing_codepoints: tuple[str, ...]

    @property
    def covers_text(self) -> bool:
        return self.exists and self.loadable and not self.missing_codepoints


@dataclass(frozen=True)
class PatchApplicationResult:
    status: str
    candidate_pdf: str | None
    candidate_sha256: str | None
    findings: tuple[ConstraintFinding, ...]
    write_evidence: tuple[dict[str, Any], ...]
    source_locked_objects_sha256: str
    candidate_locked_objects_sha256: str | None
    outside_allowed_changed_pixel_ratio: float | None
    embedded_font_resources: tuple[str, ...]


@dataclass(frozen=True)
class RepairDecision:
    outcome: str
    accepted: bool
    selected_candidate_ref: str
    round_index: int
    no_improvement_count: int
    reason: str

