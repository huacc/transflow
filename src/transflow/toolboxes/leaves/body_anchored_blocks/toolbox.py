"""Wrap the anchored-block core in the production PageToolbox lifecycle."""

from __future__ import annotations

import re
from pathlib import Path

from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.body_anchored_blocks.layout import (
    plan_anchored_layout,
)
from transflow.toolboxes.leaves.body_anchored_blocks.models import (
    AnchoredBlocksTemplate,
    AnchoredContainer,
    AnchoredFinding,
    AnchoredLayoutPlan,
    AnchoredPlacement,
)
from transflow.toolboxes.leaves.body_anchored_blocks.template import (
    TOOLBOX_KEY,
    build_anchored_blocks_template,
)
from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle
from transflow.toolboxes.leaves.lifted_text_leaf import (
    LiftedAtomicTextToolbox,
    LiftedPlacementSpec,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


class AnchoredBlocksToolbox(
    LiftedAtomicTextToolbox[
        AnchoredBlocksTemplate,
        AnchoredContainer,
        AnchoredLayoutPlan,
        AnchoredPlacement,
    ]
):
    """Translate independent owners without Provider orchestration."""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        super().__init__(TOOLBOX_KEY, policy, font_path)

    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> AnchoredBlocksTemplate:
        return build_anchored_blocks_template(facts, self._policy)

    def _requested_containers(
        self,
        template: AnchoredBlocksTemplate,
    ) -> tuple[AnchoredContainer, ...]:
        return tuple(
            item
            for item in template.translatable_containers
            if _requires_translation(
                item.source_text,
                self._policy.target_language,
            )
        )

    def _plan_core_layout(
        self,
        template: AnchoredBlocksTemplate,
        bundle: PageTranslationBundle,
        facts: ExtractedPageFacts,
    ) -> tuple[AnchoredLayoutPlan, tuple[AnchoredFinding, ...]]:
        del facts
        return plan_anchored_layout(
            template,
            bundle,
            self._policy,
            self._font_path,
        )

    def _layout_placements(
        self,
        layout: AnchoredLayoutPlan,
    ) -> tuple[AnchoredPlacement, ...]:
        return layout.placements

    def _placement_spec(
        self,
        placement: AnchoredPlacement,
    ) -> LiftedPlacementSpec:
        return LiftedPlacementSpec(
            container_id=placement.container_id,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            text_align=placement.alignment,
            preserve_drawing_overlap=True,
        )


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin
