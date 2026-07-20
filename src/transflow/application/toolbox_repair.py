"""把已迁移 Toolbox 黑盒 repair 窄化接入 P9B RepairCoordinator。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from transflow.application.repair_catalog import RepairPolicySnapshot
from transflow.application.repair_coordinator import (
    RepairCandidateEvidence,
    RepairCoordinator,
    RepairMaterializationError,
    RepairMaterializer,
    RepairSeed,
    materialization_failure_evidence,
)
from transflow.domain.artifacts import ArtifactReference
from transflow.domain.common import content_sha256
from transflow.domain.completeness import bundle_content_hash
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.layout_memory import DocumentLayoutMemory
from transflow.domain.pages import PageExecutionContext
from transflow.domain.repair_memory import (
    PageEffectiveLayout,
    PageRepairMemory,
    QualityVector,
    RepairAtom,
    RepairMemoryIdentity,
    RepairProposal,
    repair_state_hash,
)
from transflow.domain.toolbox import PagePatch
from transflow.domain.translation import TranslationBundle
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.contracts import PageToolbox, ToolboxCandidate, ToolboxJudgement

LOGGER = logging.getLogger("transflow.application.toolbox_repair")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


class CandidatePdfPort(Protocol):
    """声明从当前真实源页物化一个 Toolbox 候选 PDF 的最小接口。"""

    def render_pdf(self, patch: PagePatch | None) -> bytes:
        """返回可由真实 PDF 解析器重新打开的完整候选字节。"""

        ...


class RepairMemoryRuntimePort(Protocol):
    """声明 P9B 应用层所需的最小页记忆持久化接口。"""

    def put_candidate(self, action_key: str, content: bytes) -> ArtifactReference:
        """在页记忆提交前幂等写入一个真实候选 PDF。"""

        ...

    def put_candidate_zero(self, content: bytes) -> ArtifactReference:
        """幂等写入不可变 candidate-0 基线。"""

        ...

    def commit(self, memory: PageRepairMemory) -> None:
        """以 CAS 语义提交 append-only 页修复记忆。"""

        ...

    def restore(self, expected_identity: RepairMemoryIdentity) -> PageRepairMemory | None:
        """只恢复完整身份兼容的同 run 页记忆。"""

        ...


class RepairRuntimeFactory(Protocol):
    """声明按完整页身份创建 G3 Artifact/Checkpoint 运行时的接口。"""

    def __call__(self, identity: RepairMemoryIdentity) -> RepairMemoryRuntimePort:
        """返回绑定同一 run/page 的文件运行时。"""

        ...


@dataclass(frozen=True, slots=True)
class ToolboxRepairExecution:
    """返回最后批准的真实 Toolbox 候选、Judge 和终态页记忆。"""

    candidate: ToolboxCandidate
    judgement: ToolboxJudgement
    memory: PageRepairMemory


class LegacyToolboxRepairMaterializer(RepairMaterializer):
    """只暴露手工登记的 legacy_repair 原子，不反射发现工具或跨叶调用。"""

    def __init__(
        self,
        toolbox: PageToolbox,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
        layout: PageEffectiveLayout,
        renderer: CandidatePdfPort,
        runtime: RepairMemoryRuntimePort,
    ) -> None:
        """绑定一个叶、candidate-0、同一 TranslationBundle 派生布局和文件边界。"""

        self._toolbox = toolbox
        self._candidate = candidate
        self._judgement = judgement
        self._layout = layout
        self._renderer = renderer
        self._runtime = runtime
        self._candidates: dict[str, tuple[ToolboxCandidate, ToolboxJudgement]] = {}

    def materialize(
        self,
        atom: RepairAtom,
        proposal: RepairProposal,
        attempt_no: int,
    ) -> RepairCandidateEvidence:
        """调用一次叶私有 repair、页内回流和 Judge，再写入真实 PDF 候选。"""

        if atom.apply_adapter != "legacy_repair":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "未知 Legacy Repair Adapter")
        LOGGER.info(
            "调用 Legacy Repair Adapter，意图=执行手工登记叶动作 atom=%s attempt=%s",
            atom.atom_id,
            attempt_no,
        )
        try:
            candidate = self._toolbox.repair(self._candidate, self._judgement)
            judgement = self._toolbox.judge(candidate)
            content = self._renderer.render_pdf(candidate.plan.patch)
            reference = self._runtime.put_candidate(proposal.action_key, content)
        except (OSError, ValueError, PortCallError) as error:
            error_code = f"{type(error).__name__.upper()}_MATERIALIZATION_FAILED"
            raise RepairMaterializationError(
                error_code,
                materialization_failure_evidence(proposal.action_key, error_code),
            ) from error
        layout = _layout_after(self._layout, candidate)
        evidence = _candidate_evidence(
            candidate,
            judgement,
            layout,
            reference.relative_path,
            content,
        )
        self._candidates[evidence.state_hash] = (candidate, judgement)
        return evidence

    def register_seed(
        self,
        evidence: RepairCandidateEvidence,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> None:
        """登记 candidate-0，使无修复或回滚终态也能返回实际批准对象。"""

        self._candidates[evidence.state_hash] = (candidate, judgement)

    def approved(self, state_hash: str) -> tuple[ToolboxCandidate, ToolboxJudgement]:
        """按协调器批准状态返回对应 Toolbox 候选和其真实 Judge。"""

        return self._candidates[state_hash]


class P9BToolboxRepairHandler:
    """为 ToolboxPageCoordinator 构造 candidate-0、页记忆并运行 P9B 闭环。"""

    def __init__(
        self,
        policy: RepairPolicySnapshot,
        document_memory: DocumentLayoutMemory,
        run_token: str,
        schema_hash: str,
        implementation_hash: str,
        runtime_factory: RepairRuntimeFactory,
        renderer: CandidatePdfPort,
    ) -> None:
        """绑定只读文档记忆、配置指纹、Worker fencing 和真实 PDF 边界。"""

        self._policy = policy
        self._document_memory = document_memory
        self._run_token = run_token
        self._schema_hash = schema_hash
        self._implementation_hash = implementation_hash
        self._runtime_factory = runtime_factory
        self._renderer = renderer

    def execute(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        toolbox: PageToolbox,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
        bundle: TranslationBundle,
    ) -> ToolboxRepairExecution:
        """固定同一 Bundle 和 document memory，执行本页确定性多轮修复。"""

        memory_ref = context.document_layout_memory_ref
        if memory_ref is None or memory_ref.memory_hash != self._document_memory.memory_hash:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "P9B 缺少权威文档记忆")
        catalog, comparator = self._policy.resolve(toolbox.descriptor.route)
        bundle_hash = bundle_content_hash(bundle)
        layout = _initial_layout(
            self._document_memory,
            facts,
            toolbox.descriptor.route,
            bundle_hash,
        )
        identity = RepairMemoryIdentity(
            run_id=context.run_id,
            run_token=self._run_token,
            source_hash=context.source_hash,
            page_no=context.page_no,
            route=toolbox.descriptor.route,
            toolbox_id=catalog.toolbox_id,
            toolbox_version=catalog.toolbox_version,
            config_hash=self._policy.config_hash,
            document_memory_hash=memory_ref.memory_hash,
            atom_catalog_hash=catalog.catalog_hash,
            comparator_hash=comparator.comparator_hash,
            translation_bundle_hash=bundle_hash,
            schema_hash=self._schema_hash,
            implementation_hash=self._implementation_hash,
            static_registry_hash=self._policy.static_registry.registry_hash,
        )
        runtime = self._runtime_factory(identity)
        seed_pdf = self._renderer.render_pdf(candidate.plan.patch)
        seed_ref = runtime.put_candidate_zero(seed_pdf)
        seed_evidence = _candidate_evidence(
            candidate,
            judgement,
            layout,
            seed_ref.relative_path,
            seed_pdf,
        )
        memory = PageRepairMemory(
            identity=identity,
            initial_layout=layout,
            current_layout=layout,
            initial_state_hash=seed_evidence.state_hash,
            attempts=(),
            max_repair_rounds=self._policy.max_repair_rounds,
            max_no_improvement=self._policy.max_no_improvement,
        )
        restored = runtime.restore(identity)
        if restored is not None:
            memory = restored
        materializer = LegacyToolboxRepairMaterializer(
            toolbox,
            candidate,
            judgement,
            layout,
            self._renderer,
            runtime,
        )
        materializer.register_seed(seed_evidence, candidate, judgement)
        result = RepairCoordinator().execute(
            memory,
            RepairSeed(seed_evidence),
            catalog,
            comparator,
            materializer,
            runtime,
            available_facts=frozenset({"route_capability_match", "translation_complete"}),
        )
        approved_candidate, approved_judgement = materializer.approved(
            result.approved.state_hash
        )
        return ToolboxRepairExecution(approved_candidate, approved_judgement, result.memory)


def _initial_layout(
    document_memory: DocumentLayoutMemory,
    facts: ExtractedPageFacts,
    route: str,
    bundle_hash: str,
) -> PageEffectiveLayout:
    """以文档软先验区间中值和当前页硬事实解析 candidate-0 有效布局。"""

    policy = document_memory.target_layout_policy
    return PageEffectiveLayout(
        document_memory_hash=document_memory.memory_hash,
        page_no=facts.page.page_no,
        route=route,
        source_facts_hash=facts.kernel_facts_hash,
        translation_bundle_hash=bundle_hash,
        font_scale=sum(policy.font_scale_range) / 2,
        line_spacing=sum(policy.line_spacing_range) / 2,
        paragraph_spacing=sum(policy.paragraph_spacing_range) / 2,
        wrap_mode=policy.wrap_mode,
    )


def _layout_after(
    current: PageEffectiveLayout,
    candidate: ToolboxCandidate,
) -> PageEffectiveLayout:
    """从实际候选 Patch 的字号提取页级差异，不回写 DocumentLayoutMemory。"""

    sizes = tuple(
        operation.font_size
        for operation in (candidate.plan.patch.operations if candidate.plan.patch else ())
        if operation.font_size is not None
    )
    if not sizes:
        return current
    minimum_size = min(sizes)
    return replace(
        current,
        font_scale=min(current.font_scale, minimum_size / max(sizes)),
        page_adjustments=(("minimum_font_size", minimum_size),),
    )


def _candidate_evidence(
    candidate: ToolboxCandidate,
    judgement: ToolboxJudgement,
    layout: PageEffectiveLayout,
    relative_path: str | None,
    content: bytes,
) -> RepairCandidateEvidence:
    """把 Toolbox Finding、Patch 与真实 PDF 内容归一为 comparator 可消费证据。"""

    if relative_path is None:
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "候选 Artifact 缺少相对路径")
    findings = tuple((*candidate.plan.findings, *judgement.findings))
    unique = {item.finding_id: item for item in findings}
    overflow = sum(item.code == "P9_TEXT_LAYOUT_OVERFLOW" for item in unique.values())
    hard = tuple(sorted({item.code for item in unique.values() if item.severity == "HARD"}))
    quality = QualityVector(
        metrics=(
            ("overflow", float(overflow)),
            ("unresolved_hard_findings", float(len(hard))),
        ),
        hard_failure_codes=hard,
    )
    patch_hash = content_sha256(
        candidate.plan.patch
        if candidate.plan.patch is not None
        else {"fallback": candidate.plan.fallback_requested, "route": candidate.plan.route}
    )
    geometry_hash = (
        candidate.plan.patch.geometry_hash
        if candidate.plan.patch is not None
        else layout.source_facts_hash
    )
    content_hash = hashlib.sha256(content).hexdigest()
    state_hash = repair_state_hash(
        patch_hash,
        geometry_hash,
        content_hash,
        layout.layout_hash,
    )
    passed = (
        judgement.decision.disposition.value == "ACCEPT"
        and not candidate.plan.fallback_requested
    )
    return RepairCandidateEvidence(
        layout=layout,
        quality=quality,
        patch_hash=patch_hash,
        geometry_hash=geometry_hash,
        content_hash=content_hash,
        state_hash=state_hash,
        artifact_ref=relative_path,
        evidence_hash=content_hash,
        finding_codes=tuple(sorted({item.code for item in unique.values()})),
        passed=passed,
    )


def main() -> int:
    """记录 Legacy Adapter 只暴露手工登记动作且不持有 TranslationPort。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P9BToolboxRepairHandler 示例，意图=窄化复用既有叶 repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
