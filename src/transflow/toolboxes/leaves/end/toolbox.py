"""Wrap the lifted end core in the production PageToolbox lifecycle."""

from __future__ import annotations

from pathlib import Path

from transflow.domain.toolbox import Finding
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.lifted_contracts import (
    PageTranslationBundle,
    lift_page_facts,
)
from transflow.toolboxes.leaves.lifted_text_leaf import (
    LiftedAtomicTextToolbox,
    LiftedPlacementSpec,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

from .constants import TOOLBOX_KEY
from .layout import plan_end_layout
from .models import (
    EndFinding,
    EndLayoutPlan,
    EndPlacement,
    EndTemplate,
    EndTextRegion,
)
from .template import build_end_template


class EndToolbox(
    LiftedAtomicTextToolbox[
        EndTemplate,
        EndTextRegion,
        EndLayoutPlan,
        EndPlacement,
    ]
):
    """Translate native end-page semantics while preserving fixed anchors."""

    def __init__(
        self,
        policy: P8ToolboxPolicy,
        font_path: Path,
    ) -> None:
        super().__init__(TOOLBOX_KEY, policy, font_path)

    def _build_core_template(
        self,
        facts: ExtractedPageFacts,
    ) -> EndTemplate:
        return build_end_template(
            lift_page_facts(facts),
            self._policy.source_language,
            self._policy.target_language,
        )

    def _requested_containers(
        self,
        template: EndTemplate,
    ) -> tuple[EndTextRegion, ...]:
        return template.translatable_regions

    def _zero_translation_passthrough(self) -> bool:
        return True

    def _plan_core_layout(
        self,
        template: EndTemplate,
        bundle: PageTranslationBundle,
        facts: ExtractedPageFacts,
    ) -> tuple[EndLayoutPlan, tuple[EndFinding, ...]]:
        return plan_end_layout(
            template,
            bundle,
            font_file=str(self._font_path),
        )

    def _layout_placements(
        self,
        layout: EndLayoutPlan,
    ) -> tuple[EndPlacement, ...]:
        return layout.placements

    def _placement_spec(
        self,
        placement: EndPlacement,
    ) -> LiftedPlacementSpec:
        return LiftedPlacementSpec(
            container_id=placement.region_id,
            translated_text=placement.translated_text,
            output_bbox=placement.output_bbox,
            font_size=placement.font_size,
            line_height=placement.line_height,
            color_srgb=placement.color_srgb,
            text_align=placement.alignment.upper(),
        )

    def _additional_findings(
        self,
        plan_id: str,
        template: EndTemplate,
        layout: EndLayoutPlan,
    ) -> tuple[Finding, ...]:
        protected = tuple(region for region in template.regions if region.disposition == "protect")
        collisions = tuple(
            placement.region_id
            for placement in layout.placements
            if any(
                _intersection_area(
                    placement.output_bbox,
                    region.source_bbox,
                )
                > 0.05
                for region in protected
            )
        )
        if not collisions:
            return ()
        return (
            Finding(
                f"{plan_id}-protected-anchor-overlap",
                "END_PROTECTED_ANCHOR_OVERLAP",
                "HARD",
                collisions,
            ),
        )


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )
