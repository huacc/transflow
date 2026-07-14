from __future__ import annotations

from dataclasses import dataclass

from toolboxes.body.flow_text.single.tools.models import ToolboxFinding

from ..models import MultiColumnLayoutPlan


@dataclass(frozen=True)
class TypographyRepairAction:
    failure_class: str
    repair_atom: str
    bound_tool: str
    target_column_ids: tuple[str, ...]
    before_profiles: tuple[str, ...]
    after_profiles: tuple[str, ...]
    candidate_state_hash: str

    @property
    def action_key(self) -> str:
        return "|".join(
            (
                self.failure_class,
                self.repair_atom,
                ",".join(self.target_column_ids),
                ",".join(self.after_profiles),
            )
        )


@dataclass(frozen=True)
class TypographyActionSelection:
    status: str
    action: TypographyRepairAction | None
    reason: str


@dataclass(frozen=True)
class TypographyRepairCandidate:
    action: TypographyRepairAction
    plan: MultiColumnLayoutPlan
    planning_findings: tuple[ToolboxFinding, ...]


def new_typography_repair_memory(page_id: str, initial_state_hash: str) -> dict[str, object]:
    return {
        "schema_version": "p5-typography-repair-memory/v1",
        "page_id": page_id,
        "initial_state_hash": initial_state_hash,
        "attempted_action_keys": [],
        "seen_state_hashes": [initial_state_hash],
        "attempts": [],
        "terminal_reason": None,
    }


def select_next_typography_action(
    memory: dict[str, object],
    actions: tuple[TypographyRepairAction, ...],
) -> TypographyActionSelection:
    attempted = set(memory["attempted_action_keys"])
    seen_states = set(memory["seen_state_hashes"])
    for action in actions:
        if action.action_key in attempted:
            continue
        if action.candidate_state_hash in seen_states:
            memory["terminal_reason"] = "STATE_CYCLE_DETECTED"
            return TypographyActionSelection(
                "STATE_CYCLE_DETECTED",
                None,
                "candidate profile state already appeared in this page run",
            )
        return TypographyActionSelection("CANDIDATE_READY", action, "first untried finite candidate")
    memory["terminal_reason"] = "CANDIDATES_EXHAUSTED"
    return TypographyActionSelection(
        "CANDIDATES_EXHAUSTED",
        None,
        "all finite safe repair actions were attempted",
    )


def classify_typography_attempt(
    *,
    before_verdict: str,
    after_verdict: str | None,
    mechanical_gate: str,
) -> str:
    if mechanical_gate != "PASS":
        return "MECHANICAL_GATE_REJECTED_ROLLBACK"
    if after_verdict == before_verdict:
        return "NO_IMPROVEMENT_ROLLBACK"
    if after_verdict == "acceptable":
        return "ACCEPTED"
    return "ACCEPTED_NEW_FAILURE"


def record_typography_attempt(
    memory: dict[str, object],
    *,
    action: TypographyRepairAction,
    before_verdict: str,
    after_verdict: str | None,
    mechanical_gate: str,
    outcome: str,
    evidence: dict[str, object] | None = None,
) -> None:
    attempted = memory["attempted_action_keys"]
    if action.action_key not in attempted:
        attempted.append(action.action_key)
    seen_states = memory["seen_state_hashes"]
    if action.candidate_state_hash not in seen_states:
        seen_states.append(action.candidate_state_hash)
    record = {
        "attempt_index": len(memory["attempts"]) + 1,
        "failure_class": action.failure_class,
        "repair_atom": action.repair_atom,
        "bound_tool": action.bound_tool,
        "target_column_ids": list(action.target_column_ids),
        "action_key": action.action_key,
        "before_profiles": list(action.before_profiles),
        "after_profiles": list(action.after_profiles),
        "before_verdict": before_verdict,
        "after_verdict": after_verdict,
        "mechanical_gate": mechanical_gate,
        "outcome": outcome,
        "candidate_state_hash": action.candidate_state_hash,
    }
    if evidence:
        record["evidence"] = evidence
    memory["attempts"].append(record)
