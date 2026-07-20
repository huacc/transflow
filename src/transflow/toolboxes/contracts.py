"""冻结 PageToolbox 六阶段接口、执行 DTO 与确定性终态映射。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from transflow.domain.common import require_non_empty, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext, PageOutcome
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.domain.toolbox import Decision, Finding, PagePatch, ToolboxDescriptor
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel.facts import ExtractedPageFacts

LOGGER = logging.getLogger("transflow.toolboxes.contracts")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TOOLBOX_CONTRACT_VERSION = "transflow.page-toolbox/v1"
SIX_STAGE_ORDER = (
    "prepare",
    "build_translation_request",
    "consume_translation_bundle",
    "render",
    "judge",
    "repair",
)


@dataclass(frozen=True, slots=True)
class PageTemplate:
    """表示 prepare 阶段生成且绑定源页、owner 和对象集合的模板。"""

    template_id: str
    context: PageExecutionContext
    facts_hash: str
    owner: str
    object_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """校验模板身份、事实指纹、所有者和对象唯一性。"""

        require_non_empty(self.template_id, "template_id")
        require_sha256(self.facts_hash, "facts_hash")
        require_non_empty(self.owner, "owner")
        require_unique(self.object_ids, "object_ids")


@dataclass(frozen=True, slots=True)
class TranslationFailure:
    """表示 PageCoordinator 交给叶子的结构化翻译失败，不携带异常对象。"""

    code: str
    retryable: bool
    detail: str

    def __post_init__(self) -> None:
        """校验错误码和无秘密说明均为非空字符串。"""

        require_non_empty(self.code, "translation_failure.code")
        require_non_empty(self.detail, "translation_failure.detail")


@dataclass(frozen=True, slots=True)
class TranslationDispatch:
    """封装已校验 Bundle、结构化错误或显式零翻译，确保出口唯一。"""

    batch: TranslationBatch | None
    bundle: TranslationBundle | None = None
    failure: TranslationFailure | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        """拒绝多出口、无批次翻译结果和身份不匹配的翻译返回。"""

        result_count = sum(
            item is not None for item in (self.bundle, self.failure, self.skip_reason)
        )
        if result_count != 1:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "TranslationDispatch 必须且只能包含 Bundle、Failure 或 Skip",
            )
        if self.skip_reason is not None:
            require_non_empty(self.skip_reason, "translation_dispatch.skip_reason")
            if self.batch is not None:
                raise DomainContractError(
                    ErrorCode.INVALID_TRANSLATION_BUNDLE,
                    "零翻译 Dispatch 不得携带 Batch",
                )
            return
        if self.batch is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "Bundle 或 Failure 必须绑定原始 Batch",
            )
        if self.bundle is not None:
            if (
                self.bundle.batch_id != self.batch.batch_id
                or self.bundle.requested_unit_ids != self.batch.ordered_unit_ids
            ):
                raise DomainContractError(
                    ErrorCode.INVALID_TRANSLATION_BUNDLE,
                    "Bundle 与原始 Batch 身份不一致",
                )


@dataclass(frozen=True, slots=True)
class ToolboxLayoutPlan:
    """表示 consume_translation_bundle 阶段形成的 Patch 与发现项计划。"""

    plan_id: str
    route: str
    patch: PagePatch | None
    findings: tuple[Finding, ...]
    fallback_requested: bool = False
    passthrough_requested: bool = False
    region_fallback_applied: bool = False

    def __post_init__(self) -> None:
        """校验计划身份、Route、发现项唯一性和三种写入出口互斥。"""

        require_non_empty(self.plan_id, "plan_id")
        require_non_empty(self.route, "route")
        require_unique(tuple(item.finding_id for item in self.findings), "findings.finding_id")
        if self.fallback_requested and self.patch is not None:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "请求 fallback 的布局计划不得同时携带 Patch",
            )
        if self.passthrough_requested and (self.patch is not None or self.fallback_requested):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "显式透传计划不得同时携带 Patch 或失败 fallback",
            )
        if self.region_fallback_applied and (
            self.patch is None or self.fallback_requested or self.passthrough_requested
        ):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "区域回退计划必须保留至少一个安全 Patch，且不得同时整页透传",
            )


@dataclass(frozen=True, slots=True)
class ToolboxCandidate:
    """表示 render 或 repair 阶段生成的可裁决候选页。"""

    candidate_id: str
    plan: ToolboxLayoutPlan
    render_fingerprint: str
    repair_round: int = 0

    def __post_init__(self) -> None:
        """校验候选身份、渲染指纹和非负修复轮次。"""

        require_non_empty(self.candidate_id, "candidate_id")
        require_sha256(self.render_fingerprint, "render_fingerprint")
        if self.repair_round < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "repair_round 不得为负")


@dataclass(frozen=True, slots=True)
class ToolboxJudgement:
    """表示 judge 阶段返回的结构化发现项和裁决。"""

    findings: tuple[Finding, ...]
    decision: Decision

    def __post_init__(self) -> None:
        """校验裁决引用的发现项都存在于当前判断结果。"""

        known = {item.finding_id for item in self.findings}
        if not set(self.decision.finding_ids).issubset(known):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Decision 引用了未知 Finding")


@dataclass(frozen=True, slots=True)
class ToolboxExecutionTrace:
    """记录六阶段顺序及最终 outcome 归一化步骤。"""

    stages: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝遗漏、重排或增加的执行阶段。"""

        if self.stages != (*SIX_STAGE_ORDER, "outcome"):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Toolbox 执行阶段顺序不闭合")


