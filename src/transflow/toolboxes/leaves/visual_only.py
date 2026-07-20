"""实现 visual_only 的零翻译、零 Patch、原页透传 PageToolbox。"""

from __future__ import annotations

import logging
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import (
    Decision,
    DecisionDisposition,
    ToolboxDescriptor,
)
from transflow.domain.translation import TranslationBatch
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)

LOGGER = logging.getLogger("transflow.toolboxes.leaves.visual_only")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ROUTE_VISUAL_ONLY = "visual_only"


class VisualOnlyToolbox:
    """只确认页面身份与不可变事实，不提取图片内部文字。"""

    def __init__(self) -> None:
        """建立无状态 visual_only 叶描述符。"""

        self._descriptor = ToolboxDescriptor(
            toolbox_id=ROUTE_VISUAL_ONLY,
            route=ROUTE_VISUAL_ONLY,
            contract_version=TOOLBOX_CONTRACT_VERSION,
            owner=ROUTE_VISUAL_ONLY,
        )

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回 visual_only 稳定身份。"""

        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """核对源页绑定并记录全部只读对象，不做 OCR 或语义提取。"""

        LOGGER.info(
            "调用 visual_only prepare，意图=冻结原页透传事实 page_no=%s",
            context.page_no,
        )
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "visual_only 页面事实漂移")
        object_ids = tuple(
            dict.fromkeys(
                (
                    *(item.object_id for item in facts.objects),
                    *(item.object_id for item in facts.image_objects),
                    *(item.object_id for item in facts.drawing_objects),
                )
            )
        )
        return PageTemplate(
            template_id=f"visual-template-{facts.page_identity[:24]}",
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=ROUTE_VISUAL_ONLY,
            object_ids=object_ids,
        )

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        """显式返回零翻译请求，保证 TranslationPort 调用次数为零。"""

        LOGGER.info(
            "调用 visual_only 翻译请求构造，意图=声明零 TranslationUnit page_no=%s",
            template.context.page_no,
        )
        return None

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """只接受协调器给出的显式 Skip，并建立无 Patch 透传计划。"""

        LOGGER.info(
            "调用 visual_only 消费阶段，意图=生成原页透传计划 page_no=%s",
            template.context.page_no,
        )
        if dispatch.skip_reason != "TOOLBOX_ZERO_TRANSLATION":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "visual_only 收到非零翻译结果")
        return ToolboxLayoutPlan(
            plan_id=f"visual-plan-{template.template_id[-24:]}",
            route=ROUTE_VISUAL_ONLY,
            patch=None,
            findings=(),
            passthrough_requested=True,
        )

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """对不可变事实生成候选指纹；真实原页 PNG 由生产 Pipeline 发布。"""

        LOGGER.info("调用 visual_only render，意图=证明零写入 page_no=%s", context.page_no)
        fingerprint = content_sha256(
            {
                "kernel_facts_hash": facts.kernel_facts_hash,
                "locked_objects_hash": facts.locked_objects_hash,
                "page_no": context.page_no,
                "plan_id": plan.plan_id,
            }
        )
        return ToolboxCandidate(
            candidate_id=f"visual-candidate-{facts.page_identity[:20]}",
            plan=plan,
            render_fingerprint=fingerprint,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """接受无 Patch 且显式透传的候选，拒绝任何写入漂移。"""

        valid = candidate.plan.passthrough_requested and candidate.plan.patch is None
        disposition = DecisionDisposition.ACCEPT if valid else DecisionDisposition.FALLBACK
        return ToolboxJudgement(
            findings=(),
            decision=Decision(
                decision_id=f"visual-decision-{candidate.candidate_id[-20:]}",
                disposition=disposition,
                finding_ids=(),
                reason_code="VISUAL_ONLY_PASSTHROUGH" if valid else "VISUAL_ONLY_WRITE_REJECTED",
            ),
        )

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """保持候选原样；visual_only 没有产品修复循环。"""

        LOGGER.info(
            "调用 visual_only repair，意图=保持零 Repair candidate_id=%s",
            candidate.candidate_id,
        )
        return candidate


def main() -> int:
    """记录 visual_only 的关键运行不变量。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("VisualOnlyToolbox 示例，意图=零翻译零 Patch 原页透传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
