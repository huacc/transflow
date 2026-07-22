"""实现 body.flow_text.single 的独立六阶段生产 Toolbox。"""

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
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.patch import patch_operation_hash, probe_operation_fit
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.body_flow_text_single.judge import judge_placements
from transflow.toolboxes.leaves.body_flow_text_single.layout import plan_placements
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    MINIMUM_LINE_HEIGHT,
    SingleTextContainer,
)
from transflow.toolboxes.leaves.body_flow_text_single.template import build_containers
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

LOGGER = logging.getLogger("transflow.toolboxes.leaves.body_flow_text_single")
ROUTE = "body.flow_text.single"
_TRANSLATED_LIST_LINE = re.compile(
    r"^\s*(?:[\uf0b7\u2022\u25cf\u25aa-]|\(?\d+[.)]|\(?[A-Za-z][.)]|\(?(?i:[ivxlcdm]{2,})[.)])\s+"
)


def _normalize_translated_text(text: str) -> str:
    """折叠视觉换行，只保留后续独立列表项前的语义换行。"""

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    output: list[str] = []
    for line in (item for item in lines if item):
        if not output:
            output.append(line)
        elif _TRANSLATED_LIST_LINE.match(line):
            output.append("\n" + line)
        else:
            output.append(" " + line)
    return "".join(output).strip()


@dataclass(frozen=True, slots=True)
class _SingleSnapshot:
    facts: ExtractedPageFacts
    containers: tuple[SingleTextContainer, ...]


