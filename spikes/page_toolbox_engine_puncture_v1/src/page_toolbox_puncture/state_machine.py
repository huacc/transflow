from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class InvalidTransition(RuntimeError):
    pass


class PageState(str, Enum):
    SAMPLE_READY = "SAMPLE_READY"
    FACTS_READY = "FACTS_READY"
    TEMPLATE_READY = "TEMPLATE_READY"
    TRANSLATION_READY = "TRANSLATION_READY"
    PATCH_READY = "PATCH_READY"
    CANDIDATE_READY = "CANDIDATE_READY"
    QUALITY_DECIDED = "QUALITY_DECIDED"
    REPAIRING = "REPAIRING"
    PAGE_PASSED = "PAGE_PASSED"
    CAPABILITY_FAILED = "CAPABILITY_FAILED"
    QUALITY_FAILED = "QUALITY_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"


STATE_LABEL_ZH = {
    PageState.SAMPLE_READY: "样本已准备",
    PageState.FACTS_READY: "页面事实已就绪",
    PageState.TEMPLATE_READY: "页面模板已就绪",
    PageState.TRANSLATION_READY: "译文已就绪",
    PageState.PATCH_READY: "排版计划已就绪",
    PageState.CANDIDATE_READY: "候选页已就绪",
    PageState.QUALITY_DECIDED: "质量裁决已完成",
    PageState.REPAIRING: "局部修复中",
    PageState.PAGE_PASSED: "页面通过",
    PageState.CAPABILITY_FAILED: "能力不足",
    PageState.QUALITY_FAILED: "产品质量失败",
    PageState.PROCESS_FAILED: "流程契约失败",
}


ALLOWED = {
    PageState.SAMPLE_READY: {PageState.FACTS_READY},
    PageState.FACTS_READY: {PageState.TEMPLATE_READY},
    PageState.TEMPLATE_READY: {PageState.TRANSLATION_READY},
    PageState.TRANSLATION_READY: {PageState.PATCH_READY},
    PageState.PATCH_READY: {PageState.CANDIDATE_READY},
    PageState.CANDIDATE_READY: {PageState.QUALITY_DECIDED},
    PageState.QUALITY_DECIDED: {PageState.REPAIRING, PageState.PAGE_PASSED, PageState.QUALITY_FAILED},
    PageState.REPAIRING: {PageState.CANDIDATE_READY, PageState.QUALITY_FAILED},
}

TERMINAL = {PageState.PAGE_PASSED, PageState.CAPABILITY_FAILED, PageState.QUALITY_FAILED, PageState.PROCESS_FAILED}
FAILABLE = set(PageState) - TERMINAL


@dataclass(frozen=True)
class StateEvent:
    sequence: int
    from_state: str | None
    to_state: str
    to_state_zh: str
    event: str
    evidence_ref: str | None
    recorded_at: str


class PageStateMachine:
    def __init__(self) -> None:
        self.current = PageState.SAMPLE_READY
        self.events = [self._event(None, self.current, "样本快照和清单已创建", None)]

    def transition(self, target: PageState, event: str, evidence_ref: str | None = None) -> None:
        if target not in ALLOWED.get(self.current, set()):
            raise InvalidTransition(f"illegal_transition:{self.current.value}->{target.value}")
        previous = self.current
        self.current = target
        self.events.append(self._event(previous, target, event, evidence_ref))

    def fail_capability(self, event: str, evidence_ref: str | None = None) -> None:
        if self.current not in FAILABLE:
            raise InvalidTransition(f"terminal_state_cannot_fail:{self.current.value}")
        previous = self.current
        self.current = PageState.CAPABILITY_FAILED
        self.events.append(self._event(previous, self.current, event, evidence_ref))

    def fail_process(self, event: str, evidence_ref: str | None = None) -> None:
        if self.current not in FAILABLE:
            raise InvalidTransition(f"terminal_state_cannot_fail:{self.current.value}")
        previous = self.current
        self.current = PageState.PROCESS_FAILED
        self.events.append(self._event(previous, self.current, event, evidence_ref))

    def _event(self, source: PageState | None, target: PageState, event: str, evidence_ref: str | None) -> StateEvent:
        return StateEvent(
            sequence=len(getattr(self, "events", [])),
            from_state=source.value if source else None,
            to_state=target.value,
            to_state_zh=STATE_LABEL_ZH[target],
            event=event,
            evidence_ref=evidence_ref,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
