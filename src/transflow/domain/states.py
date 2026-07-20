"""冻结 Job、Page、Checkpoint 与修复预算的状态不变量。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from transflow.domain.common import require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.states")


class JobControlState(StrEnum):
    """表示 PDF Job 的控制状态，和页面流水线状态严格分离。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_DEGRADATION = "COMPLETED_WITH_DEGRADATION"
    PROCESS_FAILED = "PROCESS_FAILED"


class PagePipelineState(StrEnum):
    """表示单页从发现到可最终化终态的流水线状态。"""

    DISCOVERED = "DISCOVERED"
    FACTS_READY = "FACTS_READY"
    CLASSIFIED = "CLASSIFIED"
    TEMPLATE_READY = "TEMPLATE_READY"
    TRANSLATION_READY = "TRANSLATION_READY"
    PATCH_READY = "PATCH_READY"
    CANDIDATE_READY = "CANDIDATE_READY"
    QUALITY_DECIDED = "QUALITY_DECIDED"
    REPAIRING = "REPAIRING"
    FALLBACK_READY = "FALLBACK_READY"
    FINALIZED = "FINALIZED"


class DocumentOutcome(StrEnum):
    """表示文档正常、降级或流程硬失败的最终结果。"""

    COMPLETED = "COMPLETED"
    COMPLETED_WITH_DEGRADATION = "COMPLETED_WITH_DEGRADATION"
    PROCESS_FAILED = "PROCESS_FAILED"


class ArtifactProduced(StrEnum):
    """表示是否形成了可引用 Artifact。"""

    YES = "YES"
    NO = "NO"


class ArtifactIntegrity(StrEnum):
    """表示 Artifact 完整性是否通过。"""

    PASS = "PASS"
    FAIL = "FAIL"


class TranslationCoverage(StrEnum):
    """表示页面翻译覆盖程度。"""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class Capability(StrEnum):
    """表示页面能力支持程度。"""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    MISSING_TOOL = "MISSING_TOOL"


class Quality(StrEnum):
    """表示页面质量 Gate 结果。"""

    PASS = "PASS"
    FAIL = "FAIL"


class Fallback(StrEnum):
    """表示页面未降级、区域回退或整页透传。"""

    NONE = "NONE"
    REGION_FALLBACK = "REGION_FALLBACK"
    PAGE_PASSTHROUGH = "PAGE_PASSTHROUGH"


JOB_TRANSITIONS: dict[JobControlState, frozenset[JobControlState]] = {
    JobControlState.QUEUED: frozenset({JobControlState.RUNNING, JobControlState.CANCELLED}),
    JobControlState.RUNNING: frozenset(
        {
            JobControlState.PAUSING,
            JobControlState.CANCELLING,
            JobControlState.RECOVERING,
            JobControlState.FINALIZING,
            JobControlState.PROCESS_FAILED,
        }
    ),
    JobControlState.PAUSING: frozenset(
        {JobControlState.PAUSED, JobControlState.CANCELLING}
    ),
    JobControlState.PAUSED: frozenset(
        {JobControlState.QUEUED, JobControlState.CANCELLED}
    ),
    JobControlState.CANCELLING: frozenset({JobControlState.CANCELLED}),
    JobControlState.RECOVERING: frozenset(
        {JobControlState.CANCELLING, JobControlState.RUNNING}
    ),
    JobControlState.FINALIZING: frozenset(
        {
            JobControlState.CANCELLING,
            JobControlState.COMPLETED,
            JobControlState.COMPLETED_WITH_DEGRADATION,
            JobControlState.PROCESS_FAILED,
        }
    ),
    JobControlState.CANCELLED: frozenset(),
    JobControlState.COMPLETED: frozenset(),
    JobControlState.COMPLETED_WITH_DEGRADATION: frozenset(),
    JobControlState.PROCESS_FAILED: frozenset(),
}

PAGE_TRANSITIONS: dict[PagePipelineState, frozenset[PagePipelineState]] = {
    PagePipelineState.DISCOVERED: frozenset({PagePipelineState.FACTS_READY}),
    PagePipelineState.FACTS_READY: frozenset({PagePipelineState.CLASSIFIED}),
    PagePipelineState.CLASSIFIED: frozenset(
        {PagePipelineState.TEMPLATE_READY, PagePipelineState.FALLBACK_READY}
    ),
    PagePipelineState.TEMPLATE_READY: frozenset({PagePipelineState.TRANSLATION_READY}),
    PagePipelineState.TRANSLATION_READY: frozenset(
        {PagePipelineState.PATCH_READY, PagePipelineState.FALLBACK_READY}
    ),
    PagePipelineState.PATCH_READY: frozenset({PagePipelineState.CANDIDATE_READY}),
    PagePipelineState.CANDIDATE_READY: frozenset({PagePipelineState.QUALITY_DECIDED}),
    PagePipelineState.QUALITY_DECIDED: frozenset(
        {
            PagePipelineState.REPAIRING,
            PagePipelineState.FINALIZED,
            PagePipelineState.FALLBACK_READY,
        }
    ),
    PagePipelineState.REPAIRING: frozenset({PagePipelineState.CANDIDATE_READY}),
    PagePipelineState.FALLBACK_READY: frozenset({PagePipelineState.FINALIZED}),
    PagePipelineState.FINALIZED: frozenset(),
}


