"""定义安全最终交付、翻译诊断候选及发布隔离合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.artifacts import ArtifactReference
from transflow.domain.common import require_non_empty, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.delivery")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DIAGNOSTIC_SCHEMA = "transflow.translated-diagnostic/v1"


class DiagnosticStatus(StrEnum):
    """表示翻译诊断候选的三个互斥终态。"""

    TRANSLATED_DIAGNOSTIC_READY = "TRANSLATED_DIAGNOSTIC_READY"
    DIAGNOSTIC_MATERIALIZATION_FAILED = "DIAGNOSTIC_MATERIALIZATION_FAILED"
    NO_TRANSLATED_CANDIDATE = "NO_TRANSLATED_CANDIDATE"


class ReleaseSurface(StrEnum):
    """列出诊断 Artifact 永远不得进入的产品发布表面。"""

    FINAL_PATCH = "FINAL_PATCH"
    PREVIEW = "PREVIEW"
    DOWNLOAD = "DOWNLOAD"
    STANDALONE_FINAL = "STANDALONE_FINAL"
    TARGET_POINTER = "TARGET_POINTER"


@dataclass(frozen=True, slots=True)
class FinalDeliveryArtifact:
    """把可安全交付的完整 PDF 与是否源文透传分开表达。"""

    artifact: ArtifactReference
    source_passthrough: bool

    def __post_init__(self) -> None:
        """只接受 final 标签且媒体类型为 PDF 的已登记引用。"""

        if self.artifact.label != "final" or self.artifact.media_type != "application/pdf":
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "FinalDeliveryArtifact 必须引用 final PDF",
            )


@dataclass(frozen=True, slots=True)
class DiagnosticUnitEvidence:
    """记录一个语义单元在真实诊断 PDF 中的提取、字体与 bbox 证据。"""

    unit_id: str
    expected_text_hash: str
    extracted: bool
    font_names: tuple[str, ...]
    bboxes: tuple[tuple[float, float, float, float], ...]

    def __post_init__(self) -> None:
        """校验身份、哈希和实际字体集合不含重复。"""

        require_non_empty(self.unit_id, "diagnostic.unit_id")
        require_sha256(self.expected_text_hash, "diagnostic.expected_text_hash")
        require_unique(self.font_names, "diagnostic.font_names")

    def to_dict(self) -> dict[str, Any]:
        """序列化为纯 JSON 证据对象。"""

        return {
            "bboxes": [list(item) for item in self.bboxes],
            "expected_text_hash": self.expected_text_hash,
            "extracted": self.extracted,
            "font_names": list(self.font_names),
            "unit_id": self.unit_id,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticEvidence:
    """汇总诊断 PDF 的来源、几何、unit、字形和越权验证结果。"""

    source_hash: str
    candidate_hash: str | None
    page_count: int
    expected_unit_count: int
    materialized_unit_count: int
    missing_unit_ids: tuple[str, ...]
    geometry_preserved: bool
    glyph_failure_count: int
    owner_violation_count: int
    protected_violation_count: int
    outside_owner_diff_ratio: float
    units: tuple[DiagnosticUnitEvidence, ...]
    failure_type: str | None = None

    def __post_init__(self) -> None:
        """校验计数、哈希、单位身份和差异比例范围。"""

        require_sha256(self.source_hash, "diagnostic.source_hash")
        if self.candidate_hash is not None:
            require_sha256(self.candidate_hash, "diagnostic.candidate_hash")
        if min(
            self.page_count,
            self.expected_unit_count,
            self.materialized_unit_count,
            self.glyph_failure_count,
            self.owner_violation_count,
            self.protected_violation_count,
        ) < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "诊断证据计数不得为负")
        if not 0 <= self.outside_owner_diff_ratio <= 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "区域外差异比例无效")
        require_unique(self.missing_unit_ids, "diagnostic.missing_unit_ids")
        require_unique(tuple(item.unit_id for item in self.units), "diagnostic.units.unit_id")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可进入 Artifact manifest 的纯 JSON 证据。"""

        return {
            "candidate_hash": self.candidate_hash,
            "expected_unit_count": self.expected_unit_count,
            "failure_type": self.failure_type,
            "geometry_preserved": self.geometry_preserved,
            "glyph_failure_count": self.glyph_failure_count,
            "materialized_unit_count": self.materialized_unit_count,
            "missing_unit_ids": list(self.missing_unit_ids),
            "outside_owner_diff_ratio": self.outside_owner_diff_ratio,
            "owner_violation_count": self.owner_violation_count,
            "page_count": self.page_count,
            "protected_violation_count": self.protected_violation_count,
            "source_hash": self.source_hash,
            "units": [item.to_dict() for item in self.units],
        }


