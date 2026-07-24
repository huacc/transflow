"""Wrap the visual-anchored core in the production PageToolbox lifecycle."""

from __future__ import annotations

import re
from pathlib import Path

from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.body_flow_text_visual_anchored.layout import (
    plan_visual_anchored_layout,
)
from transflow.toolboxes.leaves.body_flow_text_visual_anchored.models import (
    VisualAnchoredContainer,
    VisualAnchoredFinding,
    VisualAnchoredLayoutPlan,
    VisualAnchoredPlacement,
    VisualAnchoredTemplate,
)
from transflow.toolboxes.leaves.body_flow_text_visual_anchored.template import (
    TOOLBOX_KEY,
    build_visual_anchored_template,
)
from transflow.toolboxes.leaves.lifted_contracts import (
    PageTranslationBundle,
)
from transflow.toolboxes.leaves.lifted_text_leaf import (
    LiftedAtomicTextToolbox,
    LiftedPlacementSpec,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


class VisualAnchoredToolbox(
    LiftedAtomicTextToolbox[
        VisualAnchoredTemplate,
        VisualAnchoredContainer,
        VisualAnchoredLayoutPlan,
        VisualAnchoredPlacement,
    ]
):
    """Translate native flow text without moving its fixed visual owner."""

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
    ) -> None:
        super().__init__(TOOLBOX_KEY, policy, font_path)

    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> VisualAnchoredTemplate:
        return build_visual_anchored_template(facts, self._policy)

    def _requested_containers(
        self,
        template: VisualAnchoredTemplate,
    ) -> tuple[VisualAnchoredContainer, ...]:
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
        template: VisualAnchoredTemplate,
        bundle: PageTranslationBundle,
        facts: ExtractedPageFacts,
    ) -> tuple[
        VisualAnchoredLayoutPlan,
        tuple[VisualAnchoredFinding, ...],
    ]:
        del facts
        return plan_visual_anchored_layout(
            template,
            bundle,
            self._policy,
            self._font_path,
        )

    def _layout_placements(
        self,
        layout: VisualAnchoredLayoutPlan,
    ) -> tuple[VisualAnchoredPlacement, ...]:
        return layout.placements

    def _placement_spec(
        self,
        placement: VisualAnchoredPlacement,
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


def _requires_translation(
    text: str,
    target_language: str,
) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin
