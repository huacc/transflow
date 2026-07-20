"""定义页面事实、执行上下文、处理计划和页面结果合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from transflow.domain.common import require_non_empty, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.layout_memory import DocumentLayoutMemoryRef
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)

LOGGER = logging.getLogger("transflow.domain.pages")


@dataclass(frozen=True, slots=True)
class PageFacts:
    """表示从一张原始页面获得且可复现的基础事实。"""

    source_hash: str
    page_no: int
    width_points: float
    height_points: float
    geometry_hash: str
    facts_hash: str

    def __post_init__(self) -> None:
        """校验页面编号、页面尺寸和全部内容指纹。"""

        require_sha256(self.source_hash, "source_hash")
        require_sha256(self.geometry_hash, "geometry_hash")
        require_sha256(self.facts_hash, "facts_hash")
        if self.page_no < 0 or self.width_points <= 0 or self.height_points <= 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "页面编号或尺寸无效")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PageFacts:
        """从 JSON 字典恢复页面事实并重新执行合同校验。"""

        return cls(**payload)


@dataclass(frozen=True, slots=True)
class PageExecutionContext:
    """绑定页面处理所需的 Job、Run、源文件和页面身份。"""

    job_id: str
    run_id: str
    source_hash: str
    page_no: int
    geometry_hash: str
    config_snapshot_hash: str
    document_layout_memory_ref: DocumentLayoutMemoryRef | None = None

    def __post_init__(self) -> None:
        """校验上下文身份完整且页面编号非负。"""

        require_non_empty(self.job_id, "job_id")
        require_non_empty(self.run_id, "run_id")
        require_sha256(self.source_hash, "source_hash")
        require_sha256(self.geometry_hash, "geometry_hash")
        require_sha256(self.config_snapshot_hash, "config_snapshot_hash")
        if self.page_no < 0:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "page_no 不得为负")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PageExecutionContext:
        """从 JSON 字典恢复页面执行上下文。"""

        normalized = dict(payload)
        memory_ref = normalized.get("document_layout_memory_ref")
        if isinstance(memory_ref, dict):
            normalized["document_layout_memory_ref"] = DocumentLayoutMemoryRef(**memory_ref)
        return cls(**normalized)


@dataclass(frozen=True, slots=True)
class PagePlan:
    """表示分类完成后冻结的单页执行计划。"""

    route: str
    toolbox_id: str
    owner: str
    ordered_unit_ids: tuple[str, ...]
    repair_limit: int

    def __post_init__(self) -> None:
        """校验路由、所有者、单元顺序和修复上限。"""

        require_non_empty(self.route, "route")
        require_non_empty(self.toolbox_id, "toolbox_id")
        require_non_empty(self.owner, "owner")
        require_unique(self.ordered_unit_ids, "ordered_unit_ids")
        if self.repair_limit < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "repair_limit 不得为负")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PagePlan:
        """从 JSON 字典恢复计划，并保留翻译单元原始顺序。"""

        return cls(
            route=payload["route"],
            toolbox_id=payload["toolbox_id"],
            owner=payload["owner"],
            ordered_unit_ids=tuple(payload["ordered_unit_ids"]),
            repair_limit=payload["repair_limit"],
        )


@dataclass(frozen=True, slots=True)
class PageOutcome:
    """用相互独立的维度表达单页终态，避免以一个布尔值掩盖降级。"""

    page_no: int
    state: PagePipelineState
    artifact_produced: ArtifactProduced
    integrity: ArtifactIntegrity
    translation_coverage: TranslationCoverage
    capability: Capability
    quality: Quality
    fallback: Fallback
    finding_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验页面身份、终态要求以及发现项身份唯一性。"""

        if self.page_no < 0:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "page_no 不得为负")
        if self.state is not PagePipelineState.FINALIZED:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "PageOutcome 必须处于 FINALIZED")
        require_unique(self.finding_codes, "finding_codes")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PageOutcome:
        """从 JSON 字典恢复多维页面终态。"""

        return cls(
            page_no=payload["page_no"],
            state=PagePipelineState(payload["state"]),
            artifact_produced=ArtifactProduced(payload["artifact_produced"]),
            integrity=ArtifactIntegrity(payload["integrity"]),
            translation_coverage=TranslationCoverage(payload["translation_coverage"]),
            capability=Capability(payload["capability"]),
            quality=Quality(payload["quality"]),
            fallback=Fallback(payload["fallback"]),
            finding_codes=tuple(payload.get("finding_codes", ())),
        )


def main() -> int:
    """展示页面执行上下文的最小构造方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    context = PageExecutionContext("job-example", "run-example", "0" * 64, 0, "1" * 64, "2" * 64)
    LOGGER.info("调用页面上下文示例，意图=展示稳定页面身份 page_no=%s", context.page_no)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
