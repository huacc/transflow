from __future__ import annotations

import unittest

from toolboxes.body.flow_text.multi.tools.models import (
    ColumnAssignment,
    ColumnBand,
    ColumnLayoutSelection,
    MultiColumnLayoutPlan,
    MultiColumnTemplate,
)
from toolboxes.body.flow_text.multi.tools.orchestrator.typography_repair_loop import (
    TypographyRepairAction,
    classify_typography_attempt,
    new_typography_repair_memory,
    record_typography_attempt,
    select_next_typography_action,
)
from toolboxes.body.flow_text.multi.tools.repairs.line_height_recovery import (
    build_line_height_recovery_candidates,
)
from toolboxes.body.flow_text.multi.tools.repairs.font_scale_recovery import (
    build_font_scale_recovery_candidates,
)
from toolboxes.body.flow_text.multi.tools.validators.typography_density_rule import (
    evaluate_typography_density_failure,
)
from toolboxes.body.flow_text.single.tools.models import TextContainer
from toolboxes.body.flow_text.single.tools.p4_models import P4Placement


class P5TypographyRepairLoopTest(unittest.TestCase):
    def test_same_repair_action_is_not_selected_twice(self) -> None:
        memory = new_typography_repair_memory("page-1", "initial-state")
        action = TypographyRepairAction(
            failure_class="body_line_height_too_tight",
            repair_atom="line_height_recovery",
            bound_tool="tools/repairs/line_height_recovery.py",
            target_column_ids=("column-1",),
            before_profiles=("column-1:font-95",),
            after_profiles=("column-1:font-95-line-height-105",),
            candidate_state_hash="candidate-state",
        )

        first = select_next_typography_action(memory, (action,))
        record_typography_attempt(
            memory,
            action=action,
            before_verdict="too_tight",
            after_verdict="too_tight",
            mechanical_gate="PASS",
            outcome="NO_IMPROVEMENT_ROLLBACK",
        )
        second = select_next_typography_action(memory, (action,))

        self.assertEqual("CANDIDATE_READY", first.status)
        self.assertEqual(action.action_key, first.action.action_key)
        self.assertEqual("CANDIDATES_EXHAUSTED", second.status)
        self.assertIsNone(second.action)
        self.assertEqual([action.action_key], memory["attempted_action_keys"])

    def test_same_typography_verdict_is_no_improvement_and_rolls_back(self) -> None:
        outcome = classify_typography_attempt(
            before_verdict="too_small_and_tight",
            after_verdict="too_small_and_tight",
            mechanical_gate="PASS",
        )

        self.assertEqual("NO_IMPROVEMENT_ROLLBACK", outcome)

    def test_candidate_that_recreates_a_seen_page_state_stops_as_cycle(self) -> None:
        memory = new_typography_repair_memory("page-1", "initial-state")
        action = TypographyRepairAction(
            failure_class="body_font_scale_too_small",
            repair_atom="font_scale_recovery",
            bound_tool="tools/repairs/font_scale_recovery.py",
            target_column_ids=("column-1",),
            before_profiles=("column-1:font-95",),
            after_profiles=("column-1:vertical-natural",),
            candidate_state_hash="initial-state",
        )

        selection = select_next_typography_action(memory, (action,))

        self.assertEqual("STATE_CYCLE_DETECTED", selection.status)
        self.assertIsNone(selection.action)
        self.assertEqual("STATE_CYCLE_DETECTED", memory["terminal_reason"])

    def test_empty_finite_candidate_set_has_explicit_exhausted_terminal_reason(self) -> None:
        memory = new_typography_repair_memory("page-1", "initial-state")

        selection = select_next_typography_action(memory, ())

        self.assertEqual("CANDIDATES_EXHAUSTED", selection.status)
        self.assertIsNone(selection.action)
        self.assertEqual("CANDIDATES_EXHAUSTED", memory["terminal_reason"])

    def test_combined_density_verdict_normalizes_to_one_line_height_failure_first(self) -> None:
        decision = evaluate_typography_density_failure(
            {"verdict": "too_small_and_tight", "reason": "both are visually weak"}
        )

        self.assertEqual("FAIL", decision["rule_verdict"])
        self.assertEqual("body_line_height_too_tight", decision["selected_failure_class"])
        self.assertEqual("line_height_recovery", decision["repair_atom"])

    def test_line_height_atom_changes_only_one_independent_column_and_keeps_x_bounds(self) -> None:
        template, plan = _two_column_plan()

        candidates = build_line_height_recovery_candidates(template=template, plan=plan)

        first = candidates[0]
        before_selection = {item.column_id: item for item in plan.column_selections}
        after_selection = {item.column_id: item for item in first.plan.column_selections}
        self.assertEqual(("column-1",), first.action.target_column_ids)
        self.assertGreater(after_selection["column-1"].line_height, before_selection["column-1"].line_height)
        self.assertEqual(before_selection["column-2"], after_selection["column-2"])
        before_x = {item.container_id: (item.output_bbox[0], item.output_bbox[2]) for item in plan.placements}
        after_x = {item.container_id: (item.output_bbox[0], item.output_bbox[2]) for item in first.plan.placements}
        self.assertEqual(before_x, after_x)

    def test_font_scale_atom_changes_only_font_scale_for_one_independent_column(self) -> None:
        template, plan = _two_column_plan()

        candidates = build_font_scale_recovery_candidates(template=template, plan=plan)

        first = candidates[0]
        before_selection = {item.column_id: item for item in plan.column_selections}
        after_selection = {item.column_id: item for item in first.plan.column_selections}
        self.assertEqual(("column-1",), first.action.target_column_ids)
        self.assertGreater(after_selection["column-1"].font_scale, before_selection["column-1"].font_scale)
        self.assertEqual(before_selection["column-1"].line_height, after_selection["column-1"].line_height)
        self.assertEqual(before_selection["column-2"], after_selection["column-2"])

    def test_paired_row_line_height_recovery_changes_both_columns_synchronously(self) -> None:
        template, plan = _paired_row_plan()

        candidate = build_line_height_recovery_candidates(
            template=template,
            plan=plan,
            candidate_limit=1,
        )[0]

        self.assertEqual(("column-1", "column-2"), candidate.action.target_column_ids)
        placement_by_id = {item.container_id: item for item in candidate.plan.placements}
        for row in range(4):
            self.assertAlmostEqual(
                placement_by_id[f"left-{row}"].output_bbox[1],
                placement_by_id[f"right-{row}"].output_bbox[1],
            )