class SingleFlowTextToolbox:
    """按原生 block 容器、固定锚点和有界字号曲线处理单列正文。"""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        self._policy = policy
        self._font_path = font_path.resolve()
        self._descriptor = ToolboxDescriptor(ROUTE, ROUTE, TOOLBOX_CONTRACT_VERSION, ROUTE)
        self._snapshots: dict[str, _SingleSnapshot] = {}
        self._facts_by_plan: dict[str, ExtractedPageFacts] = {}
        self._containers_by_plan: dict[str, tuple[SingleTextContainer, ...]] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """建立不含页眉页脚和图片文字的 single 容器。"""

        LOGGER.info("调用 single prepare，意图=冻结正文容器 page_no=%s", context.page_no)
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "single 页面事实漂移")
        containers = build_containers(facts, self._policy)
        template_id = f"body-flow-text-single-{facts.page_identity[:24]}"
        self._snapshots[template_id] = _SingleSnapshot(facts, containers)
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=ROUTE,
            object_ids=tuple(container.semantic_object_id for container in containers),
        )

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        """按容器阅读顺序构造单页、一次性 TranslationBatch。"""

        snapshot = self._snapshots[template.template_id]
        if not snapshot.containers:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    f"{snapshot.facts.page_identity}\0{container.container_id}".encode("ascii")
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=container.reading_order,
                source_text=container.source_text,
                region_id=(
                    f"body-flow-text-single-p{template.context.page_no:04d}"
                    f"-r{container.reading_order:04d}"
                ),
            )
            for container in snapshot.containers
        )
        return TranslationBatch(
            batch_id=f"batch-{template.context.run_id}-p{template.context.page_no:04d}-{ROUTE}",
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=units,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """把严格完整的译文映射为精确擦除、保留底图的声明 Patch。"""

        plan_id = f"plan-{template.template_id}"
        snapshot = self._snapshots[template.template_id]
        self._facts_by_plan[plan_id] = snapshot.facts
        self._containers_by_plan[plan_id] = snapshot.containers
        if dispatch.failure is not None:
            finding = Finding(
                f"{plan_id}-translation-failure",
                dispatch.failure.code,
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(plan_id, ROUTE, None, (finding,), True)
        if dispatch.skip_reason is not None:
            finding = Finding(
                f"{plan_id}-text-missing",
                "SINGLE_TEXT_UNIT_MISSING",
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(plan_id, ROUTE, None, (finding,), True)
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(ErrorCode.INVALID_TRANSLATION_BUNDLE, "single 缺少翻译结果")
        translated_by_unit = {
            item.unit_id: item.translated_text for item in dispatch.bundle.units
        }
        translated_by_container: dict[str, str] = {}
        for unit, container in zip(
            dispatch.batch.units,
            snapshot.containers,
            strict=True,
        ):
            translated = translated_by_unit[unit.unit_id]
            for page_number in container.preserved_page_numbers:
                translated = translated.replace(page_number, "", 1)
            translated = translated.strip(" \t\r\n-|/")
            translated = _normalize_translated_text(translated)
            if not translated:
                finding = Finding(
                    f"{plan_id}-{container.container_id}-semantic-footer-missing",
                    "SEMANTIC_FOOTER_TRANSLATION_MISSING",
                    "HARD",
                    (container.container_id,),
                )
                return ToolboxLayoutPlan(plan_id, ROUTE, None, (finding,), True)
            if container.preserved_prefix and not translated.lstrip().startswith(
                container.preserved_prefix
            ):
                translated = f"{container.preserved_prefix} {translated.lstrip()}"
            translated_by_container[container.container_id] = translated
        placements = plan_placements(
            snapshot.facts,
            snapshot.containers,
            translated_by_container,
            self._policy,
            self._font_path,
        )
        owned_ids = {
            object_id
            for container in snapshot.containers
            for object_id in container.source_object_ids
        }
        placement_findings = judge_placements(
            plan_id,
            snapshot.containers,
            placements,
            clip_box=snapshot.facts.crop_box,
            image_rects=tuple(item.bbox for item in snapshot.facts.image_objects),
            non_target_text_rects=tuple(
                item.bbox
                for item in snapshot.facts.text_spans
                if item.object_id not in owned_ids
            ),
        )
        container_by_id = {item.container_id: item for item in snapshot.containers}
        operations: list[PatchOperation] = []
        for unit, placement in zip(dispatch.batch.units, placements, strict=True):
            container = container_by_id[placement.container_id]
            hash_value = patch_operation_hash(
                owner=ROUTE,
                target_object_ids=container.source_object_ids,
                rect=placement.output_bbox,
                replacement_text=placement.translated_text,
                font_id=self._policy.font_id,
                font_size=placement.font_size,
                redaction_rects=container.source_rects,
                color_srgb=placement.color_srgb,
                line_height=placement.line_height,
                preserve_drawing_overlap=True,
            )
            operations.append(
                PatchOperation(
                    operation_id=f"op-{unit.unit_id[:20]}",
                    region_id=unit.region_id,
                    kind="replace_text",
                    payload_hash=hash_value,
                    owner=ROUTE,
                    target_object_ids=container.source_object_ids,
                    rect=placement.output_bbox,
                    replacement_text=placement.translated_text,
                    font_id=self._policy.font_id,
                    font_size=placement.font_size,
                    redaction_rects=container.source_rects,
                    color_srgb=placement.color_srgb,
                    line_height=placement.line_height,
                    preserve_drawing_overlap=True,
                )
            )
        patch = PagePatch(
            patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{ROUTE}",
            source_hash=template.context.source_hash,
            page_no=template.context.page_no,
            geometry_hash=template.context.geometry_hash,
            owner=ROUTE,
            operations=tuple(operations),
        )
        return ToolboxLayoutPlan(plan_id, ROUTE, patch, placement_findings)

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """用同一受控字体探测所有声明操作，不在叶内另写 PDF。"""

        self._facts_by_plan[plan.plan_id] = facts
        return self._candidate(plan, facts, 0)

    def _candidate(
        self,
        plan: ToolboxLayoutPlan,
        facts: ExtractedPageFacts,
        repair_round: int,
    ) -> ToolboxCandidate:
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
                if overflow_ids and not any(
                    item.code == "TEXT_LAYOUT_OVERFLOW" for item in plan.findings
                ):
                    candidate_plan = replace(
                        plan,
                        findings=(
                            *plan.findings,
                            Finding(
                                f"{plan.plan_id}-overflow-r{repair_round}",
                                "TEXT_LAYOUT_OVERFLOW",
                                "HARD",
                                overflow_ids,
                            ),
                        ),
                    )
            except (DomainContractError, PortCallError, ValueError, RuntimeError) as error:
                finding = Finding(
                    f"{plan.plan_id}-render-failed-r{repair_round}",
                    "TEXT_RENDER_CAPABILITY_FAILED",
                    "HARD",
                    (type(error).__name__,),
                )
                candidate_plan = ToolboxLayoutPlan(
                    plan.plan_id,
                    ROUTE,
                    None,
                    (*plan.findings, finding),
                    True,
                )
        return ToolboxCandidate(
            candidate_id=f"candidate-{plan.plan_id}-{repair_round}",
            plan=candidate_plan,
            render_fingerprint=content_sha256(
                {
                    "facts": facts.kernel_facts_hash,
                    "findings": tuple(item.code for item in candidate_plan.findings),
                    "plan_id": plan.plan_id,
                    "remainders": remainders,
                    "repair_round": repair_round,
                }
            ),
            repair_round=repair_round,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
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
        elif any(item.severity == "HARD" for item in findings):
            disposition = DecisionDisposition.FALLBACK
            reason = "SINGLE_HARD_FINDING"
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = "TEXT_PATCH_ACCEPTED"
        return ToolboxJudgement(
            findings,
            Decision(
                f"decision-{candidate.candidate_id}",
                disposition,
                tuple(item.finding_id for item in findings),
                reason,
            ),
        )

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """一次性降至策略最小字号和紧凑行距，随后重新探测与裁决。"""

        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        overflow_ids = {
            operation_id
            for finding in candidate.plan.findings
            if finding.code == "TEXT_LAYOUT_OVERFLOW"
            for operation_id in finding.evidence_ids
            if operation_id.startswith("op-")
        }
        repaired_operations: list[PatchOperation] = []
        changed_operation_ids: set[str] = set()
        for operation in patch.operations:
            if operation.operation_id not in overflow_ids:
                repaired_operations.append(operation)
                continue
            repaired = replace(
                operation,
                font_size=self._policy.minimum_font_size,
                line_height=MINIMUM_LINE_HEIGHT,
            )
            repaired_operations.append(
                replace(
                    repaired,
                    payload_hash=patch_operation_hash(
                        owner=ROUTE,
                        target_object_ids=repaired.target_object_ids,
                        rect=repaired.rect or (0.0, 0.0, 1.0, 1.0),
                        replacement_text=repaired.replacement_text or " ",
                        font_id=repaired.font_id or self._policy.font_id,
                        font_size=self._policy.minimum_font_size,
                        redaction_rects=repaired.redaction_rects,
                        color_srgb=repaired.color_srgb,
                        line_height=MINIMUM_LINE_HEIGHT,
                        preserve_drawing_overlap=repaired.preserve_drawing_overlap,
                    ),
                )
            )
            if repaired_operations[-1] != operation:
                changed_operation_ids.add(operation.operation_id)
        if not changed_operation_ids:
            # 规划阶段已经使用最小字号/行距时，不存在可执行的第二个动作。仍推进到
            # 有界终态，让随后 Judge 明确给出 EXHAUSTED，而不是一边整页透传、
            # 一边把 verdict 留在 REQUIRED。
            return self._candidate(
                candidate.plan,
                self._facts_by_plan[candidate.plan.plan_id],
                self._policy.repair_limit,
            )
        repaired_plan = replace(
            candidate.plan,
            patch=replace(patch, operations=tuple(repaired_operations)),
            findings=tuple(
                item
                for item in candidate.plan.findings
                if not (
                    item.code == "TEXT_LAYOUT_OVERFLOW"
                    and item.evidence_ids
                    and set(item.evidence_ids) <= changed_operation_ids
                )
            ),
        )
        return self._candidate(
            repaired_plan,
            self._facts_by_plan[candidate.plan.plan_id],
            candidate.repair_round + 1,
        )


def main() -> int:
    """说明 single 叶使用固定锚点、精确擦除和一次有界 Repair。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("SingleFlowTextToolbox 示例，意图=演示 single 核心调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
