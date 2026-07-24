"""Wrap the lifted cover core in the production PageToolbox lifecycle."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import Finding
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.lifted_contracts import (
    PageTranslationBundle,
    lift_page_facts,
)
from transflow.toolboxes.leaves.lifted_text_leaf import (
    LiftedAtomicTextToolbox,
    LiftedCoreFinding,
    LiftedPlacementSpec,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

from .constants import TOOLBOX_KEY
from .layout import plan_cover_layout
from .models import (
    CoverContainer,
    CoverLayoutPlan,
    CoverPlacement,
    CoverTemplate,
)
from .template import build_cover_template


class CoverToolbox(
    LiftedAtomicTextToolbox[
        CoverTemplate,
        CoverContainer,
        CoverLayoutPlan,
        CoverPlacement,
    ]
):
    """Translate sparse native cover text while preserving visual objects."""

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
        source_pdf: Path,
    ) -> None:
        super().__init__(TOOLBOX_KEY, policy, font_path)
        self._source_pdf = source_pdf.resolve()

    def _validate_page_source(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> None:
        if _sha256_file(self._source_pdf) != context.source_hash:
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "cover source PDF hash drifted",
            )

    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> CoverTemplate:
        return build_cover_template(
            lift_page_facts(facts),
            self._source_pdf,
        )

    def _requested_containers(
        self,
        template: CoverTemplate,
    ) -> tuple[CoverContainer, ...]:
        return tuple(
            container
            for container in template.containers
            if container.translatable
            and _requires_translation(
                container.source_text,
                self._policy.target_language,
            )
        )

    def _zero_translation_passthrough(self) -> bool:
        return True

    def _plan_core_layout(
        self,
        template: CoverTemplate,
        bundle: PageTranslationBundle,
    ) -> tuple[CoverLayoutPlan, tuple[LiftedCoreFinding, ...]]:
        return plan_cover_layout(
            template,
            bundle,
            font_file=str(self._font_path),
        )

    def _layout_placements(
        self,
        layout: CoverLayoutPlan,
    ) -> tuple[CoverPlacement, ...]:
        return layout.placements

    def _placement_spec(
        self,
        placement: CoverPlacement,
    ) -> LiftedPlacementSpec:
        return LiftedPlacementSpec(
            container_id=placement.container_id,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            text_align=placement.alignment,
            render_text=placement.render_text,
        )

    def _additional_findings(
        self,
        plan_id: str,
        template: CoverTemplate,
        layout: CoverLayoutPlan,
    ) -> tuple[Finding, ...]:
        unsupported = tuple(
            placement.container_id for placement in layout.placements if not placement.render_text
        )
        if not unsupported:
            return ()
        return (
            Finding(
                f"{plan_id}-deduplication-patch-unsupported",
                "COVER_DEDUPLICATION_PATCH_UNSUPPORTED",
                "HARD",
                unsupported,
            ),
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
