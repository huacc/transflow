"""Shared mechanical PageToolbox wrapper for lifted atomic text cores."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
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
from transflow.pdf_kernel.patch import patch_operation_hash
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.lifted_contracts import (
    PageTranslationBundle,
    lift_translation_bundle,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


class LiftedCoreContainer(Protocol):
    """Minimum immutable container fields needed by the production wrapper."""

    container_id: str
    source_object_ids: tuple[str, ...]
    source_text: str


class LiftedCoreTemplate(Protocol):
    """Minimum immutable template identity needed by the production wrapper."""

    page_id: str


class LiftedCoreFinding(Protocol):
    """Minimum leaf-private finding fields projected into the shared contract."""

    code: str
    severity: str
    container_id: str | None


@dataclass(frozen=True, slots=True)
class LiftedPlacementSpec:
    """Project a leaf-private placement into one declarative text operation."""

    container_id: str
    translated_text: str
    output_bbox: tuple[float, float, float, float]
    font_size: float
    line_height: float
    color_srgb: int
    text_align: str
    render_text: bool = True
    preserve_drawing_overlap: bool = True


@dataclass(frozen=True, slots=True)
class _LiftedSnapshot[
    TemplateT: LiftedCoreTemplate,
    ContainerT: LiftedCoreContainer,
]:
    facts: ExtractedPageFacts
    template: TemplateT
    requested_containers: tuple[ContainerT, ...]


class LiftedAtomicTextToolbox[
    TemplateT: LiftedCoreTemplate,
    ContainerT: LiftedCoreContainer,
    LayoutT,
    PlacementT,
](ABC):
    """Keep leaf semantics private while sharing only lifecycle mechanics."""

    def __init__(
        self,
        route: str,
        policy: P8ToolboxPolicy,
        font_path: Path,
    ) -> None:
        self._route = route
        self._policy = policy
        self._font_path = font_path.resolve()
        self._descriptor = ToolboxDescriptor(
            route,
            route,
            TOOLBOX_CONTRACT_VERSION,
            route,
        )
        self._snapshots: dict[str, _LiftedSnapshot[TemplateT, ContainerT]] = {}
        self._snapshots_by_plan: dict[
            str,
            _LiftedSnapshot[TemplateT, ContainerT],
        ] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        return self._descriptor

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
                f"{self._route} page facts drifted",
            )
        self._validate_page_source(context, facts)
        core_template = self._build_core_template(facts)
        requested = self._requested_containers(core_template)
        template_id = f"{self._route.replace('.', '-')}-{facts.page_identity[:24]}"
        snapshot = _LiftedSnapshot(facts, core_template, requested)
        self._snapshots[template_id] = snapshot
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=self._route,
            object_ids=tuple(item.container_id for item in requested),
        )

    def build_translation_request(
        self,
        template: PageTemplate,
    ) -> TranslationBatch | None:
        snapshot = self._snapshots[template.template_id]
        if not snapshot.requested_containers:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    (
                        f"{snapshot.facts.page_identity}\0{container.container_id}\0{self._route}"
                    ).encode("ascii")
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=ordinal,
                source_text=container.source_text,
                region_id=container.container_id,
                source_object_ids=container.source_object_ids,
            )
            for ordinal, container in enumerate(snapshot.requested_containers)
        )
        return TranslationBatch(
            batch_id=(
                f"batch-{template.context.run_id}-p{template.context.page_no:04d}-{self._route}"
            ),
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=units,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        snapshot = self._snapshots[template.template_id]
        plan_id = f"plan-{template.template_id}"
        self._snapshots_by_plan[plan_id] = snapshot
        if dispatch.failure is not None:
            finding = Finding(
                f"{plan_id}-translation-failure",
                dispatch.failure.code,
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(
                plan_id,
                self._route,
                None,
                (finding,),
                fallback_requested=True,
            )
        if dispatch.skip_reason is not None:
            if self._zero_translation_passthrough():
                return ToolboxLayoutPlan(
                    plan_id,
                    self._route,
                    None,
                    (),
                    passthrough_requested=True,
                )
            finding = Finding(
                f"{plan_id}-zero-translation-unsupported",
                self._zero_translation_finding_code(),
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(
                plan_id,
                self._route,
                None,
                (finding,),
                fallback_requested=True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                f"{self._route} validated translation is missing",
            )

        translated_by_unit = {item.unit_id: item.translated_text for item in dispatch.bundle.units}
        lifted_bundle = lift_translation_bundle(
            request_id=dispatch.batch.batch_id,
            page_id=snapshot.template.page_id,
            translations=tuple(
                (
                    container.container_id,
                    translated_by_unit[unit.unit_id],
                )
                for unit, container in zip(
                    dispatch.batch.units,
                    snapshot.requested_containers,
                    strict=True,
                )
            ),
        )
        layout, core_findings = self._plan_core_layout(
            snapshot.template,
            lifted_bundle,
        )
        findings = [
            Finding(
                (f"{plan_id}-{item.code.casefold().replace('_', '-')}-{index:03d}"),
                item.code,
                item.severity,
                (item.container_id or template.template_id,),
            )
            for index, item in enumerate(core_findings)
        ]
        findings.extend(
            self._additional_findings(
                plan_id,
                snapshot.template,
                layout,
            )
        )
        patch = self._build_page_patch(
            snapshot,
            template,
            dispatch.batch,
            self._layout_placements(layout),
        )
        return ToolboxLayoutPlan(
            plan_id,
            self._route,
            patch,
            tuple(findings),
            fallback_requested=patch is None,
        )

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        snapshot = self._snapshots_by_plan[plan.plan_id]
        return ToolboxCandidate(
            candidate_id=f"candidate-{plan.plan_id}-0",
            plan=plan,
            render_fingerprint=content_sha256(
                {
                    "facts": snapshot.facts.kernel_facts_hash,
                    "plan": plan,
                    "source": context.source_hash,
                }
            ),
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        findings = candidate.plan.findings
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = f"{self._route.upper()}_PLAN_FALLBACK"
        elif findings:
            disposition = DecisionDisposition.FALLBACK
            reason = f"{self._route.upper()}_HARD_FINDING"
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                f"{self._route.upper()}_PATCH_ACCEPTED"
                if candidate.plan.patch is not None
                else f"{self._route.upper()}_PASSTHROUGH_ACCEPTED"
            )
        return ToolboxJudgement(
            findings=findings,
            decision=Decision(
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
        """The lifted core has already exhausted its deterministic fit ladder."""

        return candidate

    def _build_page_patch(
        self,
        snapshot: _LiftedSnapshot[TemplateT, ContainerT],
        template: PageTemplate,
        batch: TranslationBatch,
        placements: tuple[PlacementT, ...],
    ) -> PagePatch | None:
        container_by_id = {item.container_id: item for item in snapshot.requested_containers}
        unit_by_container = {
            container.container_id: unit
            for unit, container in zip(
                batch.units,
                snapshot.requested_containers,
                strict=True,
            )
        }
        source_bbox_by_id = {item.object_id: item.bbox for item in snapshot.facts.text_spans}
        operations: list[PatchOperation] = []
        for placement in placements:
            spec = self._placement_spec(placement)
            if not spec.render_text:
                continue
            container = container_by_id.get(spec.container_id)
            unit = unit_by_container.get(spec.container_id)
            if container is None or unit is None:
                continue
            if not container.source_object_ids or any(
                object_id not in source_bbox_by_id for object_id in container.source_object_ids
            ):
                raise DomainContractError(
                    ErrorCode.PATCH_OWNER_VIOLATION,
                    f"{self._route} placement lost source object binding",
                )
            target_object_ids = container.source_object_ids
            redaction_rects = tuple(source_bbox_by_id[object_id] for object_id in target_object_ids)
            payload_hash = patch_operation_hash(
                owner=self._route,
                target_object_ids=target_object_ids,
                rect=spec.output_bbox,
                replacement_text=spec.translated_text,
                font_id=self._policy.font_id,
                font_size=spec.font_size,
                redaction_rects=redaction_rects,
                color_srgb=spec.color_srgb,
                line_height=spec.line_height,
                preserve_drawing_overlap=spec.preserve_drawing_overlap,
                text_align=spec.text_align,
            )
            operations.append(
                PatchOperation(
                    operation_id=f"op-{unit.unit_id[:20]}",
                    region_id=unit.region_id,
                    kind="replace_text",
                    payload_hash=payload_hash,
                    owner=self._route,
                    target_object_ids=target_object_ids,
                    rect=spec.output_bbox,
                    replacement_text=spec.translated_text,
                    font_id=self._policy.font_id,
                    font_size=spec.font_size,
                    redaction_rects=redaction_rects,
                    color_srgb=spec.color_srgb,
                    line_height=spec.line_height,
                    preserve_drawing_overlap=spec.preserve_drawing_overlap,
                    text_align=spec.text_align,
                )
            )
        if not operations:
            return None
        return PagePatch(
            patch_id=(f"patch-{snapshot.facts.page_identity[:24]}-{self._route}"),
            source_hash=template.context.source_hash,
            page_no=template.context.page_no,
            geometry_hash=template.context.geometry_hash,
            owner=self._route,
            operations=tuple(operations),
        )

    def _validate_page_source(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> None:
        return None

    def _zero_translation_passthrough(self) -> bool:
        return False

    def _zero_translation_finding_code(self) -> str:
        return f"{self._route.upper()}_TRANSLATION_UNITS_MISSING"

    def _additional_findings(
        self,
        plan_id: str,
        template: TemplateT,
        layout: LayoutT,
    ) -> tuple[Finding, ...]:
        return ()

    @abstractmethod
    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> TemplateT:
        """Build the unchanged leaf-private structural template."""

    @abstractmethod
    def _requested_containers(
        self,
        template: TemplateT,
    ) -> tuple[ContainerT, ...]:
        """Return only containers owned by the shared translation batch."""

    @abstractmethod
    def _plan_core_layout(
        self,
        template: TemplateT,
        bundle: PageTranslationBundle,
    ) -> tuple[LayoutT, tuple[LiftedCoreFinding, ...]]:
        """Run the unchanged leaf-private layout planner."""

    @abstractmethod
    def _layout_placements(
        self,
        layout: LayoutT,
    ) -> tuple[PlacementT, ...]:
        """Expose leaf-private placements without sharing their semantics."""

    @abstractmethod
    def _placement_spec(
        self,
        placement: PlacementT,
    ) -> LiftedPlacementSpec:
        """Project one placement into shared mechanical Patch fields."""