@dataclass(frozen=True, slots=True)
class TranslatedDiagnosticCandidate:
    """表示与 final 隔离的真实译文诊断候选或诚实失败状态。"""

    status: DiagnosticStatus
    page_no: int | None
    map_hash: str | None
    bundle_hash: str | None
    decision_hash: str | None
    artifact: ArtifactReference | None
    evidence: DiagnosticEvidence
    schema_version: str = DIAGNOSTIC_SCHEMA

    def __post_init__(self) -> None:
        """校验 READY 必须有 diagnostic PDF，失败状态不得挂伪 Artifact。"""

        if self.schema_version != DIAGNOSTIC_SCHEMA:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "诊断候选 Schema 无效")
        for value, name in (
            (self.map_hash, "map_hash"),
            (self.bundle_hash, "bundle_hash"),
            (self.decision_hash, "decision_hash"),
        ):
            if value is not None:
                require_sha256(value, name)
        if self.page_no is not None and self.page_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "诊断候选页码无效")
        if self.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY:
            if (
                self.artifact is None
                or self.artifact.label != "diagnostic"
                or self.artifact.media_type != "application/pdf"
                or None in (self.map_hash, self.bundle_hash, self.decision_hash)
            ):
                raise DomainContractError(
                    ErrorCode.DIAGNOSTIC_INVALID,
                    "READY 诊断候选缺少独立 PDF 或合同哈希",
                )
        elif self.artifact is not None:
            raise DomainContractError(
                ErrorCode.DIAGNOSTIC_INVALID,
                "失败或无候选状态不得登记 Artifact",
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化诊断状态、引用和实际证据。"""

        artifact = None
        if self.artifact is not None:
            artifact = {
                "artifact_id": self.artifact.artifact_id,
                "content_hash": self.artifact.content_hash,
                "label": self.artifact.label,
                "media_type": self.artifact.media_type,
                "relative_path": self.artifact.relative_path,
                "size_bytes": self.artifact.size_bytes,
            }
        return {
            "artifact": artifact,
            "bundle_hash": self.bundle_hash,
            "decision_hash": self.decision_hash,
            "evidence": self.evidence.to_dict(),
            "map_hash": self.map_hash,
            "page_no": self.page_no,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }


class ReleaseArtifactGuard:
    """在 final、preview、download 与 target 边界拒绝 diagnostic 引用。"""

    @staticmethod
    def assert_allowed(reference: ArtifactReference, surface: ReleaseSurface) -> None:
        """校验引用标签与产品发布表面相容。"""

        LOGGER.info(
            "调用发布隔离，意图=阻止诊断产物进入产品表面 surface=%s label=%s",
            surface,
            reference.label,
        )
        if reference.label == "diagnostic":
            raise DomainContractError(
                ErrorCode.DIAGNOSTIC_RELEASE_FORBIDDEN,
                f"diagnostic 不得进入 {surface.value}",
            )
        required_label = {
            ReleaseSurface.FINAL_PATCH: "page",
            ReleaseSurface.PREVIEW: "preview",
            ReleaseSurface.DOWNLOAD: "final",
            ReleaseSurface.STANDALONE_FINAL: "final",
            ReleaseSurface.TARGET_POINTER: "final",
        }[surface]
        if reference.label != required_label:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                f"{surface.value} 要求 {required_label} Artifact",
            )


def main() -> int:
    """记录 final 与 diagnostic 必须使用不同领域类型。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("交付双轨示例，意图=安全 final 与翻译诊断候选永不互推")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
