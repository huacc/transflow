from __future__ import annotations

from .models import RepairDecision


class RepairController:
    def __init__(self, baseline_candidate_ref: str, *, max_rounds: int = 3, max_no_improvement: int = 2) -> None:
        if max_rounds < 1 or max_no_improvement < 1:
            raise ValueError("repair_limits_must_be_positive")
        self.selected_candidate_ref = baseline_candidate_ref
        self.max_rounds = max_rounds
        self.max_no_improvement = max_no_improvement
        self.round_index = 0
        self.no_improvement_count = 0
        self.stopped = False

    def consider(
        self,
        *,
        trial_candidate_ref: str,
        target_score_before: float,
        target_score_after: float,
        hard_findings_before: set[str],
        hard_findings_after: set[str],
        locked_objects_unchanged: bool,
    ) -> RepairDecision:
        if self.stopped:
            return RepairDecision("BUDGET_EXHAUSTED", False, self.selected_candidate_ref, self.round_index, self.no_improvement_count, "repair_session_already_stopped")
        self.round_index += 1
        target_improved = target_score_after < target_score_before
        no_new_hard = hard_findings_after.issubset(hard_findings_before)
        accepted = target_improved and no_new_hard and locked_objects_unchanged
        if accepted:
            self.selected_candidate_ref = trial_candidate_ref
            self.no_improvement_count = 0
            outcome = "ACCEPTED"
            reason = "target_improved_without_hard_regression"
        else:
            self.no_improvement_count += 1
            outcome = "ROLLED_BACK"
            reason = "target_not_improved_or_hard_regression"
        if self.round_index >= self.max_rounds or self.no_improvement_count >= self.max_no_improvement:
            self.stopped = True
            if not accepted:
                outcome = "NO_IMPROVEMENT" if self.no_improvement_count >= self.max_no_improvement else "BUDGET_EXHAUSTED"
        return RepairDecision(outcome, accepted, self.selected_candidate_ref, self.round_index, self.no_improvement_count, reason)

