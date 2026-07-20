"""让 PageCoordinator 独占 TranslationPort 调度并驱动 PageToolbox 六阶段。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import DecisionDisposition
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.ports.translation import TranslationPort
from transflow.toolboxes.contracts import (
    PageToolbox,
    ToolboxExecutionResult,
    ToolboxExecutionTrace,
    TranslationDispatch,
    TranslationFailure,
    normalized_page_outcome,
)

LOGGER = logging.getLogger("transflow.application.toolbox_page_coordinator")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class TranslationTraceEvent:
    """记录兼容迁移所需的请求与消费顺序，不保存密钥或 Provider 信息。"""

    page_no: int
    batch_id: str
    event: str
    unit_ids: tuple[str, ...]


class TranslationCompatibilityRecorder:
    """以线程安全方式记录旧叶拆分前后可比较的稳定翻译 trace。"""

    def __init__(self) -> None:
        """初始化当前进程内、无外部副作用的事件列表。"""

        self._events: list[TranslationTraceEvent] = []
        self._lock = Lock()

    def record(self, event: TranslationTraceEvent) -> None:
        """追加一条不含秘密的稳定事件。"""

        LOGGER.info(
            "调用翻译兼容记录，意图=保留迁移顺序 page_no=%s event=%s",
            event.page_no,
            event.event,
        )
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[TranslationTraceEvent, ...]:
        """返回事件不可变快照，避免调用方修改内部状态。"""

        with self._lock:
            return tuple(self._events)


@dataclass(frozen=True, slots=True)
class ToolboxPageWork:
    """绑定一个页面的上下文、事实和已经由 Catalog 解析的 Toolbox。"""

    context: PageExecutionContext
    facts: ExtractedPageFacts
    toolbox: PageToolbox


class ToolboxPageCoordinator:
    """执行唯一翻译调度、身份校验、六阶段编排和页面顺序归并。"""

    def __init__(
        self,
        translation: TranslationPort,
        recorder: TranslationCompatibilityRecorder | None = None,
    ) -> None:
        """绑定 TranslationPort 和可选的无秘密迁移记录器。"""

        self._translation = translation
        self._recorder = recorder

    def execute(self, work: ToolboxPageWork) -> ToolboxExecutionResult:
        """执行单页六阶段；Toolbox 从不获得 TranslationPort 实例。"""

        context = work.context
        toolbox = work.toolbox
        descriptor = toolbox.descriptor
        LOGGER.info(
            "调用 Toolbox 页面协调，意图=统一调度翻译 page_no=%s route=%s",
            context.page_no,
            descriptor.route,
        )
        template = toolbox.prepare(context, work.facts)
        if template.context != context or template.owner != descriptor.owner:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "Toolbox 模板上下文或 owner 漂移")
        batch = toolbox.build_translation_request(template)
        if batch is None:
            # 零翻译叶通过显式 Skip 收敛，绝不触碰 TranslationPort。
            dispatch = TranslationDispatch(batch=None, skip_reason="TOOLBOX_ZERO_TRANSLATION")
            ordered_unit_ids: tuple[str, ...] = ()
        else:
            if any(unit.page_no != context.page_no for unit in batch.units):
                raise DomainContractError(ErrorCode.INVALID_IDENTITY, "翻译请求包含其他页面单元")
            ordered_unit_ids = batch.ordered_unit_ids
            self._record(context.page_no, batch.batch_id, "request", ordered_unit_ids)
            try:
                bundle = self._translation.translate(batch)
                # 构造 TranslationDispatch 会在交回叶子前再次核对 batch/unit 身份。
                dispatch = TranslationDispatch(batch=batch, bundle=bundle)
                returned_ids = bundle.requested_unit_ids
            except (DomainContractError, PortCallError, TimeoutError) as error:
                dispatch = TranslationDispatch(
                    batch=batch,
                    failure=TranslationFailure(
                        code=getattr(getattr(error, "code", None), "value", type(error).__name__),
                        retryable=bool(getattr(error, "retryable", False)),
                        detail="TranslationPort 调用失败，叶必须执行确定性 fallback",
                    ),
                )
                returned_ids = ()
            self._record(context.page_no, batch.batch_id, "consume", returned_ids)
        plan = toolbox.consume_translation_bundle(template, dispatch)
        if plan.route != descriptor.route:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "布局计划 Route 漂移")
        if plan.patch is not None:
            plan.patch.validate_binding(context, descriptor.owner)
        candidate = toolbox.render(context, work.facts, plan)
        judgement = toolbox.judge(candidate)
        repaired = toolbox.repair(candidate, judgement)
        # Repair 产生新候选后必须重新裁决，不能沿用修复前 verdict。
        final_judgement = toolbox.judge(repaired) if repaired != candidate else judgement
        accepted = (
            final_judgement.decision.disposition is DecisionDisposition.ACCEPT
            and not repaired.plan.fallback_requested
        )
        finding_by_id = {
            item.finding_id: item
            for item in (*repaired.plan.findings, *judgement.findings, *final_judgement.findings)
        }
        findings = tuple(finding_by_id.values())
        outcome = normalized_page_outcome(
            context.page_no,
            accepted=accepted,
            translated=dispatch.bundle is not None,
            finding_codes=tuple(dict.fromkeys(item.code for item in findings)),
            passthrough=accepted and repaired.plan.passthrough_requested,
            region_fallback=accepted and repaired.plan.region_fallback_applied,
        )
        return ToolboxExecutionResult(
            page_no=context.page_no,
            patch=repaired.plan.patch if accepted else None,
            findings=findings,
            verdict=final_judgement.decision,
            outcome=outcome,
            trace=ToolboxExecutionTrace(
                (
                    "prepare",
                    "build_translation_request",
                    "consume_translation_bundle",
                    "render",
                    "judge",
                    "repair",
                    "outcome",
                )
            ),
            ordered_unit_ids=ordered_unit_ids,
            # 原始提案只供穿刺和迁移诊断落盘；正式发布仍只能使用上方批准后的 patch。
            proposed_patch=plan.patch,
        )

    def execute_many(
        self,
        work_items: tuple[ToolboxPageWork, ...],
        page_concurrency: int,
    ) -> tuple[ToolboxExecutionResult, ...]:
        """并发执行多个页面，并按 page_no 而非响应到达顺序归位。"""

        if page_concurrency < 1:
            raise ValueError("page_concurrency 必须为正整数")
        expected_pages = tuple(sorted(item.context.page_no for item in work_items))
        if len(expected_pages) != len(set(expected_pages)):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "并发页面列表存在重复 page_no")
        completed: list[ToolboxExecutionResult] = []
        with ThreadPoolExecutor(max_workers=page_concurrency) as executor:
            futures = {
                executor.submit(self.execute, item): item.context.page_no for item in work_items
            }
            for future in as_completed(futures):
                result = future.result()
                if result.page_no != futures[future]:
                    raise DomainContractError(ErrorCode.INVALID_IDENTITY, "并发结果发生串页")
                completed.append(result)
        ordered = tuple(sorted(completed, key=lambda item: item.page_no))
        if tuple(item.page_no for item in ordered) != expected_pages:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "并发结果遗漏或乱序未收敛")
        return ordered

    def _record(
        self,
        page_no: int,
        batch_id: str,
        event: str,
        unit_ids: tuple[str, ...],
    ) -> None:
        """在启用 recorder 时记录稳定事件，否则保持零副作用。"""

        if self._recorder is not None:
            self._recorder.record(TranslationTraceEvent(page_no, batch_id, event, unit_ids))


def main() -> int:
    """记录 TranslationPort 只由 PageCoordinator 持有的生产边界。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ToolboxPageCoordinator 示例，意图=集中翻译调度并按页面归位")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