@dataclass(frozen=True, slots=True)
class ToolboxExecutionResult:
    """聚合标准 Patch、Finding、verdict、PageOutcome 与完整执行 trace。"""

    page_no: int
    patch: PagePatch | None
    findings: tuple[Finding, ...]
    verdict: Decision
    outcome: PageOutcome
    trace: ToolboxExecutionTrace
    ordered_unit_ids: tuple[str, ...]
    proposed_patch: PagePatch | None = None

    def __post_init__(self) -> None:
        """校验页面身份和翻译单元身份唯一性。"""

        if self.page_no != self.outcome.page_no:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "执行结果与 PageOutcome 串页")
        require_unique(self.ordered_unit_ids, "ordered_unit_ids")


@runtime_checkable
class PageToolbox(Protocol):
    """声明叶工具唯一允许的六阶段生产外形。"""

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回稳定 Route、版本、owner 和 Toolbox 身份。"""

        ...

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """从只读页面事实建立模板，不执行翻译或最终发布。"""

        ...

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        """构造稳定翻译请求；零翻译叶显式返回 ``None``。"""

        ...

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """消费 PageCoordinator 给出的 Bundle 或错误并形成布局计划。"""

        ...

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """把声明式计划渲染为可裁决候选，不发布最终文档。"""

        ...

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """对候选生成结构化 Finding 和确定性 verdict。"""

        ...

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """在叶内有界修复一次；无修复需求时原样返回候选。"""

        ...


def normalized_page_outcome(
    page_no: int,
    *,
    accepted: bool,
    translated: bool,
    finding_codes: tuple[str, ...],
    passthrough: bool = False,
    region_fallback: bool = False,
) -> PageOutcome:
    """把六阶段结果映射为稳定 PageOutcome，不从未知字典猜测成功。"""

    LOGGER.info(
        "调用 Toolbox 终态映射，意图=归一化页面结果 page_no=%s accepted=%s",
        page_no,
        accepted,
    )
    return PageOutcome(
        page_no=page_no,
        state=PagePipelineState.FINALIZED,
        artifact_produced=ArtifactProduced.YES if accepted else ArtifactProduced.NO,
        integrity=ArtifactIntegrity.PASS if accepted else ArtifactIntegrity.FAIL,
        translation_coverage=(
            TranslationCoverage.PARTIAL
            if accepted and translated and region_fallback
            else TranslationCoverage.FULL
            if accepted and translated
            else TranslationCoverage.NONE
        ),
        capability=(
            Capability.PARTIAL if region_fallback or not accepted else Capability.SUPPORTED
        ),
        quality=Quality.FAIL if region_fallback or not accepted else Quality.PASS,
        fallback=(
            Fallback.PAGE_PASSTHROUGH
            if passthrough or not accepted
            else Fallback.REGION_FALLBACK
            if region_fallback
            else Fallback.NONE
        ),
        finding_codes=finding_codes,
    )


def main() -> int:
    """记录六阶段固定顺序和外部终态归一化边界。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PageToolbox 合同已加载，意图=冻结六阶段顺序 stages=%s", SIX_STAGE_ORDER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
