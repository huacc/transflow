"""Wrap the lifted cover core in the production PageToolbox lifecycle."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

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
    lift_page_facts,
    lift_translation_bundle,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

from .layout import plan_cover_layout
from .models import CoverContainer, CoverPlacement, CoverTemplate
from .template import build_cover_template

ROUTE = "cover"


@dataclass(frozen=True, slots=True)
class _CoverSnapshot:
    facts: ExtractedPageFacts
    template: CoverTemplate
    requested_containers: tuple[CoverContainer, ...]


class CoverToolbox:
    """Translate sparse native cover text while preserving visual objects."""

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
            ROUTE,
            ROUTE,
            TOOLBOX_CONTRACT_VERSION,
            ROUTE,
        )
        self._snapshots: dict[str, _CoverSnapshot] = {}
        self._snapshots_by_plan: dict[str, _CoverSnapshot] = {}

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
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "cover 页面事实漂移")
        if _sha256_file(self._source_pdf) != context.source_hash:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "cover 源 PDF 哈希漂移")

        core_template = build_cover_template(
            lift_page_facts(facts),
            self._source_pdf,
        )
        requested = tuple(
            container
            for container in core_template.containers
            if container.translatable
            and _requires_translation(container.source_text, self._policy.target_language)
        )
        template_id = f"cover-{facts.page_identity[:24]}"
        self._snapshots[template_id] = _CoverSnapshot(
            facts=facts,
            template=core_template,
            requested_containers=requested,
        )
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=ROUTE,
            object_ids=tuple(container.container_id for container in requested),
        )

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        snapshot = self._snapshots[template.template_id]
        if not snapshot.requested_containers:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    (f"{snapshot.facts.page_identity}\0{container.container_id}\0{ROUTE}").encode(
                        "ascii"
                    )
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
            return ToolboxLayoutPlan(plan_id, ROUTE, None, (finding,), True)
        if dispatch.skip_reason is not None:
            return ToolboxLayoutPlan(
                plan_id,
                ROUTE,
                None,
                (),
                passthrough_requested=True,
            )
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "cover 缺少已校验译文",
            )

        translated_by_unit = {item.unit_id: item.translated_text for item in dispatch.bundle.units}
        lifted_bundle = lift_translation_bundle(
            request_id=dispatch.batch.batch_id,
            page_id=snapshot.template.page_id,
            translations=tuple(
                (container.container_id, translated_by_unit[unit.unit_id])
                for unit, container in zip(
                    dispatch.batch.units,
                    snapshot.requested_containers,
                    strict=True,
                )
            ),
        )
        layout, core_findings = plan_cover_layout(
            snapshot.template,
            lifted_bundle,
            font_file=str(self._font_path),
        )
        findings = [
            Finding(
                f"{plan_id}-{item.code.lower()}-{index:03d}",
                item.code,
                item.severity,
                (item.container_id or template.template_id,),
            )
            for index, item in enumerate(core_findings)
        ]
        unsupported_deduplications = tuple(
            placement.container_id for placement in layout.placements if not placement.render_text
        )
        if unsupported_deduplications:
            findings.append(
                Finding(
                    f"{plan_id}-deduplication-patch-unsupported",
                    "COVER_DEDUPLICATION_PATCH_UNSUPPORTED",
                    "HARD",
                    unsupported_deduplications,
                )
            )

        patch = _build_page_patch(
            snapshot,
            template,
            dispatch.batch,
            layout.placements,
            self._policy.font_id,
        )
        return ToolboxLayoutPlan(
            plan_id,
            ROUTE,
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
            reason = "COVER_PLAN_FALLBACK"
        elif findings:
            disposition = DecisionDisposition.FALLBACK
            reason = "COVER_HARD_FINDING"
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "COVER_PATCH_ACCEPTED"
                if candidate.plan.patch is not None
                else "COVER_PASSTHROUGH_ACCEPTED"
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
        """The lifted cover core already exhausts its deterministic size ladder."""

        return candidate


def _build_page_patch(
    snapshot: _CoverSnapshot,
    template: PageTemplate,
    batch: TranslationBatch,
    placements: tuple[CoverPlacement, ...],
    font_id: str,
) -> PagePatch | None:
    container_by_id = {
        container.container_id: container for container in snapshot.template.containers
    }
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
        if not placement.render_text:
            continue
        container = container_by_id[placement.container_id]
        unit = unit_by_container.get(placement.container_id)
        if unit is None:
            continue
        target_object_ids = tuple(
            object_id for object_id in container.source_object_ids if object_id in source_bbox_by_id
        )
        redaction_rects = tuple(source_bbox_by_id[object_id] for object_id in target_object_ids)
        payload_hash = patch_operation_hash(
            owner=ROUTE,
            target_object_ids=target_object_ids,
            rect=placement.output_bbox,
            replacement_text=placement.translated_text,
            font_id=font_id,
            font_size=placement.font_size,
            redaction_rects=redaction_rects,
            color_srgb=placement.color_srgb,
            line_height=placement.line_height,
            preserve_drawing_overlap=True,
            text_align=placement.alignment,
        )
        operations.append(
            PatchOperation(
                operation_id=f"op-{unit.unit_id[:20]}",
                region_id=unit.region_id,
                kind="replace_text",
                payload_hash=payload_hash,
                owner=ROUTE,
                target_object_ids=target_object_ids,
                rect=placement.output_bbox,
                replacement_text=placement.translated_text,
                font_id=font_id,
                font_size=placement.font_size,
                redaction_rects=redaction_rects,
                color_srgb=placement.color_srgb,
                line_height=placement.line_height,
                preserve_drawing_overlap=True,
                text_align=placement.alignment,
            )
        )
    if not operations:
        return None
    return PagePatch(
        patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{ROUTE}",
        source_hash=template.context.source_hash,
        page_no=template.context.page_no,
        geometry_hash=template.context.geometry_hash,
        owner=ROUTE,
        operations=tuple(operations),
    )


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
