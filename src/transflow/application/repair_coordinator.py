"""实现 P9B 当前 run 页级多轮重排、比较、回滚和确定性停止。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from transflow.domain.common import content_sha256, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.repair_memory import (
    PageEffectiveLayout,
    PageRepairMemory,
    QualityVector,
    RepairAtom,
    RepairAtomCatalog,
    RepairAttempt,
    RepairAttemptStatus,
    RepairComparison,
    RepairComparisonOutcome,
    RepairProposal,
    RepairStopReason,
)

LOGGER = logging.getLogger("transflow.application.repair_coordinator")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class RepairCandidateEvidence:
    """保存一个真实物化候选及其可复算状态、质量和叶 Judge 结论。"""

    layout: PageEffectiveLayout
    quality: QualityVector
    patch_hash: str
    geometry_hash: str
    content_hash: str
    state_hash: str
    artifact_ref: str
    evidence_hash: str
    finding_codes: tuple[str, ...]
    passed: bool

    def __post_init__(self) -> None:
        """校验候选哈希和受控相对引用，禁止协调器接受伪造候选。"""

        for value, name in (
            (self.patch_hash, "patch_hash"),
            (self.geometry_hash, "geometry_hash"),
            (self.content_hash, "content_hash"),
            (self.state_hash, "state_hash"),
            (self.evidence_hash, "evidence_hash"),
        ):
            require_sha256(value, name)
        path = Path(self.artifact_ref)
        if path.is_absolute() or ".." in path.parts:
            raise DomainContractError(
                ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
                "修复候选引用必须是受控相对路径",
            )


@dataclass(frozen=True, slots=True)
class RepairSeed:
    """绑定 candidate-0 的质量、Finding、当前批准 Patch 和真实候选证据。"""

    evidence: RepairCandidateEvidence


class RepairMaterializationError(RuntimeError):
    """表示动作已实际执行但未能安全形成候选，携带结构化失败证据。"""

    def __init__(self, error_code: str, evidence_hash: str) -> None:
        """保存无秘密错误码与实际失败证据哈希。"""

        super().__init__(error_code)
        self.error_code = error_code
        self.evidence_hash = require_sha256(evidence_hash, "evidence_hash")


class RepairMaterializer(Protocol):
    """声明叶 Adapter 执行一个手工登记原子并物化真实候选的最小接口。"""

    def materialize(
        self,
        atom: RepairAtom,
        proposal: RepairProposal,
        attempt_no: int,
    ) -> RepairCandidateEvidence:
        """从不可变源页和当前最佳完整计划执行、写入并复核候选。"""

        ...


class RepairMemoryCommitter(Protocol):
    """声明每轮 Artifact 完成后提交 append-only 页记忆 Checkpoint 的接口。"""

    def commit(self, memory: PageRepairMemory) -> None:
        """以 CAS 语义提交当前权威页记忆。"""

        ...


@dataclass(frozen=True, slots=True)
class RepairCoordinationResult:
    """返回终态页记忆和最后批准候选，不把失败候选带入发布路径。"""

    memory: PageRepairMemory
    approved: RepairCandidateEvidence


class RepairCoordinator:
    """按叶目录每轮只执行一个动作，并在预算内接受、回滚或降级。"""

    def execute(
        self,
        memory: PageRepairMemory,
        seed: RepairSeed,
        catalog: RepairAtomCatalog,
        comparator: RepairComparison,
        materializer: RepairMaterializer,
        committer: RepairMemoryCommitter | None = None,
        *,
        available_facts: frozenset[str],
        active_conditions: frozenset[str] = frozenset(),
    ) -> RepairCoordinationResult:
        """执行确定性多轮闭环；布局轮次不持有 TranslationPort，也不修改文档记忆。"""

        LOGGER.info(
            "调用页级修复协调，意图=执行有限多轮重排 page_no=%s route=%s",
            memory.identity.page_no,
            memory.identity.route,
        )
        self._validate_bindings(memory, seed, catalog, comparator)
        approved = seed.evidence
        if approved.passed:
            finalized = memory.finalize(RepairStopReason.PASSED)
            self._commit(committer, finalized)
            return RepairCoordinationResult(finalized, approved)
        while not memory.finalized:
            if len(memory.attempts) >= memory.max_repair_rounds:
                memory = memory.finalize(RepairStopReason.BUDGET_EXHAUSTED)
                break
            choices = catalog.applicable_atoms(
                approved.finding_codes,
                available_facts,
                active_conditions,
                memory.attempted_action_keys,
                approved.state_hash,
            )
            if not choices:
                memory = memory.finalize(RepairStopReason.NO_APPLICABLE_ACTION)
                break
            atom, action_key = choices[0]
            failure_code = sorted(
                set(atom.applicable_finding_codes) & set(approved.finding_codes)
            )[0]
            parameters = tuple((item.name, item.default) for item in atom.bounded_parameters)
            proposal = RepairProposal(
                action_key=action_key,
                atom_id=atom.atom_id,
                failure_code=failure_code,
                owner=catalog.route,
                parameters=parameters,
                input_state_hash=approved.state_hash,
            )
            attempt_no = len(memory.attempts) + 1
            try:
                candidate = materializer.materialize(atom, proposal, attempt_no)
            except RepairMaterializationError as error:
                attempt = RepairAttempt(
                    attempt_no=attempt_no,
                    proposal=proposal,
                    status=RepairAttemptStatus.MATERIALIZATION_FAILED,
                    layout_before_hash=memory.current_layout.layout_hash,
                    layout_after=None,
                    quality_before=approved.quality,
                    quality_after=None,
                    output_state_hash=None,
                    candidate_artifact_ref=None,
                    patch_hash=None,
                    evidence_hash=error.evidence_hash,
                    error_code=error.error_code,
                )
                memory = memory.append(
                    attempt,
                    current_layout=memory.current_layout,
                    no_improvement=True,
                )
                self._commit(committer, memory)
                if self._must_stop_without_improvement(memory):
                    memory = memory.finalize(self._budget_stop_reason(memory))
                continue
            comparison = comparator.compare(approved.quality, candidate.quality)
            is_cycle = candidate.state_hash in memory.seen_state_hashes
            if is_cycle:
                status = RepairAttemptStatus.REJECTED
            elif comparison is RepairComparisonOutcome.IMPROVED:
                status = RepairAttemptStatus.ACCEPTED
            elif comparison in {
                RepairComparisonOutcome.REGRESSED,
                RepairComparisonOutcome.HARD_REJECTED,
            }:
                status = RepairAttemptStatus.ROLLED_BACK
            else:
                status = RepairAttemptStatus.REJECTED
            attempt = RepairAttempt(
                attempt_no=attempt_no,
                proposal=proposal,
                status=status,
                layout_before_hash=memory.current_layout.layout_hash,
                layout_after=candidate.layout,
                quality_before=approved.quality,
                quality_after=candidate.quality,
                output_state_hash=candidate.state_hash,
                candidate_artifact_ref=candidate.artifact_ref,
                patch_hash=candidate.patch_hash,
                evidence_hash=candidate.evidence_hash,
            )
            accepted = status is RepairAttemptStatus.ACCEPTED
            memory = memory.append(
                attempt,
                current_layout=candidate.layout if accepted else memory.current_layout,
                no_improvement=not accepted,
            )
            self._commit(committer, memory)
            if is_cycle:
                memory = memory.finalize(RepairStopReason.STATE_CYCLE)
            elif comparison is RepairComparisonOutcome.HARD_REJECTED:
                memory = memory.finalize(RepairStopReason.HARD_CONSTRAINT_FAILED)
            elif accepted:
                approved = candidate
                if candidate.passed:
                    memory = memory.finalize(RepairStopReason.PASSED)
            elif self._must_stop_without_improvement(memory):
                memory = memory.finalize(self._budget_stop_reason(memory))
        self._commit(committer, memory)
        return RepairCoordinationResult(memory, approved)

    @staticmethod
    def _validate_bindings(
        memory: PageRepairMemory,
        seed: RepairSeed,
        catalog: RepairAtomCatalog,
        comparator: RepairComparison,
    ) -> None:
        """拒绝陈旧页记忆、错误叶目录、comparator 或 candidate-0 身份。"""

        identity = memory.identity
        if (
            identity.atom_catalog_hash != catalog.catalog_hash
            or identity.comparator_hash != comparator.comparator_hash
            or identity.route != catalog.route
            or identity.toolbox_id != catalog.toolbox_id
            or identity.toolbox_version != catalog.toolbox_version
            or seed.evidence.layout != memory.initial_layout
            or seed.evidence.state_hash != memory.initial_state_hash
        ):
            raise DomainContractError(ErrorCode.CHECKPOINT_INCOMPATIBLE, "P9B 运行指纹不匹配")

    @staticmethod
    def _must_stop_without_improvement(memory: PageRepairMemory) -> bool:
        """判断预算或连续无改善是否已达到统一配置阈值。"""

        return (
            len(memory.attempts) >= memory.max_repair_rounds
            or memory.consecutive_no_improvement >= memory.max_no_improvement
        )

    @staticmethod
    def _budget_stop_reason(memory: PageRepairMemory) -> RepairStopReason:
        """让预算耗尽优先于无改善输出稳定停止原因。"""

        if len(memory.attempts) >= memory.max_repair_rounds:
            return RepairStopReason.BUDGET_EXHAUSTED
        return RepairStopReason.NO_IMPROVEMENT

    @staticmethod
    def _commit(
        committer: RepairMemoryCommitter | None,
        memory: PageRepairMemory,
    ) -> None:
        """在配置持久化边界时提交当前页记忆；纯内存测试允许显式省略。"""

        if committer is not None:
            committer.commit(memory)


def materialization_failure_evidence(action_key: str, error_code: str) -> str:
    """从动作身份和稳定错误码生成不含异常正文的物化失败证据哈希。"""

    return content_sha256({"action_key": action_key, "error_code": error_code})


def main() -> int:
    """记录 P9B 协调器不调用翻译或 Repair 模型的运行边界。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("RepairCoordinator 示例，意图=仅编排叶目录中的确定性修复原子")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
