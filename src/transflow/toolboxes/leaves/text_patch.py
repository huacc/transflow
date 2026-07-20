"""实现第一批文本叶共用的 unit、Patch、Judge 与一次有界 Repair。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import (
    Decision,
    DecisionDisposition,
    Finding,
    PagePatch,
    PatchOperation,
    ToolboxDescriptor,
)
from transflow.domain.translation import TranslationBatch, TranslationUnit
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageObjectFact
from transflow.pdf_kernel.patch import patch_operation_hash, probe_operation_fit
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

LOGGER = logging.getLogger("transflow.toolboxes.leaves.text_patch")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ROUTE_SINGLE = "body.flow_text.single"
ROUTE_CHART = "body.chart"
ROUTE_DIAGRAM = "body.diagram"
LITERAL_PREFIX = re.compile(r"^(\s*(?:(?:[-*•])|(?:\d+[.)]))\s+)")
PAGE_NUMBER = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _TextSnapshot:
    """保存模板后续阶段需要的机械文本事实，不持有打开的 PDF。"""

    facts: ExtractedPageFacts
    objects: tuple[PageObjectFact, ...]
    literal_prefixes: tuple[tuple[str, str], ...]


def _rectangles_intersect(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    """以纯几何方式判断两个矩形是否存在正面积交集。"""

    return not (
        left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1]
    )


def _center_distance_ratio(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    width: float,
    height: float,
) -> float:
    """计算两个矩形中心点的页面归一化曼哈顿距离。"""

    left_x = (left[0] + left[2]) / 2
    left_y = (left[1] + left[3]) / 2
    right_x = (right[0] + right[2]) / 2
    right_y = (right[1] + right[3]) / 2
    return abs(left_x - right_x) / width + abs(left_y - right_y) / height


class TextPatchToolbox:
    """按 Route 私有选择规则复用稳定的文本 Patch 六阶段实现。"""

    def __init__(
        self,
        route: str,
        policy: P8ToolboxPolicy,
        font_path: Path,
    ) -> None:
        """绑定显式 Route、集中策略和已经由字体注册表验证的字体路径。"""

        if route not in {ROUTE_SINGLE, ROUTE_CHART, ROUTE_DIAGRAM}:
            raise ValueError("TextPatchToolbox Route 不受支持")
        self._route = route
        self._policy = policy
        self._font_path = font_path.resolve()
        self._descriptor = ToolboxDescriptor(route, route, TOOLBOX_CONTRACT_VERSION, route)
        self._snapshots: dict[str, _TextSnapshot] = {}
        self._facts_by_plan: dict[str, ExtractedPageFacts] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回当前叶的稳定 Toolbox 身份和 owner。"""

        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """按页面结构选择本叶可拥有的原生文本对象并冻结阅读顺序。"""

        LOGGER.info(
            "调用文本叶 prepare，意图=建立结构驱动 owner route=%s page_no=%s",
            self._route,
            context.page_no,
        )
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "文本叶页面事实漂移")
        selected = tuple(
            sorted(
                self._select_objects(facts),
                key=lambda item: (round(item.bbox[1], 4), round(item.bbox[0], 4), item.object_id),
            )
        )
        template_id = f"{self._route.replace('.', '-')}-{facts.page_identity[:24]}"
        prefixes = tuple(
            (item.object_id, match.group(1) if (match := LITERAL_PREFIX.match(item.text)) else "")
            for item in selected
        )
        self._snapshots[template_id] = _TextSnapshot(facts, selected, prefixes)
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=self._route,
            object_ids=tuple(item.object_id for item in selected),
        )

    def _select_objects(self, facts: ExtractedPageFacts) -> tuple[PageObjectFact, ...]:
        """根据 Route 使用归一化结构事实选择 owner，禁止身份或固定坐标分支。"""

        text_objects = tuple(
            item
            for item in facts.objects
            if item.kind == "text" and not item.protected and item.text.strip()
        )
        if self._route == ROUTE_SINGLE:
            top = facts.page.height_points * self._policy.body_margin_top_ratio
            bottom = facts.page.height_points * self._policy.body_margin_bottom_ratio
            return tuple(
                item
                for item in text_objects
                if top <= (item.bbox[1] + item.bbox[3]) / 2 <= bottom
                and not PAGE_NUMBER.fullmatch(item.text)
                and not any(
                    _rectangles_intersect(item.bbox, protected)
                    for protected in facts.protected_regions
                )
            )
        # V1 不 OCR：只含栅格图或没有原生绘图锚点时不领取任何标签。
        if facts.image_objects or not facts.drawing_objects:
            return ()
        distance_limit = 0.20 if self._route == ROUTE_CHART else 0.12
        return tuple(
            item
            for item in text_objects
            if min(
                _center_distance_ratio(
                    item.bbox,
                    drawing.bbox,
                    facts.page.width_points,
                    facts.page.height_points,
                )
                for drawing in facts.drawing_objects
            )
            <= distance_limit
        )

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        """按阅读顺序构造单页 TranslationBatch；无 owner 时显式零翻译。"""

        LOGGER.info(
            "调用文本叶翻译请求构造，意图=生成稳定 unit route=%s page_no=%s",
            self._route,
            template.context.page_no,
        )
        snapshot = self._snapshots[template.template_id]
        if not snapshot.objects:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    f"{snapshot.facts.page_identity}\0{item.object_id}".encode("ascii")
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=ordinal,
                source_text=item.text,
                region_id=f"{self._route}-p{template.context.page_no:04d}-r{ordinal:04d}",
            )
            for ordinal, item in enumerate(snapshot.objects)
        )
        return TranslationBatch(
            batch_id=f"batch-{template.context.run_id}-p{template.context.page_no:04d}-{self._route}",
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=units,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """把严格对齐译文转成声明 Patch，错误或缺少正文时形成确定出口。"""

        LOGGER.info(
            "调用文本叶译文消费，意图=构造声明 Patch route=%s page_no=%s",
            self._route,
            template.context.page_no,
        )
        plan_id = f"plan-{template.template_id}"
        snapshot = self._snapshots[template.template_id]
        self._facts_by_plan[plan_id] = snapshot.facts
        if dispatch.failure is not None:
            finding = Finding(
                finding_id=f"{plan_id}-translation-failure",
                code=dispatch.failure.code,
                severity="HARD",
                evidence_ids=(template.template_id,),
            )
            return ToolboxLayoutPlan(plan_id, self._route, None, (finding,), True)
        if dispatch.skip_reason is not None:
            if self._route == ROUTE_SINGLE:
                finding = Finding(
                    finding_id=f"{plan_id}-text-missing",
                    code="SINGLE_TEXT_UNIT_MISSING",
                    severity="HARD",
                    evidence_ids=(template.template_id,),
                )
                return ToolboxLayoutPlan(plan_id, self._route, None, (finding,), True)
            return ToolboxLayoutPlan(
                plan_id,
                self._route,
                None,
                (),
                passthrough_requested=True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(ErrorCode.INVALID_TRANSLATION_BUNDLE, "文本叶缺少翻译结果")
        translated_by_id = {item.unit_id: item.translated_text for item in dispatch.bundle.units}
        prefix_by_object = dict(snapshot.literal_prefixes)
        operations: list[PatchOperation] = []
        for unit, source_object in zip(dispatch.batch.units, snapshot.objects, strict=True):
            translated = translated_by_id[unit.unit_id]
            prefix = prefix_by_object[source_object.object_id]
            if prefix and not translated.startswith(prefix):
                translated = f"{prefix}{translated.lstrip()}"
            font_size = max(
                self._policy.minimum_font_size,
                min(
                    self._policy.maximum_font_size,
                    round(
                        (source_object.bbox[3] - source_object.bbox[1]) * self._policy.font_scale, 2
                    ),
                ),
            )
            payload_hash = patch_operation_hash(
                owner=self._route,
                target_object_ids=(source_object.object_id,),
                rect=source_object.bbox,
                replacement_text=translated,
                font_id=self._policy.font_id,
                font_size=font_size,
            )
            operations.append(
                PatchOperation(
                    operation_id=f"op-{unit.unit_id[:20]}",
                    region_id=unit.region_id,
                    kind="replace_text",
                    payload_hash=payload_hash,
                    owner=self._route,
                    target_object_ids=(source_object.object_id,),
                    rect=source_object.bbox,
                    replacement_text=translated,
                    font_id=self._policy.font_id,
                    font_size=font_size,
                )
            )
        patch = PagePatch(
            patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{self._route}",
            source_hash=template.context.source_hash,
            page_no=template.context.page_no,
            geometry_hash=template.context.geometry_hash,
            owner=self._route,
            operations=tuple(operations),
        )
        return ToolboxLayoutPlan(plan_id, self._route, patch, ())

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """用真实字体探测 Patch 容量并产出可复算候选，不直接发布 PDF。"""

        LOGGER.info(
            "调用文本叶 render，意图=探测真实字体容量 route=%s page_no=%s",
            self._route,
            context.page_no,
        )
        self._facts_by_plan[plan.plan_id] = facts
        return self._build_candidate(plan, facts, 0)

    def _build_candidate(
        self,
        plan: ToolboxLayoutPlan,
        facts: ExtractedPageFacts,
        repair_round: int,
    ) -> ToolboxCandidate:
        """为初次布局或 Repair 执行相同真实容量探测并记录 Finding。"""

        candidate_plan = plan
        remainders: tuple[float, ...] = ()
        if plan.patch is not None:
            try:
                remainders = tuple(
                    probe_operation_fit(facts, operation, self._font_path)
                    for operation in plan.patch.operations
                )
                overflow_ids = tuple(
                    operation.operation_id
                    for operation, remainder in zip(
                        plan.patch.operations,
                        remainders,
                        strict=True,
                    )
                    if remainder < 0
                )
                if overflow_ids:
                    finding = Finding(
                        finding_id=f"{plan.plan_id}-overflow-r{repair_round}",
                        code="TEXT_LAYOUT_OVERFLOW",
                        severity="HARD",
                        evidence_ids=overflow_ids,
                    )
                    candidate_plan = replace(plan, findings=(*plan.findings, finding))
            except (DomainContractError, PortCallError, ValueError, RuntimeError) as error:
                finding = Finding(
                    finding_id=f"{plan.plan_id}-render-failed-r{repair_round}",
                    code="TEXT_RENDER_CAPABILITY_FAILED",
                    severity="HARD",
                    evidence_ids=(type(error).__name__,),
                )
                candidate_plan = ToolboxLayoutPlan(
                    plan.plan_id,
                    plan.route,
                    None,
                    (*plan.findings, finding),
                    True,
                )
        fingerprint = content_sha256(
            {
                "findings": tuple(item.code for item in candidate_plan.findings),
                "kernel_facts_hash": facts.kernel_facts_hash,
                "plan_id": plan.plan_id,
                "remainders": remainders,
                "repair_round": repair_round,
            }
        )
        return ToolboxCandidate(
            candidate_id=f"candidate-{plan.plan_id}-{repair_round}",
            plan=candidate_plan,
            render_fingerprint=fingerprint,
            repair_round=repair_round,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """按 fallback、溢出和一次 Repair 预算给出确定裁决。"""

        LOGGER.info(
            "调用文本叶 judge，意图=裁决硬约束 route=%s repair_round=%s",
            self._route,
            candidate.repair_round,
        )
        findings = candidate.plan.findings
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = "TEXT_PLAN_FALLBACK"
        elif any(item.code == "TEXT_LAYOUT_OVERFLOW" for item in findings):
            disposition = (
                DecisionDisposition.REPAIR
                if candidate.repair_round < self._policy.repair_limit
                else DecisionDisposition.FALLBACK
            )
            reason = (
                "TEXT_REPAIR_REQUIRED"
                if disposition is DecisionDisposition.REPAIR
                else "TEXT_REPAIR_EXHAUSTED"
            )
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "TEXT_PATCH_ACCEPTED"
                if candidate.plan.patch is not None
                else "TEXT_PASSTHROUGH_ACCEPTED"
            )
        return ToolboxJudgement(
            findings=findings,
            decision=Decision(
                decision_id=f"decision-{candidate.candidate_id}",
                disposition=disposition,
                finding_ids=tuple(item.finding_id for item in findings),
                reason_code=reason,
            ),
        )

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """仅在 REPAIR 时把全部字号降至下限，并用同一探测逻辑重建候选。"""

        LOGGER.info(
            "调用文本叶 repair，意图=执行一次有界字号修复 route=%s repair_round=%s",
            self._route,
            candidate.repair_round,
        )
        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        repaired_operations = tuple(
            replace(
                operation,
                font_size=self._policy.minimum_font_size,
                payload_hash=patch_operation_hash(
                    owner=self._route,
                    target_object_ids=operation.target_object_ids,
                    rect=operation.rect or (0.0, 0.0, 1.0, 1.0),
                    replacement_text=operation.replacement_text or " ",
                    font_id=operation.font_id or self._policy.font_id,
                    font_size=self._policy.minimum_font_size,
                ),
            )
            for operation in patch.operations
        )
        repaired_patch = replace(patch, operations=repaired_operations)
        repaired_plan = replace(
            candidate.plan,
            patch=repaired_patch,
            findings=tuple(
                item for item in candidate.plan.findings if item.code != "TEXT_LAYOUT_OVERFLOW"
            ),
        )
        return self._build_candidate(
            repaired_plan,
            self._facts_by_plan[candidate.plan.plan_id],
            candidate.repair_round + 1,
        )


def main() -> int:
    """记录文本叶复用核心只处理单页 unit、Patch 和一次 Repair。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("TextPatchToolbox 示例，意图=复用稳定文本六阶段而不共享叶语义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
