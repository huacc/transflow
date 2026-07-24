"""Wrap the lifted contents core in the production PageToolbox lifecycle."""

from __future__ import annotations

from pathlib import Path

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
from .layout import plan_contents_layout
from .models import (
    ContentsContainer,
    ContentsLayoutPlan,
    ContentsPlacement,
    ContentsTemplate,
)
from .template import build_contents_template


class ContentsToolbox(
    LiftedAtomicTextToolbox[
        ContentsTemplate,
        ContentsContainer,
        ContentsLayoutPlan,
        ContentsPlacement,
    ]
):
    """Translate contents labels while keeping page-number anchors immutable."""

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
    ) -> None:
        super().__init__(TOOLBOX_KEY, policy, font_path)

    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> ContentsTemplate:
        return build_contents_template(lift_page_facts(facts))

    def _requested_containers(
        self,
        template: ContentsTemplate,
    ) -> tuple[ContentsContainer, ...]:
        return template.containers

    def _zero_translation_finding_code(self) -> str:
        return "CONTENTS_NATIVE_TEXT_REQUIRED"

    def _plan_core_layout(
        self,
        template: ContentsTemplate,
        bundle: PageTranslationBundle,
    ) -> tuple[ContentsLayoutPlan, tuple[LiftedCoreFinding, ...]]:
        return plan_contents_layout(
            template,
            bundle,
            font_file=str(self._font_path),
        )

    def _layout_placements(
        self,
        layout: ContentsLayoutPlan,
    ) -> tuple[ContentsPlacement, ...]:
        return layout.placements

    def _placement_spec(
        self,
        placement: ContentsPlacement,
    ) -> LiftedPlacementSpec:
        return LiftedPlacementSpec(
            container_id=placement.container_id,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            text_align="LEFT",
        )
