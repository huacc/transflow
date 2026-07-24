"""Implement the independent six-stage production Toolbox for body.chart."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

import pymupdf

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
from transflow.pdf_kernel.facts import ExtractedPageFacts
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
from transflow.toolboxes.leaves.body_chart.judge import (
    judge_chart_plan,
    required_literal_preserved,
)
from transflow.toolboxes.leaves.body_chart.layout import (
    layout_rule_trace,
    materialize_translated_diagnostic_plan,
    plan_chart_layout,
)
from transflow.toolboxes.leaves.body_chart.models import (
    ChartLayoutPlan,
    ChartTemplate,
    ChartTextContainer,
)
from transflow.toolboxes.leaves.body_chart.template import build_chart_template
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

LOGGER = logging.getLogger("transflow.toolboxes.leaves.body_chart")
ROUTE = "body.chart"


@dataclass(frozen=True, slots=True)
class _ChartSnapshot:
    facts: ExtractedPageFacts
    template: ChartTemplate
    requested_containers: tuple[ChartTextContainer, ...]
    kept_numeric_prefix_by_container: dict[str, str]


class ChartToolbox:
    """Translate only located native semantic chart text and preserve visuals."""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        self._policy = policy
        self._font_path = font_path.resolve()
        self._descriptor = ToolboxDescriptor(
            ROUTE,
            ROUTE,
            TOOLBOX_CONTRACT_VERSION,
            ROUTE,
        )
        self._snapshots: dict[str, _ChartSnapshot] = {}
        self._facts_by_plan: dict[str, ExtractedPageFacts] = {}
        self._templates_by_plan: dict[str, ChartTemplate] = {}
        self._containers_by_plan: dict[str, dict[str, ChartTextContainer]] = {}
        self._rule_trace_by_plan: dict[str, tuple[dict[str, object], ...]] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        return self._descriptor

    def rule_trace(self, plan_id: str) -> tuple[dict[str, object], ...]:
        """Return the deterministic category-scoped layout decisions."""

        return self._rule_trace_by_plan.get(plan_id, ())

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """Freeze total chart-text ownership from read-only Kernel facts."""

        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "chart 页面事实漂移")
        chart_template = build_chart_template(facts)
        inventory_by_id = {
            item.object_id: item
            for item in freeze_page_text_inventory(
                facts,
                target_language=self._policy.target_language,
            ).items
        }
        kept_numeric_prefix_by_container = {
            container.container_id: prefix
            for container in chart_template.containers
            if (
                prefix := _kept_numeric_prefix_text(
                    container,
                    facts,
                    inventory_by_id,
                    self._policy.target_language,
                )
            )
            is not None
        }
        requested = tuple(
            projected
            for container in chart_template.containers
            if (
                projected := _translation_projection(
                    container,
                    facts,
                    inventory_by_id,
                    self._policy.target_language,
                )
            )
        )
        projected_by_id = {item.container_id: item for item in requested}
        removed_object_ids = tuple(
            object_id
            for container in chart_template.containers
            if container.container_id in projected_by_id
            for object_id in container.source_object_ids
            if object_id
            not in projected_by_id[container.container_id].source_object_ids
        )
        execution_template = replace(
            chart_template,
            containers=tuple(
                projected_by_id.get(container.container_id, container)
                for container in chart_template.containers
            ),
            protected_object_ids=tuple(
                dict.fromkeys(
                    (*chart_template.protected_object_ids, *removed_object_ids)
                )
            ),
            structure_hash=content_sha256(
                {
                    "source_structure_hash": chart_template.structure_hash,
                    "translation_projection": requested,
                }
            ),
        )
        template_id = f"body-chart-{facts.page_identity[:24]}"
        self._snapshots[template_id] = _ChartSnapshot(
            facts,
            execution_template,
            requested,
            kept_numeric_prefix_by_container,
        )
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=ROUTE,
            object_ids=tuple(item.semantic_object_id for item in requested),
        )

    def build_translation_request(
        self,
        template: PageTemplate,
    ) -> TranslationBatch | None:
        """Build one stable page-level batch in chart reading order."""

        snapshot = self._snapshots[template.template_id]
        if not snapshot.requested_containers:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    (
                        f"{snapshot.facts.page_identity}\0"
                        f"{container.container_id}\0body.chart"
                    ).encode("ascii")
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=ordinal,
                source_text=container.source_text,
                region_id=(
                    f"body-chart-p{template.context.page_no:04d}-"
                    f"{container.container_id}"
                ),
                source_object_ids=container.source_object_ids,
            )
            for ordinal, container in enumerate(snapshot.requested_containers)
        )
        return TranslationBatch(
            batch_id=(
                f"batch-{template.context.run_id}-"
                f"p{template.context.page_no:04d}-{ROUTE}"
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
    ) -> tuple[PagePatch | None, tuple[dict[str, object], ...]]:
        """Materialize a real rejected bundle without claiming product acceptance."""

        snapshot = self._snapshots[template.template_id]
        if (
            batch.ordered_unit_ids != bundle.requested_unit_ids
            or len(batch.units) != len(snapshot.requested_containers)
        ):
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "chart 诊断翻译身份漂移",
            )
        translated_by_unit = {
            item.unit_id: _normalize_translation(item.translated_text)
            for item in bundle.units
        }
        translated_by_container = {
            container.container_id: _normalize_kept_numeric_prefix_translation(
                translated_by_unit[unit.unit_id],
                snapshot.kept_numeric_prefix_by_container.get(
                    container.container_id
                ),
                self._policy.target_language,
            )
            for unit, container in zip(
                batch.units,
                snapshot.requested_containers,
                strict=True,
            )
        }
        layout, _ = plan_chart_layout(
            snapshot.template,
            translated_by_container,
            font_file=self._font_path,
            minimum_font_size=self._policy.minimum_font_size,
        )
        layout = _align_kept_numeric_prefix_rows(
            snapshot.template,
            layout,
            snapshot.kept_numeric_prefix_by_container,
        )
        diagnostic_template = _restore_diagnostic_source_geometry(snapshot)
        try:
            _, diagnostic_layout, repaired = materialize_translated_diagnostic_plan(
                diagnostic_template,
                layout,
            )
        except RuntimeError as error:
            return (
                None,
                (
                    {
                        "operation_type": "source_fallback",
                        "failure": str(error),
                        "product_acceptance": False,
                    },
                ),
            )
        repaired_by_id = {
            str(record["container_id"]): record for record in repaired
        }
        records = tuple(
            repaired_by_id.get(
                placement.container_id,
                {
                    "container_id": placement.container_id,
                    "operation_type": "translated_diagnostic_render",
                    "output_bbox": placement.output_bbox,
                    "font_size": placement.font_size,
                    "line_height": placement.line_height,
                    "profile": placement.profile,
                    "page_extended": False,
                    "product_acceptance": False,
                },
            )
            for placement in diagnostic_layout.placements
        )
        return (
            _build_page_patch(
                snapshot,
                template,
                batch,
                diagnostic_layout,
                self._policy,
            ),
            records,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """Validate semantic output and convert private placements to PagePatch."""

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
            return ToolboxLayoutPlan(plan_id, ROUTE, None, (finding,), True)
        if dispatch.skip_reason is not None:
            return ToolboxLayoutPlan(
                plan_id,
                ROUTE,
                None,
                (),
                False,
                True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "chart 缺少翻译结果",
            )

        translated_by_unit = {
            item.unit_id: _normalize_translation(item.translated_text)
            for item in dispatch.bundle.units
        }
        translated_by_container = {
            container.container_id: _normalize_kept_numeric_prefix_translation(
                translated_by_unit[unit.unit_id],
                snapshot.kept_numeric_prefix_by_container.get(
                    container.container_id
                ),
                self._policy.target_language,
            )
            for unit, container in zip(
                dispatch.batch.units,
                snapshot.requested_containers,
                strict=True,
            )
        }
        validation_findings = _validate_translations(
            plan_id,
            snapshot.requested_containers,
            translated_by_container,
            self._policy.target_language,
        )
        if validation_findings:
            return ToolboxLayoutPlan(
                plan_id,
                ROUTE,
                None,
                validation_findings,
                True,
            )

        layout, private_findings = plan_chart_layout(
            snapshot.template,
            translated_by_container,
            font_file=self._font_path,
            minimum_font_size=self._policy.minimum_font_size,
        )
        layout = _align_kept_numeric_prefix_rows(
            snapshot.template,
            layout,
            snapshot.kept_numeric_prefix_by_container,
        )
        self._rule_trace_by_plan[plan_id] = layout_rule_trace(
            snapshot.template,
            layout,
        )
        findings = [
            Finding(
                f"{plan_id}-layout-{index:03d}",
                item.code,
                item.severity,
                tuple(
                    value
                    for value in (item.container_id, item.association_id)
                    if value is not None
                ),
            )
            for index, item in enumerate(private_findings)
        ]
        findings.extend(
            judge_chart_plan(
                plan_id,
                snapshot.template,
                layout,
                snapshot.facts,
                self._policy.target_language,
            )
        )

        patch = _build_page_patch(
            snapshot,
            template,
            dispatch.batch,
            layout,
            self._policy,
        )
        return ToolboxLayoutPlan(
            plan_id,
            ROUTE,
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
            except (DomainContractError, PortCallError, ValueError, RuntimeError) as error:
                finding = Finding(
                    f"{plan.plan_id}-render-failed-r{repair_round}",
                    "CHART_RENDER_CAPABILITY_FAILED",
                    "HARD",
                    (type(error).__name__,),
                )
                candidate_plan = ToolboxLayoutPlan(
                    plan.plan_id,
                    ROUTE,
                    None,
                    _deduplicate_findings((*plan.findings, finding)),
                    True,
                )
        return ToolboxCandidate(
            candidate_id=f"candidate-{plan.plan_id}-{repair_round}",
            plan=candidate_plan,
            render_fingerprint=content_sha256(
                {
                    "facts": facts.kernel_facts_hash,
                    "findings": tuple(
                        item.code for item in candidate_plan.findings
                    ),
                    "plan_id": plan.plan_id,
                    "remainders": remainders,
                    "repair_round": repair_round,
                }
            ),
            repair_round=repair_round,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        findings = candidate.plan.findings
        repairable = {
            "CHART_TEXT_SLOT_OVERFLOW",
            "TEXT_LAYOUT_OVERFLOW",
        }
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = "CHART_PLAN_FALLBACK"
        elif any(item.code in repairable for item in findings):
            disposition = (
                DecisionDisposition.REPAIR
                if candidate.repair_round < self._policy.repair_limit
                else DecisionDisposition.FALLBACK
            )
            reason = (
                "CHART_REPAIR_REQUIRED"
                if disposition is DecisionDisposition.REPAIR
                else "CHART_REPAIR_EXHAUSTED"
            )
        elif any(item.severity == "HARD" for item in findings):
            disposition = DecisionDisposition.FALLBACK
            reason = "CHART_HARD_FINDING"
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "CHART_PASSTHROUGH_ACCEPTED"
                if candidate.plan.passthrough_requested
                else "CHART_PATCH_ACCEPTED"
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
        """Repair only the overflowing operations, then re-probe the page."""

        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        containers = self._containers_by_plan[candidate.plan.plan_id]
        overflow_operation_ids = {
            evidence_id
            for finding in candidate.plan.findings
            if finding.code == "TEXT_LAYOUT_OVERFLOW"
            for evidence_id in finding.evidence_ids
        }
        overflow_container_ids = {
            finding.evidence_ids[0]
            for finding in candidate.plan.findings
            if finding.code == "CHART_TEXT_SLOT_OVERFLOW"
            and finding.evidence_ids
        }
        repaired_operations: list[PatchOperation] = []
        repaired_any = False
        for operation in patch.operations:
            container_id = operation.region_id.rsplit("-", 3)[-1]
            container = containers.get(container_id)
            if container is None:
                container = next(
                    (
                        item
                        for item in containers.values()
                        if set(operation.target_object_ids)
                        <= set(item.source_object_ids)
                    ),
                    None,
                )
            if container is None:
                repaired_operations.append(operation)
                continue
            if (
                operation.operation_id not in overflow_operation_ids
                and container.container_id not in overflow_container_ids
            ):
                repaired_operations.append(operation)
                continue
            repaired = replace(
                operation,
                rect=container.allowed_bbox,
                font_size=self._policy.minimum_font_size,
                line_height=0.92,
            )
            repaired_any = True
            repaired_operations.append(
                replace(
                    repaired,
                    payload_hash=patch_operation_hash(
                        owner=ROUTE,
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
        if not repaired_any:
            return candidate
        repaired_plan = replace(
            candidate.plan,
            patch=replace(patch, operations=tuple(repaired_operations)),
            findings=tuple(
                item
                for item in candidate.plan.findings
                if item.code not in {"CHART_TEXT_SLOT_OVERFLOW", "TEXT_LAYOUT_OVERFLOW"}
            ),
        )
        return self._candidate(
            repaired_plan,
            self._facts_by_plan[candidate.plan.plan_id],
            candidate.repair_round + 1,
        )


def _build_page_patch(
    snapshot: _ChartSnapshot,
    template: PageTemplate,
    batch: TranslationBatch,
    layout: ChartLayoutPlan,
    policy: P8ToolboxPolicy,
) -> PagePatch:
    source_bbox_by_id = {
        item.object_id: item.bbox for item in snapshot.facts.text_spans
    }
    container_by_id = {
        item.container_id: item for item in snapshot.template.containers
    }
    unit_by_container = {
        container.container_id: unit
        for unit, container in zip(
            batch.units,
            snapshot.requested_containers,
            strict=True,
        )
    }
    operations: list[PatchOperation] = []
    for placement in layout.placements:
        container = container_by_id[placement.container_id]
        unit = unit_by_container[placement.container_id]
        target_object_ids = _patch_target_object_ids(
            container.source_object_ids,
            source_bbox_by_id,
        )
        redaction_rects = tuple(
            source_bbox_by_id[object_id]
            for object_id in target_object_ids
        )
        operation_id = f"op-{unit.unit_id[:20]}"
        payload_hash = patch_operation_hash(
            owner=ROUTE,
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
            rotation=placement.rotation,
        )
        operations.append(
            PatchOperation(
                operation_id=operation_id,
                region_id=unit.region_id,
                kind="replace_text",
                payload_hash=payload_hash,
                owner=ROUTE,
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
                rotation=placement.rotation,
            )
        )
    return PagePatch(
        patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{ROUTE}",
        source_hash=template.context.source_hash,
        page_no=template.context.page_no,
        geometry_hash=template.context.geometry_hash,
        owner=ROUTE,
        operations=tuple(operations),
    )


def _restore_diagnostic_source_geometry(
    snapshot: _ChartSnapshot,
) -> ChartTemplate:
    """Restore full geometry only for split formula annotations."""

    original_by_id = {
        container.container_id: container
        for container in build_chart_template(snapshot.facts).containers
    }
    return replace(
        snapshot.template,
        containers=tuple(
            replace(
                container,
                source_bbox=(
                    original_by_id[container.container_id].source_bbox
                    if _is_split_formula_annotation(
                        original_by_id[container.container_id],
                        container,
                    )
                    else container.source_bbox
                ),
                allowed_bbox=(
                    original_by_id[container.container_id].allowed_bbox
                    if _is_split_formula_annotation(
                        original_by_id[container.container_id],
                        container,
                    )
                    else container.allowed_bbox
                ),
            )
            for container in snapshot.template.containers
        ),
    )


def _is_split_formula_annotation(
    original: ChartTextContainer,
    projected: ChartTextContainer,
) -> bool:
    return (
        original.role == "ANNOTATION"
        and original.source_object_ids != projected.source_object_ids
        and bool(re.search(r"[=＝]", original.source_text))
        and bool(re.search(r"[%％]", original.source_text))
    )


def _align_kept_numeric_prefix_rows(
    template: ChartTemplate,
    layout: ChartLayoutPlan,
    kept_numeric_prefix_by_container: dict[str, str],
) -> ChartLayoutPlan:
    """Align a translated suffix with the preserved number on its source row."""

    if not kept_numeric_prefix_by_container:
        return layout
    containers = {
        container.container_id: container for container in template.containers
    }
    fonts: dict[str, pymupdf.Font] = {}
    placements = []
    for placement in layout.placements:
        if (
            not placement.fit
            or placement.container_id
            not in kept_numeric_prefix_by_container
        ):
            placements.append(placement)
            continue
        container = containers[placement.container_id]
        font = fonts.setdefault(
            placement.font_file,
            pymupdf.Font(fontfile=placement.font_file),
        )
        rendered_height = (
            float(font.ascender) - float(font.descender)
        ) * placement.font_size
        target_top = container.source_bbox[3] - rendered_height
        offset = target_top - placement.output_bbox[1]
        shifted = (
            placement.output_bbox[0],
            placement.output_bbox[1] + offset,
            placement.output_bbox[2],
            placement.output_bbox[3] + offset,
        )
        if (
            shifted[0] >= container.allowed_bbox[0] - 0.05
            and shifted[1] >= container.allowed_bbox[1] - 0.05
            and shifted[2] <= container.allowed_bbox[2] + 0.05
            and shifted[3] <= container.allowed_bbox[3] + 0.05
        ):
            placement = replace(
                placement,
                output_bbox=tuple(round(value, 4) for value in shifted),
            )
        placements.append(placement)
    return replace(layout, placements=tuple(placements))


def _normalize_translation(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()


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


def _validate_translations(
    plan_id: str,
    containers: tuple[ChartTextContainer, ...],
    translated: dict[str, str],
    target_language: str,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    normalized_outputs: dict[str, list[str]] = {}
    for container in containers:
        text = translated.get(container.container_id, "").strip()
        evidence = (container.container_id, container.association_id)
        code: str | None = None
        if not text or re.search(r"\[\[.+?\]\]|\{\{.+?\}\}|\bTODO\b", text):
            code = "TRANSLATION_PLACEHOLDER_OUTPUT"
        elif any(
            not required_literal_preserved(
                literal,
                text,
                target_language,
            )
            for literal in container.required_literals
        ):
            code = "TRANSLATION_REQUIRED_LITERAL_MISSING"
        elif target_language.casefold().startswith("zh"):
            semantic = text
            for literal in container.required_literals:
                semantic = semantic.replace(literal, "")
            if (
                re.search(r"[A-Za-z]", semantic)
                and not re.search(r"[\u3400-\u9fff]", semantic)
                and not _same_acronym(container.source_text, semantic)
            ):
                code = "TRANSLATION_SOURCE_LANGUAGE_RESIDUE"
        elif target_language.casefold().startswith("en") and re.search(
            r"[\u3400-\u9fff]",
            text,
        ):
            code = "TRANSLATION_SOURCE_LANGUAGE_RESIDUE"
        if code is not None:
            findings.append(
                Finding(
                    f"{plan_id}-{container.container_id}-{code.casefold()}",
                    code,
                    "HARD",
                    evidence,
                )
            )
        compact = re.sub(r"\s+", " ", text).strip().casefold()
        if len(compact) >= 40:
            normalized_outputs.setdefault(compact, []).append(container.container_id)
    for container_ids in normalized_outputs.values():
        if len(container_ids) < 2:
            continue
        findings.append(
            Finding(
                f"{plan_id}-cross-container-duplicate-{container_ids[0]}",
                "TRANSLATION_CROSS_CONTAINER_DUPLICATE",
                "HARD",
                tuple(container_ids),
            )
        )
    return tuple(findings)


def _same_acronym(source: str, translated: str) -> bool:
    left = re.fullmatch(r"([A-Z]{2,8})s?", source.strip())
    right = re.fullmatch(r"([A-Z]{2,8})s?", translated.strip())
    return bool(left and right and left.group(1) == right.group(1))


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin and not has_cjk
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _translation_projection(
    container: ChartTextContainer,
    facts: ExtractedPageFacts,
    inventory_by_id: dict[str, PageTextInventoryItem],
    target_language: str,
) -> ChartTextContainer | None:
    translatable_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory_by_id[object_id].disposition is InventoryDisposition.TRANSLATE
    )
    if not translatable_ids:
        return None
    text_by_id = {item.object_id: item for item in facts.text_spans}
    source_text = _join_projected_text(
        tuple(text_by_id[object_id].text for object_id in translatable_ids)
    )
    if not _requires_translation(source_text, target_language):
        return None
    if translatable_ids == container.source_object_ids:
        return replace(
            container,
            source_text=source_text,
            required_literals=tuple(
                literal
                for literal in container.required_literals
                if literal in source_text
            ),
        )

    source_bbox = _union_rect(
        tuple(text_by_id[object_id].bbox for object_id in translatable_ids)
    )
    excluded_bboxes = tuple(
        text_by_id[object_id].bbox
        for object_id in container.source_object_ids
        if object_id not in translatable_ids
    )
    projected_allowed_bbox = _clip_around_kept_text(
        container.allowed_bbox,
        source_bbox,
        excluded_bboxes,
    )
    word_gutter = _kept_numeric_prefix_word_gutter(
        container,
        source_bbox,
        translatable_ids,
        text_by_id,
        target_language,
    )
    if word_gutter:
        projected_allowed_bbox = (
            min(
                projected_allowed_bbox[2] - 0.1,
                max(
                    projected_allowed_bbox[0],
                    source_bbox[0] + word_gutter,
                ),
            ),
            min(
                projected_allowed_bbox[1],
                source_bbox[3] - container.font_size * 1.45,
            ),
            projected_allowed_bbox[2],
            projected_allowed_bbox[3],
        )
    return replace(
        container,
        source_object_ids=translatable_ids,
        semantic_object_id=translatable_ids[0],
        source_text=source_text,
        source_bbox=source_bbox,
        allowed_bbox=projected_allowed_bbox,
        required_literals=tuple(
            literal
            for literal in container.required_literals
            if literal in source_text
        ),
    )


def _kept_numeric_prefix_word_gutter(
    container: ChartTextContainer,
    source_bbox: tuple[float, float, float, float],
    translatable_ids: tuple[str, ...],
    text_by_id: dict[str, object],
    target_language: str,
) -> float:
    prefix = _kept_numeric_prefix_item(
        container,
        source_bbox,
        translatable_ids,
        text_by_id,
        target_language,
    )
    if prefix is not None:
        return max(1.5, container.font_size * 0.35)
    return 0.0


def _kept_numeric_prefix_text(
    container: ChartTextContainer,
    facts: ExtractedPageFacts,
    inventory_by_id: dict[str, PageTextInventoryItem],
    target_language: str,
) -> str | None:
    translatable_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory_by_id[object_id].disposition is InventoryDisposition.TRANSLATE
    )
    if not translatable_ids:
        return None
    text_by_id = {item.object_id: item for item in facts.text_spans}
    source_bbox = _union_rect(
        tuple(text_by_id[object_id].bbox for object_id in translatable_ids)
    )
    prefix = _kept_numeric_prefix_item(
        container,
        source_bbox,
        translatable_ids,
        text_by_id,
        target_language,
    )
    return None if prefix is None else prefix.text.strip()


def _kept_numeric_prefix_item(
    container: ChartTextContainer,
    source_bbox: tuple[float, float, float, float],
    translatable_ids: tuple[str, ...],
    text_by_id: dict[str, object],
    target_language: str,
) -> object | None:
    if (
        container.role
        not in {"TABLE_HEADER", "TABLE_SECTION", "TABLE_CELL", "TABLE_TOTAL"}
        or not target_language.casefold().startswith("en")
    ):
        return None
    translatable = set(translatable_ids)
    candidates: list[tuple[float, object]] = []
    for object_id in container.source_object_ids:
        if object_id in translatable:
            continue
        item = text_by_id[object_id]
        gap = source_bbox[0] - item.bbox[2]
        overlap = max(
            0.0,
            min(source_bbox[3], item.bbox[3])
            - max(source_bbox[1], item.bbox[1]),
        )
        minimum_height = min(
            source_bbox[3] - source_bbox[1],
            item.bbox[3] - item.bbox[1],
        )
        if (
            re.fullmatch(
                r"[-+]?\d+(?:[.,:/-]\d+)*%?",
                item.text.strip(),
            )
            and -0.2 <= gap <= container.font_size * 0.75
            and overlap >= minimum_height * 0.50
        ):
            candidates.append((gap, item))
    return None if not candidates else min(candidates, key=lambda item: item[0])[1]


def _normalize_kept_numeric_prefix_translation(
    translated_text: str,
    numeric_prefix: str | None,
    target_language: str,
) -> str:
    if numeric_prefix is None or not target_language.casefold().startswith("en"):
        return translated_text
    normalized = re.sub(r"\s+", " ", translated_text).strip()
    semantic_suffix = normalized.casefold().rstrip(".")
    if semantic_suffix in {"under", "below"}:
        return "or below"
    if semantic_suffix in {"over", "above"}:
        return "or above"
    if semantic_suffix in {"year", "years"}:
        return "years"
    return translated_text


def _join_projected_text(fragments: tuple[str, ...]) -> str:
    if len(fragments) == 1:
        return fragments[0]
    result = ""
    for fragment in fragments:
        text = fragment.strip()
        if not text:
            continue
        if result and not (
            re.search(r"[\u3400-\u9fff]", result)
            and re.search(r"[\u3400-\u9fff]", text)
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
    left, top, right, bottom = allowed_bbox
    for kept in kept_bboxes:
        if kept[2] <= source_bbox[0]:
            left = max(left, source_bbox[0])
        elif kept[0] >= source_bbox[2]:
            right = min(right, source_bbox[2])
        elif kept[3] <= source_bbox[1]:
            top = max(top, source_bbox[1])
        elif kept[1] >= source_bbox[3]:
            bottom = min(bottom, source_bbox[3])
        else:
            return source_bbox
    if right <= left or bottom <= top:
        return source_bbox
    return tuple(round(value, 4) for value in (left, top, right, bottom))


def _deduplicate_findings(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    by_identity: dict[tuple[str, tuple[str, ...]], Finding] = {}
    for item in findings:
        by_identity.setdefault((item.code, item.evidence_ids), item)
    return tuple(by_identity.values())
