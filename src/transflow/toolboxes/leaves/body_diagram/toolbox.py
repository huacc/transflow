"""Implement the independent six-stage production Toolbox for body.diagram."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageExecutionContext
from transflow.domain.text_inventory import (
    InventoryDisposition,
    PageTextInventoryItem,
)
from transflow.domain.toolbox import (
    Decision,
    DecisionDisposition,
    Finding,
    PagePatch,
    PatchOperation,
    ToolboxDescriptor,
)
from transflow.domain.translation import (
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact
from transflow.pdf_kernel.patch import patch_operation_hash, probe_operation_fit
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.body_diagram.constants import TOOLBOX_KEY
from transflow.toolboxes.leaves.body_diagram.judge import (
    judge_diagram_plan,
    required_literal_preserved,
)
from transflow.toolboxes.leaves.body_diagram.layout import (
    local_flow_chains,
    plan_diagram_layout,
)
from transflow.toolboxes.leaves.body_diagram.models import (
    DiagramContainer,
    DiagramLayoutPlan,
    DiagramTemplate,
)
from transflow.toolboxes.leaves.body_diagram.template import build_diagram_template
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


@dataclass(frozen=True, slots=True)
class _DiagramSnapshot:
    facts: ExtractedPageFacts
    template: DiagramTemplate


class DiagramToolbox:
    """Translate native diagram labels while preserving node and connector geometry."""

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
        source_pdf: Path,
    ) -> None:
        self._policy = policy
        self._font_path = font_path.resolve()
        self._source_pdf = source_pdf.resolve()
        self._descriptor = ToolboxDescriptor(
            TOOLBOX_KEY,
            TOOLBOX_KEY,
            TOOLBOX_CONTRACT_VERSION,
            TOOLBOX_KEY,
        )
        self._snapshots: dict[str, _DiagramSnapshot] = {}
        self._facts_by_plan: dict[str, ExtractedPageFacts] = {}
        self._templates_by_plan: dict[str, DiagramTemplate] = {}
        self._containers_by_plan: dict[str, dict[str, DiagramContainer]] = {}
        self._rule_trace_by_plan: dict[str, tuple[dict[str, object], ...]] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        return self._descriptor

    def rule_trace(self, plan_id: str) -> tuple[dict[str, object], ...]:
        return self._rule_trace_by_plan.get(plan_id, ())

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """Recover total text ownership and freeze native diagram topology."""

        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "diagram 页面事实漂移",
            )
        if _sha256_file(self._source_pdf) != context.source_hash:
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "diagram 源 PDF 与执行上下文不一致",
            )
        diagram_template = build_diagram_template(facts, self._source_pdf)
        inventory_by_id = {
            item.object_id: item
            for item in freeze_page_text_inventory(
                facts,
                target_language=self._policy.target_language,
            ).items
        }
        diagram_template = _promote_translatable_margin_text(
            diagram_template,
            facts,
            inventory_by_id,
            self._policy.target_language,
        )
        requested = tuple(
            projected
            for container in diagram_template.containers
            if (
                projected := _translation_projection(
                    container,
                    facts,
                    inventory_by_id,
                    self._policy.target_language,
                )
            )
            is not None
        )
        requested_by_id = {container.container_id: container for container in requested}
        projected_object_ids = {
            object_id
            for container in requested
            for object_id in (
                *container.source_object_ids,
                *container.recomposed_object_ids,
            )
        }
        newly_protected_ids = tuple(
            object_id
            for container in diagram_template.containers
            for object_id in container.source_object_ids
            if object_id not in projected_object_ids
        )
        execution_template = replace(
            diagram_template,
            mode="translated" if requested else "passthrough",
            nodes=tuple(
                replace(
                    node,
                    container_ids=tuple(
                        container_id
                        for container_id in node.container_ids
                        if container_id in requested_by_id
                    ),
                )
                for node in diagram_template.nodes
            ),
            containers=requested,
            protected_object_ids=tuple(
                dict.fromkeys(
                    (
                        *diagram_template.protected_object_ids,
                        *newly_protected_ids,
                    )
                )
            ),
            structure_sha256=content_sha256(
                {
                    "source_structure_sha256": diagram_template.structure_sha256,
                    "translation_projection": requested,
                }
            ),
        )
        execution_template = _expand_local_flow_corridors(execution_template)
        execution_template = _constrain_protected_image_boundaries(
            execution_template,
            facts,
        )
        template_id = f"body-diagram-{facts.page_identity[:24]}"
        self._snapshots[template_id] = _DiagramSnapshot(
            facts,
            execution_template,
        )
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=TOOLBOX_KEY,
            object_ids=tuple(container.container_id for container in execution_template.containers),
        )

    def build_translation_request(
        self,
        template: PageTemplate,
    ) -> TranslationBatch | None:
        """Build one stable page-level batch in diagram reading order."""

        snapshot = self._snapshots[template.template_id]
        if not snapshot.template.containers:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    (
                        f"{snapshot.facts.page_identity}\0{container.container_id}\0{TOOLBOX_KEY}"
                    ).encode("ascii")
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=ordinal,
                source_text=container.source_text,
                region_id=(
                    f"body-diagram-p{template.context.page_no:04d}-{container.container_id}"
                ),
                source_object_ids=container.source_object_ids,
                inline_keep_source_object_ids=container.recomposed_object_ids,
            )
            for ordinal, container in enumerate(snapshot.template.containers)
        )
        return TranslationBatch(
            batch_id=(
                f"batch-{template.context.run_id}-p{template.context.page_no:04d}-{TOOLBOX_KEY}"
            ),
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=units,
        )

    def build_diagnostic_patch(
        self,
        template: PageTemplate,
        batch: TranslationBatch,
        bundle: TranslationBundle,
    ) -> tuple[PagePatch, tuple[dict[str, object], ...]]:
        """Materialize rejected non-empty translations for owner review."""

        snapshot = self._snapshots[template.template_id]
        translated = _translated_by_container(snapshot, batch, bundle)
        layout, findings = plan_diagram_layout(
            snapshot.template,
            translated,
            font_file=str(self._font_path),
        )
        records = tuple(
            {
                "container_id": placement.container_id,
                "owner_kind": placement.owner_kind,
                "owner_id": placement.owner_id,
                "node_id": placement.node_id,
                "operation_type": "translated_diagnostic_render",
                "output_bbox": placement.output_bbox,
                "font_size": placement.font_size,
                "line_height": placement.line_height,
                "fit": placement.fit,
                "product_acceptance": False,
            }
            for placement in layout.placements
        )
        if findings:
            records = (
                *records,
                {
                    "operation_type": "diagnostic_findings",
                    "codes": tuple(item.code for item in findings),
                    "product_acceptance": False,
                },
            )
        return (
            _build_page_patch(
                snapshot,
                template,
                batch,
                layout,
                self._policy,
            ),
            records,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """Validate semantic output, plan owner-safe layout, and build PagePatch."""

        snapshot = self._snapshots[template.template_id]
        plan_id = f"plan-{template.template_id}"
        self._facts_by_plan[plan_id] = snapshot.facts
        self._templates_by_plan[plan_id] = snapshot.template
        self._containers_by_plan[plan_id] = {
            item.container_id: item for item in snapshot.template.containers
        }
        if dispatch.failure is not None:
            finding = Finding(
                f"{plan_id}-translation-failure",
                dispatch.failure.code,
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(
                plan_id,
                TOOLBOX_KEY,
                None,
                (finding,),
                True,
            )
        if dispatch.skip_reason is not None:
            return ToolboxLayoutPlan(
                plan_id,
                TOOLBOX_KEY,
                None,
                (),
                False,
                True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "diagram 缺少翻译结果",
            )
        translated = _translated_by_container(
            snapshot,
            dispatch.batch,
            dispatch.bundle,
        )
        validation_findings = _validate_translations(
            plan_id,
            snapshot.template.containers,
            translated,
        )
        layout, private_findings = plan_diagram_layout(
            snapshot.template,
            translated,
            font_file=str(self._font_path),
        )
        findings = [
            Finding(
                f"{plan_id}-layout-{index:03d}",
                item.code,
                item.severity,
                tuple(value for value in (item.container_id, item.node_id) if value is not None),
            )
            for index, item in enumerate(private_findings)
        ]
        findings.extend(validation_findings)
        findings.extend(judge_diagram_plan(plan_id, snapshot.template, layout))
        patch = _build_page_patch(
            snapshot,
            template,
            dispatch.batch,
            layout,
            self._policy,
        )
        self._rule_trace_by_plan[plan_id] = _layout_rule_trace(
            snapshot.template,
            layout,
        )
        return ToolboxLayoutPlan(
            plan_id,
            TOOLBOX_KEY,
            patch,
            _deduplicate_findings(tuple(findings)),
        )

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """Probe the exact production Patch without writing the source PDF."""

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
                if overflow_ids:
                    candidate_plan = replace(
                        plan,
                        findings=_deduplicate_findings(
                            (
                                *plan.findings,
                                Finding(
                                    f"{plan.plan_id}-probe-overflow-r{repair_round}",
                                    "TEXT_LAYOUT_OVERFLOW",
                                    "HARD",
                                    overflow_ids,
                                ),
                            )
                        ),
                    )
            except (
                DomainContractError,
                PortCallError,
                ValueError,
                RuntimeError,
            ) as error:
                candidate_plan = ToolboxLayoutPlan(
                    plan.plan_id,
                    TOOLBOX_KEY,
                    None,
                    _deduplicate_findings(
                        (
                            *plan.findings,
                            Finding(
                                f"{plan.plan_id}-render-failed-r{repair_round}",
                                "DIAGRAM_RENDER_CAPABILITY_FAILED",
                                "HARD",
                                (type(error).__name__,),
                            ),
                        )
                    ),
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
                    "topology": self._templates_by_plan[plan.plan_id].topology_sha256,
                }
            ),
            repair_round=repair_round,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        findings = candidate.plan.findings
        repairable = {
            "DIAGRAM_NODE_TEXT_UNFIT",
            "TEXT_LAYOUT_OVERFLOW",
        }
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = "DIAGRAM_PLAN_FALLBACK"
        elif any(item.code in repairable for item in findings):
            disposition = (
                DecisionDisposition.REPAIR
                if candidate.repair_round < self._policy.repair_limit
                else DecisionDisposition.FALLBACK
            )
            reason = (
                "DIAGRAM_REPAIR_REQUIRED"
                if disposition is DecisionDisposition.REPAIR
                else "DIAGRAM_REPAIR_EXHAUSTED"
            )
        elif any(item.severity == "HARD" for item in findings):
            disposition = DecisionDisposition.FALLBACK
            reason = "DIAGRAM_HARD_FINDING"
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "DIAGRAM_PASSTHROUGH_ACCEPTED"
                if candidate.plan.passthrough_requested
                else "DIAGRAM_PATCH_ACCEPTED"
            )
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
        """Apply one local 10% typography repair without moving the owner anchor."""

        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        repair_ids = {
            evidence_id
            for finding in candidate.plan.findings
            if finding.code
            in {
                "DIAGRAM_NODE_TEXT_UNFIT",
                "TEXT_LAYOUT_OVERFLOW",
            }
            for evidence_id in finding.evidence_ids
        }
        repaired_operations: list[PatchOperation] = []
        repaired_any = False
        for operation in patch.operations:
            if not _operation_needs_repair(operation, repair_ids):
                repaired_operations.append(operation)
                continue
            current_font_size = operation.font_size or self._policy.minimum_font_size
            repaired = replace(
                operation,
                font_size=_next_repair_font_size(current_font_size),
                line_height=max(0.9, round((operation.line_height or 1.0) * 0.9, 2)),
            )
            repaired_operations.append(
                replace(
                    repaired,
                    payload_hash=patch_operation_hash(
                        owner=TOOLBOX_KEY,
                        target_object_ids=repaired.target_object_ids,
                        rect=repaired.rect,
                        replacement_text=repaired.replacement_text or " ",
                        font_id=repaired.font_id or self._policy.font_id,
                        font_size=repaired.font_size,
                        redaction_rects=repaired.redaction_rects,
                        color_srgb=repaired.color_srgb,
                        line_height=repaired.line_height,
                        preserve_drawing_overlap=True,
                        text_align=repaired.text_align,
                        rotation=repaired.rotation,
                    ),
                )
            )
            repaired_any = True
        if not repaired_any:
            return candidate
        repaired_plan = replace(
            candidate.plan,
            patch=replace(patch, operations=tuple(repaired_operations)),
            findings=tuple(
                finding
                for finding in candidate.plan.findings
                if finding.code
                not in {
                    "DIAGRAM_NODE_TEXT_UNFIT",
                    "TEXT_LAYOUT_OVERFLOW",
                }
            ),
        )
        return self._candidate(
            repaired_plan,
            self._facts_by_plan[candidate.plan.plan_id],
            candidate.repair_round + 1,
        )


def _translated_by_container(
    snapshot: _DiagramSnapshot,
    batch: TranslationBatch,
    bundle: TranslationBundle,
) -> dict[str, str]:
    if batch.ordered_unit_ids != bundle.requested_unit_ids or len(batch.units) != len(
        snapshot.template.containers
    ):
        raise DomainContractError(
            ErrorCode.INVALID_TRANSLATION_BUNDLE,
            "diagram 翻译身份漂移",
        )
    translated_by_unit = {item.unit_id: item.translated_text.strip() for item in bundle.units}
    return {
        container.container_id: translated_by_unit[unit.unit_id]
        for unit, container in zip(
            batch.units,
            snapshot.template.containers,
            strict=True,
        )
    }


def _translation_projection(
    container: DiagramContainer,
    facts: ExtractedPageFacts,
    inventory_by_id: dict[str, PageTextInventoryItem],
    target_language: str,
) -> DiagramContainer | None:
    """Keep Kernel-approved literals in place and translate only semantic spans."""

    translatable_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory_by_id[object_id].disposition is InventoryDisposition.TRANSLATE
    )
    if not translatable_ids:
        return None
    text_by_id = {item.object_id: item for item in facts.text_spans}
    recomposed_ids = _inline_recomposition_ids(
        container,
        text_by_id,
        inventory_by_id,
    )
    composition_ids = tuple(
        object_id
        for object_id in _reading_order_ids(container.source_object_ids, text_by_id)
        if object_id in translatable_ids or object_id in recomposed_ids
    )
    source_text = (
        text_by_id[composition_ids[0]].text
        if len(composition_ids) == 1 and not recomposed_ids
        else _join_projected_text(
            tuple(text_by_id[object_id].text for object_id in composition_ids)
        )
    )
    if not _requires_translation(source_text, target_language):
        return None
    if composition_ids == container.source_object_ids:
        return replace(
            container,
            source_object_ids=translatable_ids,
            source_text=source_text,
            required_literals=tuple(
                literal for literal in container.required_literals if literal in source_text
            ),
            recomposed_object_ids=recomposed_ids,
        )
    source_bbox = _union_rect(
        tuple(text_by_id[object_id].bbox for object_id in composition_ids)
    )
    kept_bboxes = tuple(
        text_by_id[object_id].bbox
        for object_id in container.source_object_ids
        if object_id not in composition_ids
    )
    projected_allowed_bbox = _clip_around_kept_text(
        container.allowed_bbox,
        source_bbox,
        kept_bboxes,
    )
    return replace(
        container,
        source_object_ids=translatable_ids,
        source_text=source_text,
        source_bbox=_source_anchor_bbox(
            source_bbox,
            projected_allowed_bbox,
        ),
        allowed_bbox=projected_allowed_bbox,
        required_literals=tuple(
            literal for literal in container.required_literals if literal in source_text
        ),
        recomposed_object_ids=recomposed_ids,
    )


def _inline_recomposition_ids(
    container: DiagramContainer,
    text_by_id: dict[str, KernelTextFact],
    inventory_by_id: dict[str, PageTextInventoryItem],
) -> tuple[str, ...]:
    keep_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory_by_id[object_id].disposition is InventoryDisposition.KEEP_SOURCE
    )
    if not keep_ids:
        return ()
    ordered_ids = _reading_order_ids(container.source_object_ids, text_by_id)
    leading_id = ordered_ids[0]
    return tuple(
        object_id
        for object_id in ordered_ids
        if object_id in keep_ids
        and not (
            object_id == leading_id
            and _is_mechanical_leading_marker(text_by_id[object_id].text)
        )
    )


def _reading_order_ids(
    object_ids: tuple[str, ...],
    text_by_id: dict[str, KernelTextFact],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            object_ids,
            key=lambda object_id: (
                text_by_id[object_id].block_index,
                text_by_id[object_id].line_index,
                text_by_id[object_id].span_index,
                text_by_id[object_id].bbox[0],
            ),
        )
    )


def _is_mechanical_leading_marker(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.fullmatch(
            r"(?:[•●▪◦]|[(（][A-Za-z0-9IVXLCDMivxlcdm]{1,4}[)）]"
            r"|[A-Za-z0-9IVXLCDMivxlcdm]{1,4}[.、．:：-])",
            stripped,
        )
    )


def _promote_translatable_margin_text(
    template: DiagramTemplate,
    facts: ExtractedPageFacts,
    inventory_by_id: dict[str, PageTextInventoryItem],
    target_language: str,
) -> DiagramTemplate:
    """Give spike-protected header/footer semantics a temporary shared owner."""

    protected_ids = set(template.protected_object_ids)
    margin_spans = tuple(
        span
        for span in facts.text_spans
        if span.object_id in protected_ids
        and inventory_by_id[span.object_id].disposition is InventoryDisposition.TRANSLATE
        and _requires_translation(span.text, target_language)
        and (
            span.bbox[3] <= template.height * 0.08
            or span.bbox[1] >= template.height * 0.92
        )
    )
    if not margin_spans:
        return template

    promoted = tuple(
        _margin_container(span, facts, template.width, template.height, index)
        for index, span in enumerate(
            sorted(margin_spans, key=lambda item: (item.bbox[1], item.bbox[0]))
        )
    )
    promoted_ids = {span.object_id for span in margin_spans}
    containers = tuple(
        replace(container, reading_order=index)
        for index, container in enumerate(
            sorted(
                (*template.containers, *promoted),
                key=lambda item: (
                    item.source_bbox[1],
                    item.source_bbox[0],
                    item.container_id,
                ),
            )
        )
    )
    remaining_protected = tuple(
        object_id
        for object_id in template.protected_object_ids
        if object_id not in promoted_ids
    )
    return replace(
        template,
        containers=containers,
        protected_object_ids=remaining_protected,
        structure_sha256=content_sha256(
            {
                "source_structure_sha256": template.structure_sha256,
                "shared_margin_projection": promoted,
            }
        ),
    )


def _expand_local_flow_corridors(template: DiagramTemplate) -> DiagramTemplate:
    """Let consecutive title/paragraph owners share vertical whitespace and reflow."""

    chains = local_flow_chains(template)
    if not chains:
        return template

    top_by_id: dict[str, float] = {}
    bottom_by_id: dict[str, float] = {}
    for chain in chains:
        corridor_top = min(container.allowed_bbox[1] for container in chain)
        corridor_bottom = max(container.allowed_bbox[3] for container in chain)
        for container in chain:
            top_by_id[container.container_id] = corridor_top
            bottom_by_id[container.container_id] = corridor_bottom
    containers = tuple(
        replace(
            container,
            allowed_bbox=(
                container.allowed_bbox[0],
                top_by_id[container.container_id],
                container.allowed_bbox[2],
                bottom_by_id[container.container_id],
            ),
        )
        if container.container_id in bottom_by_id
        else container
        for container in template.containers
    )
    return replace(
        template,
        containers=containers,
        structure_sha256=content_sha256(
            {
                "source_structure_sha256": template.structure_sha256,
                "local_flow_corridors": tuple(
                    tuple(container.container_id for container in chain)
                    for chain in chains
                ),
                "containers": containers,
            }
        ),
    )


def _constrain_protected_image_boundaries(
    template: DiagramTemplate,
    facts: ExtractedPageFacts,
) -> DiagramTemplate:
    """Keep label expansion inside its source image owner or outside image obstacles."""

    page_area = max(template.width * template.height, 1.0)
    image_bboxes = tuple(
        dict.fromkeys(
            image.bbox
            for image in facts.image_objects
            if _rect_area(image.bbox) / page_area < 0.45
        )
    )
    if not image_bboxes:
        return template

    containers: list[DiagramContainer] = []
    for container in template.containers:
        source_area = max(_rect_area(container.source_bbox), 1.0)
        underlays = tuple(
            image
            for image in image_bboxes
            if _intersection_area(container.source_bbox, image) / source_area >= 0.80
        )
        if underlays:
            owner = min(underlays, key=_rect_area)
            allowed = (
                max(
                    container.allowed_bbox[0],
                    min(owner[0], container.source_bbox[0]),
                ),
                max(
                    container.allowed_bbox[1],
                    min(owner[1], container.source_bbox[1]),
                ),
                min(
                    container.allowed_bbox[2],
                    max(owner[2], container.source_bbox[2]),
                ),
                min(
                    container.allowed_bbox[3],
                    max(owner[3], container.source_bbox[3]),
                ),
            )
            source_bbox = container.source_bbox
        else:
            allowed = _clip_around_kept_text(
                container.allowed_bbox,
                container.source_bbox,
                image_bboxes,
            )
            source_bbox = _source_anchor_bbox(container.source_bbox, allowed)
        containers.append(
            replace(
                container,
                source_bbox=source_bbox,
                allowed_bbox=tuple(round(value, 4) for value in allowed),
            )
        )

    constrained = tuple(containers)
    return replace(
        template,
        containers=constrained,
        structure_sha256=content_sha256(
            {
                "source_structure_sha256": template.structure_sha256,
                "protected_image_boundaries": image_bboxes,
                "containers": constrained,
            }
        ),
    )


def _margin_container(
    span: KernelTextFact,
    facts: ExtractedPageFacts,
    page_width: float,
    page_height: float,
    index: int,
) -> DiagramContainer:
    source_bbox = span.bbox
    alignment = _margin_alignment(source_bbox, page_width)
    allowed_bbox = _margin_allowed_bbox(
        source_bbox,
        facts,
        page_width,
        page_height,
        alignment,
        span.object_id,
        span.font_size,
    )
    band = "header" if source_bbox[3] <= page_height * 0.08 else "footer"
    owner_id = f"shared-margin-{band}-{index:03d}"
    return DiagramContainer(
        container_id=f"{owner_id}/text-00",
        owner_kind="shared_margin",
        owner_id=owner_id,
        node_id=None,
        source_object_ids=(span.object_id,),
        source_text=span.text.strip(),
        source_bbox=source_bbox,
        allowed_bbox=allowed_bbox,
        reading_order=0,
        required_literals=(),
        role=f"margin_{band}",
        font_name=span.font_name,
        font_size=span.font_size,
        color_srgb=span.color_srgb,
        alignment=alignment,
    )


def _margin_alignment(
    source_bbox: tuple[float, float, float, float],
    page_width: float,
) -> str:
    if source_bbox[0] <= page_width * 0.33:
        return "LEFT"
    if source_bbox[2] >= page_width * 0.67:
        return "RIGHT"
    return "CENTER"


def _margin_allowed_bbox(
    source_bbox: tuple[float, float, float, float],
    facts: ExtractedPageFacts,
    page_width: float,
    page_height: float,
    alignment: str,
    source_object_id: str,
    font_size: float,
) -> tuple[float, float, float, float]:
    inset = max(8.0, page_width * 0.03)
    left_limit = inset
    right_limit = page_width - inset
    source_center_y = (source_bbox[1] + source_bbox[3]) / 2.0
    peers = tuple(
        item
        for item in facts.text_spans
        if item.object_id != source_object_id
        and abs((item.bbox[1] + item.bbox[3]) / 2.0 - source_center_y)
        <= max(font_size, item.font_size)
    )
    left_blockers = tuple(item.bbox[2] for item in peers if item.bbox[2] <= source_bbox[0])
    right_blockers = tuple(item.bbox[0] for item in peers if item.bbox[0] >= source_bbox[2])
    if left_blockers:
        left_limit = max(left_limit, max(left_blockers) + 1.0)
    if right_blockers:
        right_limit = min(right_limit, min(right_blockers) - 1.0)

    if alignment == "LEFT":
        left = source_bbox[0]
        right = max(source_bbox[2], right_limit)
    elif alignment == "RIGHT":
        left = min(source_bbox[0], left_limit)
        right = source_bbox[2]
    else:
        center = (source_bbox[0] + source_bbox[2]) / 2.0
        half_width = max(
            (source_bbox[2] - source_bbox[0]) / 2.0,
            min(center - left_limit, right_limit - center),
        )
        left = center - half_width
        right = center + half_width
    bottom = min(page_height - 0.5, source_bbox[3] + max(1.5, font_size * 0.6))
    return tuple(
        round(value, 4)
        for value in (
            max(0.0, left),
            max(0.0, source_bbox[1] - 0.5),
            min(page_width, right),
            bottom,
        )
    )


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin and not has_cjk
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _join_projected_text(fragments: tuple[str, ...]) -> str:
    result = ""
    for fragment in fragments:
        text = fragment.strip()
        if not text:
            continue
        if result and not (
            re.search(r"[\u3400-\u9fff]", result) and re.search(r"[\u3400-\u9fff]", text)
        ):
            result += " "
        result += text
    return result


def _union_rect(
    rectangles: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    return tuple(
        round(value, 4)
        for value in (
            min(item[0] for item in rectangles),
            min(item[1] for item in rectangles),
            max(item[2] for item in rectangles),
            max(item[3] for item in rectangles),
        )
    )


def _clip_around_kept_text(
    allowed_bbox: tuple[float, float, float, float],
    source_bbox: tuple[float, float, float, float],
    kept_bboxes: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    candidate = allowed_bbox
    for kept in kept_bboxes:
        left, top, right, bottom = candidate
        if kept[2] <= left or kept[0] >= right or kept[3] <= top or kept[1] >= bottom:
            continue
        gap = 0.8
        options = (
            (left, top, min(right, kept[0] - gap), bottom),
            (max(left, kept[2] + gap), top, right, bottom),
            (left, top, right, min(bottom, kept[1] - gap)),
            (left, max(top, kept[3] + gap), right, bottom),
        )
        valid = tuple(
            option
            for option in options
            if option[2] > option[0] + 0.5 and option[3] > option[1] + 0.5
        )
        if not valid:
            return source_bbox
        candidate = max(
            valid,
            key=lambda option: (
                _intersection_area(option, source_bbox),
                _rect_area(option),
            ),
        )
    if candidate[2] <= candidate[0] or candidate[3] <= candidate[1]:
        return source_bbox
    return tuple(round(value, 4) for value in candidate)


def _source_anchor_bbox(
    source_bbox: tuple[float, float, float, float],
    allowed_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    intersection = (
        max(source_bbox[0], allowed_bbox[0]),
        max(source_bbox[1], allowed_bbox[1]),
        min(source_bbox[2], allowed_bbox[2]),
        min(source_bbox[3], allowed_bbox[3]),
    )
    if intersection[2] > intersection[0] + 0.1 and intersection[3] > intersection[1] + 0.1:
        return tuple(round(value, 4) for value in intersection)
    return allowed_bbox


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _rect_area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(
        0.0,
        rect[3] - rect[1],
    )


def _validate_translations(
    plan_id: str,
    containers: tuple[DiagramContainer, ...],
    translated: dict[str, str],
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for index, container in enumerate(containers):
        text = translated.get(container.container_id, "").strip()
        if not text:
            findings.append(
                Finding(
                    f"{plan_id}-empty-{index:03d}",
                    "DIAGRAM_TRANSLATION_EMPTY",
                    "HARD",
                    (container.container_id,),
                )
            )
            continue
        missing = tuple(
            literal
            for literal in container.required_literals
            if not required_literal_preserved(text, literal)
        )
        if missing:
            findings.append(
                Finding(
                    f"{plan_id}-literal-{index:03d}",
                    "DIAGRAM_REQUIRED_LITERAL_MISSING",
                    "SOFT",
                    (container.container_id, *missing),
                )
            )
    return tuple(findings)


def _operation_needs_repair(
    operation: PatchOperation,
    repair_ids: set[str],
) -> bool:
    if operation.operation_id in repair_ids:
        return True
    if set(operation.target_object_ids) & repair_ids:
        return True
    return any(
        operation.region_id.endswith(f"-{evidence_id}")
        for evidence_id in repair_ids
    )


def _next_repair_font_size(font_size: float) -> float:
    return max(1.0, round(font_size * 0.9, 2))


def _build_page_patch(
    snapshot: _DiagramSnapshot,
    template: PageTemplate,
    batch: TranslationBatch,
    layout: DiagramLayoutPlan,
    policy: P8ToolboxPolicy,
) -> PagePatch:
    source_bbox_by_id = {item.object_id: item.bbox for item in snapshot.facts.text_spans}
    container_by_id = {
        container.container_id: container
        for container in snapshot.template.containers
    }
    unit_by_container = {
        container.container_id: unit
        for unit, container in zip(
            batch.units,
            snapshot.template.containers,
            strict=True,
        )
    }
    operations: list[PatchOperation] = []
    for placement in layout.placements:
        unit = unit_by_container[placement.container_id]
        container = container_by_id[placement.container_id]
        target_id_set = {
            *unit.source_object_ids,
            *container.recomposed_object_ids,
        }
        semantic_target_object_ids = tuple(
            item.object_id
            for item in snapshot.facts.text_spans
            if item.object_id in target_id_set
        )
        target_object_ids = _patch_target_object_ids(
            semantic_target_object_ids,
            source_bbox_by_id,
        )
        redaction_rects = tuple(source_bbox_by_id[object_id] for object_id in target_object_ids)
        operation_id = f"op-{unit.unit_id[:20]}"
        payload_hash = patch_operation_hash(
            owner=TOOLBOX_KEY,
            target_object_ids=target_object_ids,
            rect=placement.output_bbox,
            replacement_text=placement.translated_text,
            font_id=policy.font_id,
            font_size=placement.font_size,
            redaction_rects=redaction_rects,
            color_srgb=placement.color_srgb,
            line_height=placement.line_height,
            preserve_drawing_overlap=True,
            text_align=placement.alignment,
            rotation=0,
        )
        operations.append(
            PatchOperation(
                operation_id=operation_id,
                region_id=unit.region_id,
                kind="replace_text",
                payload_hash=payload_hash,
                owner=TOOLBOX_KEY,
                target_object_ids=target_object_ids,
                rect=placement.output_bbox,
                replacement_text=placement.translated_text,
                font_id=policy.font_id,
                font_size=placement.font_size,
                redaction_rects=redaction_rects,
                color_srgb=placement.color_srgb,
                line_height=placement.line_height,
                preserve_drawing_overlap=True,
                text_align=placement.alignment,
                rotation=0,
            )
        )
    return PagePatch(
        patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{TOOLBOX_KEY}",
        source_hash=template.context.source_hash,
        page_no=template.context.page_no,
        geometry_hash=template.context.geometry_hash,
        owner=TOOLBOX_KEY,
        operations=tuple(operations),
    )


def _patch_target_object_ids(
    source_object_ids: tuple[str, ...],
    source_bbox_by_id: dict[str, tuple[float, float, float, float]],
) -> tuple[str, ...]:
    """Keep semantic aliases in the unit while emitting one erase per native box."""

    targets: list[str] = []
    seen_rects: set[tuple[float, float, float, float]] = set()
    for object_id in source_object_ids:
        bbox = source_bbox_by_id[object_id]
        if bbox in seen_rects:
            continue
        seen_rects.add(bbox)
        targets.append(object_id)
    return tuple(targets)


def _layout_rule_trace(
    template: DiagramTemplate,
    layout: DiagramLayoutPlan,
) -> tuple[dict[str, object], ...]:
    container_by_id = {container.container_id: container for container in template.containers}
    return tuple(
        {
            "scope": TOOLBOX_KEY,
            "rule": "owner_anchor_safe_box",
            "container_id": placement.container_id,
            "owner_kind": placement.owner_kind,
            "owner_id": placement.owner_id,
            "node_id": placement.node_id,
            "source_bbox": container_by_id[placement.container_id].source_bbox,
            "allowed_bbox": container_by_id[placement.container_id].allowed_bbox,
            "recomposed_keep_source_object_ids": (
                container_by_id[placement.container_id].recomposed_object_ids
            ),
            "output_bbox": placement.output_bbox,
            "alignment": placement.alignment,
            "font_size": placement.font_size,
            "line_height": placement.line_height,
            "fit_profile": placement.fit_profile,
            "fit": placement.fit,
        }
        for placement in layout.placements
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deduplicate_findings(
    findings: tuple[Finding, ...],
) -> tuple[Finding, ...]:
    result: list[Finding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for finding in findings:
        identity = (finding.code, finding.evidence_ids)
        if identity not in seen:
            seen.add(identity)
            result.append(finding)
    return tuple(result)