def transition_job(current: JobControlState, target: JobControlState) -> JobControlState:
    """执行 Job 状态转换；重复命令幂等，非法边不修改原状态。"""

    LOGGER.info("调用 Job 状态转换，意图=执行受控状态边 current=%s target=%s", current, target)
    if current == target:
        return current
    if target not in JOB_TRANSITIONS[current]:
        raise DomainContractError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Job 不允许 {current.value}->{target.value}",
        )
    return target


def transition_page(current: PagePipelineState, target: PagePipelineState) -> PagePipelineState:
    """执行 Page 状态转换；重复命令幂等，非法边不修改原状态。"""

    LOGGER.info("调用 Page 状态转换，意图=执行受控状态边 current=%s target=%s", current, target)
    if current == target:
        return current
    if target not in PAGE_TRANSITIONS[current]:
        raise DomainContractError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Page 不允许 {current.value}->{target.value}",
        )
    return target


def ensure_document_finalizable(page_states: tuple[PagePipelineState, ...]) -> None:
    """确保至少一页且全部页面 FINALIZED 后才允许文档最终化。"""

    LOGGER.info("调用文档终态屏障，意图=阻止漏页最终化 page_count=%s", len(page_states))
    if not page_states or any(state is not PagePipelineState.FINALIZED for state in page_states):
        raise DomainContractError(
            ErrorCode.DOCUMENT_NOT_FINALIZABLE,
            "所有原始页面必须进入 FINALIZED",
        )


def advance_checkpoint(current_version: int, proposed_version: int) -> int:
    """只接受严格更大的 Checkpoint 版本。"""

    LOGGER.info(
        "调用 Checkpoint 版本推进，意图=保证单调提交 current=%s proposed=%s",
        current_version,
        proposed_version,
    )
    if current_version < 0 or proposed_version <= current_version:
        raise DomainContractError(
            ErrorCode.CHECKPOINT_VERSION_NOT_MONOTONIC,
            f"current={current_version},proposed={proposed_version}",
        )
    return proposed_version


@dataclass(frozen=True, slots=True)
class CheckpointCompatibility:
    """冻结恢复前必须完全一致的源、配置、字体、Catalog 与 Schema 指纹。"""

    source_hash: str
    config_hash: str
    font_hash: str
    toolbox_catalog_hash: str
    schema_hash: str

    def __post_init__(self) -> None:
        """校验全部兼容字段都是精确 SHA-256。"""

        for field_name in (
            "source_hash",
            "config_hash",
            "font_hash",
            "toolbox_catalog_hash",
            "schema_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)


def ensure_checkpoint_compatible(
    stored: CheckpointCompatibility,
    current: CheckpointCompatibility,
) -> None:
    """拒绝在任何运行资源指纹变化后继续旧 Checkpoint。"""

    LOGGER.info("调用 Checkpoint 兼容检查，意图=阻止资源漂移后恢复旧 run")
    if stored != current:
        raise DomainContractError(ErrorCode.CHECKPOINT_INCOMPATIBLE, "恢复指纹不一致")


@dataclass(frozen=True, slots=True)
class RepairBudget:
    """记录确定性 Repair 的硬上限和已用次数。"""

    maximum_attempts: int
    attempts_used: int = 0

    def __post_init__(self) -> None:
        """校验预算和已用次数均非负且不越界。"""

        if self.maximum_attempts < 0 or not 0 <= self.attempts_used <= self.maximum_attempts:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Repair 预算字段无效")

    def consume(self) -> RepairBudget:
        """消耗一次 Repair，预算耗尽时明确拒绝。"""

        LOGGER.info(
            "调用 Repair 预算，意图=限制确定性修复次数 used=%s maximum=%s",
            self.attempts_used,
            self.maximum_attempts,
        )
        if self.attempts_used >= self.maximum_attempts:
            raise DomainContractError(ErrorCode.REPAIR_BUDGET_EXHAUSTED, "Repair 预算已耗尽")
        return RepairBudget(self.maximum_attempts, self.attempts_used + 1)


def main() -> int:
    """展示合法 Job/Page 转换和 Checkpoint 版本推进。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info(
        "状态示例完成 job=%s page=%s checkpoint=%s",
        transition_job(JobControlState.QUEUED, JobControlState.RUNNING),
        transition_page(PagePipelineState.DISCOVERED, PagePipelineState.FACTS_READY),
        advance_checkpoint(0, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
