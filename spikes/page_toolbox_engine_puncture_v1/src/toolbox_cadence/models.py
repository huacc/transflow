from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CadenceError(ValueError):
    pass


class SampleSplit(str, Enum):
    DEVELOPMENT = "development"
    REGRESSION = "regression"
    HOLDOUT = "holdout"


class ToolboxMaturity(str, Enum):
    EXPERIMENTAL = "EXPERIMENTAL"
    REGRESSION = "REGRESSION"
    PROMOTED = "PROMOTED"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"


@dataclass(frozen=True)
class ToolboxSampleRecord:
    sample_id: str
    toolbox_key: str
    split: SampleSplit
    source_ref: str
    sha256: str
    original_document_id: str
    original_page_number: int

    def __post_init__(self) -> None:
        for name in ("sample_id", "toolbox_key", "source_ref", "original_document_id"):
            if not str(getattr(self, name)).strip():
                raise CadenceError(f"{name}_is_required")
        if not SHA256_RE.fullmatch(self.sha256):
            raise CadenceError("sha256_must_be_lowercase_hex")
        if self.original_page_number < 1:
            raise CadenceError("original_page_number_must_be_positive")


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    missing_paths: tuple[str, ...]
    failed_reports: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class KernelRegressionResult:
    can_resume: bool
    required_toolboxes: tuple[str, ...]
    missing_or_failed: tuple[str, ...]

