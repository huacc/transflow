"""Dedicated TBM2 composite roots and the bounded freeform recovery leaf."""

from __future__ import annotations

import hashlib
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
from transflow.toolboxes.composites.models import (
    CompositeOwnership,
    OwnedContainer,
)
from transflow.toolboxes.composites.ownership import (
    OwnershipPlan,
    build_flow_text_chart_plan,
    build_flow_text_diagram_plan,
    build_freeform_plan,
)
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.body_chart.judge import judge_chart_plan
from transflow.toolboxes.leaves.body_chart.layout import plan_chart_layout
from transflow.toolboxes.leaves.body_diagram.judge import judge_diagram_plan
from transflow.toolboxes.leaves.body_diagram.layout import plan_diagram_layout
from transflow.toolboxes.leaves.body_flow_text_single.layout import plan_placements
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    SingleTextContainer,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


@dataclass(frozen=True, slots=True)
class _Snapshot:
    facts: ExtractedPageFacts
    plan: OwnershipPlan


@dataclass(frozen=True, slots=True)
class _OperationDraft:
    container: OwnedContainer
    rect: tuple[float, float, float, float]
    translated_text: str
    font_size: float
    line_height: float
    color_srgb: int
    alignment: str
    rotation: int = 0


class _CompositeRootToolbox:
    """Own one six-stage lifecycle; internal components are pure layout cores."""

    route: str

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        self._policy = policy
        self._font_path = font_path.resolve()
        self._descriptor = ToolboxDescriptor(
            self.route,
            self.route,
            TOOLBOX_CONTRACT_VERSION,
            self.route,
        )
        self._snapshots: dict[str, _Snapshot] = {}
        self._facts_by_plan: dict[str, ExtractedPageFacts] = {}
        self._last_template_id: str | None = None

    @property
    def descriptor(self) -> ToolboxDescriptor:
        return self._descriptor

    def ownership_audit(self) -> tuple[CompositeOwnership, ...]:
        """Return the latest immutable root ownership for evidence and tests."""

        if self._last_template_id is None:
            return ()
        plan = self._snapshots[self._last_template_id].plan
        return tuple(
            CompositeOwnership(
                object_id,
                container.component,
                container.composite_id,
            )
            for container in plan.containers
            for object_id in container.source_object_ids
        )

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "TBM2 composite page facts drifted from execution context",
            )
        ownership = self._build_ownership(facts)
        template_id = (
            f"{self.route.replace('.', '-')}-{facts.page_identity[:24]}"
        )
        self._snapshots[template_id] = _Snapshot(facts, ownership)
        self._last_template_id = template_id
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=self.route,
            object_ids=tuple(
                item.source_object_ids[0] for item in ownership.containers
            ),
        )

    def build_translation_request(
        self,
        template: PageTemplate,
    ) -> TranslationBatch | None:
        snapshot = self._snapshots[template.template_id]
        if not snapshot.plan.containers:
            return None
        return TranslationBatch(
            batch_id=(
                f"batch-{template.context.run_id}-"
                f"p{template.context.page_no:04d}-{self.route}"
            ),
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=tuple(
                TranslationUnit(
                    unit_id=_unit_id(
                        snapshot.facts.page_identity,
                        self.route,
                        item.composite_id,
                    ),
                    page_no=template.context.page_no,
                    ordinal=index,
                    source_text=item.source_text,
                    region_id=f"{self.route}/{item.composite_id}",
                    source_object_ids=item.source_object_ids,
                )
                for index, item in enumerate(snapshot.plan.containers)
            ),
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        snapshot = self._snapshots[template.template_id]
        plan_id = f"plan-{template.template_id}"
        self._facts_by_plan[plan_id] = snapshot.facts
        if dispatch.failure is not None:
            return _fallback_plan(
                plan_id,
                self.route,
                dispatch.failure.code,
                template.template_id,
            )
        if dispatch.skip_reason is not None:
            return ToolboxLayoutPlan(
                plan_id,
                self.route,
                None,
                (),
                False,
                True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "TBM2 composite translation dispatch is incomplete",
            )
        if snapshot.plan.force_fallback_reason is not None:
            return _fallback_plan(
                plan_id,
                self.route,
                snapshot.plan.force_fallback_reason,
                template.template_id,
            )

        translated_by_unit = {
            item.unit_id: item.translated_text.strip()
            for item in dispatch.bundle.units
        }
        translated = {
            container.composite_id: translated_by_unit[
                _unit_id(
                    snapshot.facts.page_identity,
                    self.route,
                    container.composite_id,
                )
            ]
            for container in snapshot.plan.containers
        }
        drafts, findings = self._layout(snapshot, translated, plan_id)
        if not drafts:
            return _fallback_plan(
                plan_id,
                self.route,
                "COMPOSITE_NO_SAFE_PATCH",
                template.template_id,
                findings,
            )
        duplicate_targets = _duplicate_target_ids(drafts)
        if duplicate_targets:
            return _fallback_plan(
                plan_id,
                self.route,
                "COMPOSITE_DUPLICATE_TARGET_OWNER",
                duplicate_targets[0],
                findings,
            )
        findings = (
            *findings,
            *_cross_component_findings(plan_id, drafts),
        )
        patch = _build_patch(
            self.route,
            template,
            snapshot.facts,
            drafts,
            self._policy,
        )
        return ToolboxLayoutPlan(
            plan_id,
            self.route,
            patch,
            _deduplicate_findings(findings),
            region_fallback_applied=bool(snapshot.plan.retained_ids),
        )

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        del context
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
                overflow = tuple(
                    operation.operation_id
                    for operation, remainder in zip(
                        plan.patch.operations,
                        remainders,
                        strict=True,
                    )
                    if remainder < 0
                )
                if overflow:
                    candidate_plan = replace(
                        plan,
                        findings=_deduplicate_findings(
                            (
                                *plan.findings,
                                Finding(
                                    f"{plan.plan_id}-probe-overflow-r{repair_round}",
                                    "TEXT_LAYOUT_OVERFLOW",
                                    "HARD",
                                    overflow,
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
                candidate_plan = replace(
                    plan,
                    findings=_deduplicate_findings(
                        (
                            *plan.findings,
                            Finding(
                                f"{plan.plan_id}-render-failed-r{repair_round}",
                                "COMPOSITE_RENDER_CAPABILITY_FAILED",
                                "HARD",
                                (type(error).__name__,),
                            ),
                        )
                    ),
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
                    "route": self.route,
                }
            ),
            repair_round=repair_round,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        findings = candidate.plan.findings
        repairable = {
            "CHART_LAYOUT_UNFIT",
            "DIAGRAM_NODE_TEXT_UNFIT",
            "FLOW_TEXT_UNFIT",
            "TEXT_LAYOUT_OVERFLOW",
        }
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = "COMPOSITE_ROOT_FALLBACK"
        elif any(
            item.severity == "HARD" and item.code not in repairable
            for item in findings
        ):
            disposition = DecisionDisposition.FALLBACK
            reason = "COMPOSITE_ROOT_HARD_FINDING"
        elif any(item.code in repairable for item in findings):
            disposition = (
                DecisionDisposition.REPAIR
                if candidate.repair_round < self._policy.repair_limit
                else DecisionDisposition.FALLBACK
            )
            reason = (
                "COMPOSITE_ROOT_REPAIR_REQUIRED"
                if disposition is DecisionDisposition.REPAIR
                else "COMPOSITE_ROOT_REPAIR_EXHAUSTED"
            )
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "COMPOSITE_ROOT_REGION_FALLBACK_ACCEPTED"
                if candidate.plan.region_fallback_applied
                else "COMPOSITE_ROOT_PATCH_ACCEPTED"
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
        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        repaired_operations = tuple(
            _shrink_operation(operation, self.route, self._policy)
            for operation in patch.operations
        )
        repairable = {
            "CHART_LAYOUT_UNFIT",
            "DIAGRAM_NODE_TEXT_UNFIT",
            "FLOW_TEXT_UNFIT",
            "TEXT_LAYOUT_OVERFLOW",
        }
        repaired_plan = replace(
            candidate.plan,
            patch=replace(patch, operations=repaired_operations),
            findings=tuple(
                item
                for item in candidate.plan.findings
                if item.code not in repairable
            ),
        )
        return self._candidate(
            repaired_plan,
            self._facts_by_plan[candidate.plan.plan_id],
            candidate.repair_round + 1,
        )

    def _layout(
        self,
        snapshot: _Snapshot,
        translated: dict[str, str],
        plan_id: str,
    ) -> tuple[tuple[_OperationDraft, ...], tuple[Finding, ...]]:
        owned = {
            (item.component, item.internal_id): item
            for item in snapshot.plan.containers
        }
        drafts: list[_OperationDraft] = []
        findings: list[Finding] = []

        if snapshot.plan.flow_containers:
            lanes = _flow_lanes(snapshot.plan.flow_containers)
            for lane_index, lane in enumerate(lanes):
                lane_bbox = _union_rect(
                    tuple(item.source_bbox for item in lane)
                )
                parallel_lanes = tuple(
                    candidate
                    for candidate in lanes[lane_index + 1 :]
                    if _vertical_overlap(
                        lane_bbox,
                        _union_rect(
                            tuple(item.source_bbox for item in candidate)
                        ),
                    )
                    > 0.5
                )
                right_limit = (
                    min(
                        item.source_bbox[0]
                        for candidate in parallel_lanes
                        for item in candidate
                    )
                    - 4.0
                    if parallel_lanes
                    else max(item.source_bbox[2] for item in lane)
                )
                planning_lane = tuple(
                    replace(
                        item,
                        source_bbox=(
                            item.source_bbox[0],
                            item.source_bbox[1],
                            max(
                                item.source_bbox[0] + 1.0,
                                min(item.source_bbox[2], right_limit),
                            ),
                            item.source_bbox[3],
                        ),
                    )
                    for item in lane
                )
                flow_translations = {
                    item.container_id: translated[f"flow/{item.container_id}"]
                    for item in lane
                }
                placements = plan_placements(
                    snapshot.facts,
                    planning_lane,
                    flow_translations,
                    self._policy,
                    self._font_path,
                )
                for placement in placements:
                    container = owned[("flow", placement.container_id)]
                    drafts.append(
                        _OperationDraft(
                            container,
                            placement.output_bbox,
                            placement.translated_text,
                            placement.font_size,
                            placement.line_height,
                            placement.color_srgb,
                            "LEFT",
                        )
                    )
                    if not placement.fit:
                        findings.append(
                            Finding(
                                f"{plan_id}-flow-{placement.container_id}-unfit",
                                "FLOW_TEXT_UNFIT",
                                "HARD",
                                (container.composite_id,),
                            )
                        )

        if snapshot.plan.chart_template is not None:
            chart_translations = {
                item.container_id: translated[f"chart/{item.container_id}"]
                for item in snapshot.plan.chart_template.containers
            }
            layout, private_findings = plan_chart_layout(
                snapshot.plan.chart_template,
                chart_translations,
                font_file=self._font_path,
                minimum_font_size=self._policy.minimum_font_size,
            )
            for placement in layout.placements:
                container = owned[("chart", placement.container_id)]
                drafts.append(
                    _OperationDraft(
                        container,
                        placement.output_bbox,
                        placement.translated_text,
                        placement.font_size,
                        placement.line_height,
                        placement.color_srgb,
                        placement.alignment,
                        placement.rotation,
                    )
                )
                if not placement.fit:
                    findings.append(
                        Finding(
                            f"{plan_id}-chart-{placement.container_id}-unfit",
                            "CHART_LAYOUT_UNFIT",
                            "HARD",
                            (container.composite_id,),
                        )
                    )
            findings.extend(
                Finding(
                    f"{plan_id}-chart-finding-{index:03d}",
                    item.code,
                    item.severity,
                    tuple(
                        value
                        for value in (item.container_id, item.association_id)
                        if value is not None
                    ),
                )
                for index, item in enumerate(private_findings)
            )
            findings.extend(
                judge_chart_plan(
                    plan_id,
                    snapshot.plan.chart_template,
                    layout,
                    snapshot.facts,
                    self._policy.target_language,
                )
            )

        if snapshot.plan.diagram_template is not None:
            diagram_translations = {
                item.container_id: translated[f"diagram/{item.container_id}"]
                for item in snapshot.plan.diagram_template.containers
            }
            layout, private_findings = plan_diagram_layout(
                snapshot.plan.diagram_template,
                diagram_translations,
                font_file=str(self._font_path),
            )
            for placement in layout.placements:
                container = owned[("diagram", placement.container_id)]
                drafts.append(
                    _OperationDraft(
                        container,
                        placement.output_bbox,
                        placement.translated_text,
                        placement.font_size,
                        placement.line_height,
                        placement.color_srgb,
                        placement.alignment,
                    )
                )
                if not placement.fit:
                    findings.append(
                        Finding(
                            f"{plan_id}-diagram-{placement.container_id}-unfit",
                            "DIAGRAM_NODE_TEXT_UNFIT",
                            "HARD",
                            (container.composite_id,),
                        )
                    )
            findings.extend(
                Finding(
                    f"{plan_id}-diagram-finding-{index:03d}",
                    item.code,
                    item.severity,
                    tuple(
                        value
                        for value in (item.container_id, item.node_id)
                        if value is not None
                    ),
                )
                for index, item in enumerate(private_findings)
            )
            findings.extend(
                judge_diagram_plan(
                    plan_id,
                    snapshot.plan.diagram_template,
                    layout,
                )
            )

        return tuple(drafts), _deduplicate_findings(tuple(findings))

    def _build_ownership(self, facts: ExtractedPageFacts) -> OwnershipPlan:
        raise NotImplementedError


class FlowTextChartToolbox(_CompositeRootToolbox):
    """Dedicated root for the fixed P17 flow-text/chart composite."""

    route = "body.composite.flow_text_chart"

    def _build_ownership(self, facts: ExtractedPageFacts) -> OwnershipPlan:
        return build_flow_text_chart_plan(facts, self._policy)


class FlowTextDiagramToolbox(_CompositeRootToolbox):
    """Dedicated root for the fixed P18 flow-text/diagram composite."""

    route = "body.composite.flow_text_diagram"

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
        source_pdf: Path,
    ) -> None:
        super().__init__(policy, font_path)
        self._source_pdf = source_pdf.resolve()

    def _build_ownership(self, facts: ExtractedPageFacts) -> OwnershipPlan:
        return build_flow_text_diagram_plan(
            facts,
            self._policy,
            self._source_pdf,
        )


class FreeformToolbox(_CompositeRootToolbox):
    """One-pass classification-failure recovery over a fixed ready allow-list."""

    route = "body.freeform"
    activation_reason = "CLASSIFICATION_FAILED"

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
        source_pdf: Path | None = None,
    ) -> None:
        super().__init__(policy, font_path)
        self._source_pdf = (
            source_pdf.resolve() if source_pdf is not None else None
        )

    def _build_ownership(self, facts: ExtractedPageFacts) -> OwnershipPlan:
        return build_freeform_plan(
            facts,
            self._policy,
            self._source_pdf,
        )


def _unit_id(page_identity: str, route: str, composite_id: str) -> str:
    return hashlib.sha256(
        f"{page_identity}\0{route}\0{composite_id}".encode()
    ).hexdigest()


def _build_patch(
    route: str,
    template: PageTemplate,
    facts: ExtractedPageFacts,
    drafts: tuple[_OperationDraft, ...],
    policy: P8ToolboxPolicy,
) -> PagePatch:
    bbox_by_id = {item.object_id: item.bbox for item in facts.text_spans}
    operations: list[PatchOperation] = []
    for draft in sorted(
        drafts,
        key=lambda item: item.container.reading_order,
    ):
        target_ids = _deduplicate_bbox_targets(
            draft.container.source_object_ids,
            bbox_by_id,
        )
        redaction_rects = tuple(bbox_by_id[item] for item in target_ids)
        unit_id = _unit_id(
            facts.page_identity,
            route,
            draft.container.composite_id,
        )
        operation = PatchOperation(
            operation_id=f"op-{unit_id[:20]}",
            region_id=f"{route}/{draft.container.composite_id}",
            kind="replace_text",
            payload_hash="0" * 64,
            owner=route,
            target_object_ids=target_ids,
            rect=draft.rect,
            replacement_text=draft.translated_text,
            font_id=policy.font_id,
            font_size=draft.font_size,
            redaction_rects=redaction_rects,
            color_srgb=draft.color_srgb,
            line_height=draft.line_height,
            preserve_drawing_overlap=(
                draft.container.component in {"chart", "diagram"}
            ),
            text_align=draft.alignment,
            rotation=draft.rotation,
        )
        operations.append(
            replace(
                operation,
                payload_hash=_operation_hash(operation, route, policy),
            )
        )
    return PagePatch(
        patch_id=f"patch-{facts.page_identity[:24]}-{route}",
        source_hash=template.context.source_hash,
        page_no=template.context.page_no,
        geometry_hash=template.context.geometry_hash,
        owner=route,
        operations=tuple(operations),
    )


def _operation_hash(
    operation: PatchOperation,
    route: str,
    policy: P8ToolboxPolicy,
) -> str:
    assert operation.rect is not None
    return patch_operation_hash(
        owner=route,
        target_object_ids=operation.target_object_ids,
        rect=operation.rect,
        replacement_text=operation.replacement_text or " ",
        font_id=operation.font_id or policy.font_id,
        font_size=operation.font_size or policy.minimum_font_size,
        redaction_rects=operation.redaction_rects,
        color_srgb=operation.color_srgb,
        line_height=operation.line_height,
        preserve_drawing_overlap=operation.preserve_drawing_overlap,
        text_align=operation.text_align,
        rotation=operation.rotation,
    )


def _shrink_operation(
    operation: PatchOperation,
    route: str,
    policy: P8ToolboxPolicy,
) -> PatchOperation:
    repaired = replace(
        operation,
        font_size=max(
            policy.minimum_font_size,
            round((operation.font_size or policy.minimum_font_size) * 0.85, 2),
        ),
        line_height=max(0.9, round((operation.line_height or 1.0) * 0.92, 2)),
    )
    return replace(
        repaired,
        payload_hash=_operation_hash(repaired, route, policy),
    )


def _fallback_plan(
    plan_id: str,
    route: str,
    code: str,
    evidence: str,
    findings: tuple[Finding, ...] = (),
) -> ToolboxLayoutPlan:
    finding = Finding(
        f"{plan_id}-root-fallback",
        code.replace(":", "_"),
        "HARD",
        (evidence,),
    )
    return ToolboxLayoutPlan(
        plan_id,
        route,
        None,
        _deduplicate_findings((*findings, finding)),
        True,
    )


def _duplicate_target_ids(
    drafts: tuple[_OperationDraft, ...],
) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for draft in drafts:
        for object_id in draft.container.source_object_ids:
            if object_id in seen:
                duplicates.append(object_id)
            seen.add(object_id)
    return tuple(dict.fromkeys(duplicates))


def _flow_lanes(
    containers: tuple[SingleTextContainer, ...],
) -> tuple[tuple[SingleTextContainer, ...], ...]:
    """Keep parallel source columns independent while reusing the single core."""

    lanes: list[list[SingleTextContainer]] = []
    for container in sorted(
        containers,
        key=lambda item: (
            item.source_bbox[0],
            item.source_bbox[1],
            item.reading_order,
        ),
    ):
        target = next(
            (
                lane
                for lane in lanes
                if abs(
                    container.source_bbox[0]
                    - sum(item.source_bbox[0] for item in lane) / len(lane)
                )
                <= 24.0
                and _horizontal_overlap_ratio(
                    container.source_bbox,
                    _union_rect(tuple(item.source_bbox for item in lane)),
                )
                >= 0.45
            ),
            None,
        )
        if target is None:
            lanes.append([container])
        else:
            target.append(container)
    return tuple(
        tuple(sorted(lane, key=lambda item: item.reading_order))
        for lane in lanes
    )


def _cross_component_findings(
    plan_id: str,
    drafts: tuple[_OperationDraft, ...],
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for index, left in enumerate(drafts):
        for right in drafts[index + 1 :]:
            if left.container.component == right.container.component:
                continue
            overlap = _intersection_area(left.rect, right.rect)
            smaller = min(_area(left.rect), _area(right.rect))
            if smaller > 0 and overlap / smaller >= 0.20:
                findings.append(
                    Finding(
                        (
                            f"{plan_id}-cross-owner-"
                            f"{left.container.reading_order:03d}-"
                            f"{right.container.reading_order:03d}"
                        ),
                        "COMPOSITE_CROSS_OWNER_OUTPUT_OVERLAP",
                        "HARD",
                        (
                            left.container.composite_id,
                            right.container.composite_id,
                        ),
                    )
                )
    return tuple(findings)


def _deduplicate_bbox_targets(
    object_ids: tuple[str, ...],
    bbox_by_id: dict[str, tuple[float, float, float, float]],
) -> tuple[str, ...]:
    targets: list[str] = []
    bboxes: set[tuple[float, float, float, float]] = set()
    for object_id in object_ids:
        bbox = bbox_by_id[object_id]
        if bbox in bboxes:
            continue
        bboxes.add(bbox)
        targets.append(object_id)
    return tuple(targets)


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _horizontal_overlap_ratio(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    minimum = min(left[2] - left[0], right[2] - right[0])
    return overlap / max(0.1, minimum)


def _vertical_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _union_rect(
    rectangles: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    return (
        min(item[0] for item in rectangles),
        min(item[1] for item in rectangles),
        max(item[2] for item in rectangles),
        max(item[3] for item in rectangles),
    )


def _deduplicate_findings(
    findings: tuple[Finding, ...],
) -> tuple[Finding, ...]:
    unique: dict[tuple[str, tuple[str, ...]], Finding] = {}
    for item in findings:
        unique.setdefault((item.code, item.evidence_ids), item)
    return tuple(unique.values())