def _two_column_plan() -> tuple[MultiColumnTemplate, MultiColumnLayoutPlan]:
    containers = (
        TextContainer("left-1", ("left-source-1",), "Left source", 1, "body", (50, 100, 250, 130), (50, 100), 10, 0),
        TextContainer("left-2", ("left-source-2",), "Left source two", 2, "body", (50, 150, 250, 180), (50, 150), 10, 0),
        TextContainer("right-1", ("right-source-1",), "Right source", 3, "body", (330, 100, 530, 130), (330, 100), 10, 0),
        TextContainer("right-2", ("right-source-2",), "Right source two", 4, "body", (330, 150, 530, 180), (330, 150), 10, 0),
    )
    columns = (
        ColumnBand("column-1", 1, 50, 250, 100, 700),
        ColumnBand("column-2", 2, 330, 530, 100, 700),
    )
    template = MultiColumnTemplate(
        "page-1",
        "body.flow_text.multi",
        600,
        800,
        columns,
        containers,
        (
            ColumnAssignment("left-1", "column-1", 1),
            ColumnAssignment("left-2", "column-1", 2),
            ColumnAssignment("right-1", "column-2", 1),
            ColumnAssignment("right-2", "column-2", 2),
        ),
    )
    placements = tuple(
        P4Placement(
            item.container_id,
            f"Translated text for {item.container_id} with enough words to wrap across two lines.",
            item.role,
            item.source_bbox,
            item.source_bbox,
            "column_width_invariant",
            item.font_size,
            9.5,
            1.0,
            "independent_column_vertical_flow",
            20.0 if item.container_id.endswith("2") else 0.0,
            10.0 if item.container_id.endswith("2") else 0.0,
            item.color_srgb,
            item.font_weight,
            True,
        )
        for item in containers
    )
    plan = MultiColumnLayoutPlan(
        "page-1",
        "body.flow_text.multi",
        "en",
        "zh-CN",
        "C:/Windows/Fonts/msyh.ttc",
        "p5cjk",
        columns,
        (
            ColumnLayoutSelection("column-1", "font-95", 0.95, 1.0, 0.55, True),
            ColumnLayoutSelection("column-2", "font-95", 0.95, 1.0, 0.55, True),
        ),
        placements,
    )
    return template, plan


def _paired_row_plan() -> tuple[MultiColumnTemplate, MultiColumnLayoutPlan]:
    columns = (
        ColumnBand("column-1", 1, 50, 250, 100, 700),
        ColumnBand("column-2", 2, 330, 530, 100, 700),
    )
    containers = []
    assignments = []
    placements = []
    reading_order = 0
    for column_id, prefix, left, right in (
        ("column-1", "left", 50, 250),
        ("column-2", "right", 330, 530),
    ):
        for row in range(4):
            reading_order += 1
            top = 100 + row * 70
            container_id = f"{prefix}-{row}"
            bbox = (
                left,
                top,
                left + 120 if column_id == "column-1" else right,
                top + 20,
            )
            containers.append(TextContainer(container_id, (f"source-{container_id}",), f"Source {row}", reading_order, "body", bbox, (left, top), 10, 0))
            assignments.append(ColumnAssignment(container_id, column_id, row + 1))
            placements.append(P4Placement(container_id, f"Translated row {row}", "body", bbox, bbox, "paired_row_width_invariant", 10, 10, 1.1, "paired_row_synchronous_vertical_reflow", 50 if row else 0, 50 if row else 0, 0, "regular", True))
    template = MultiColumnTemplate("paired", "body.flow_text.multi", 600, 800, columns, tuple(containers), tuple(assignments))
    plan = MultiColumnLayoutPlan(
        "paired",
        "body.flow_text.multi",
        "en",
        "zh-CN",
        "C:/Windows/Fonts/msyh.ttc",
        "p5cjk",
        columns,
        (
            ColumnLayoutSelection("column-1", "line-gap-compact", 1.0, 1.0, 0.65, True),
            ColumnLayoutSelection("column-2", "line-gap-compact", 1.0, 1.0, 0.65, True),
        ),
        tuple(placements),
    )
    return template, plan


if __name__ == "__main__":
    unittest.main()
