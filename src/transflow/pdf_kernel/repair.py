"""实现 P6 机械 Finding 驱动且严格受轮次、操作数和时间限制的 Repair。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel.models import KernelFinding, RepairDecision
from transflow.pdf_kernel.patch import patch_operation_hash

LOGGER = logging.getLogger("transflow.pdf_kernel.repair")


@dataclass(frozen=True, slots=True)
class RepairLimits:
    """冻结一次 Repair 的最大轮次、操作数、时长和最小字号。"""

    maximum_rounds: int
    maximum_operations: int
    maximum_duration_ms: int
    minimum_font_size: float

    def __post_init__(self) -> None:
        """拒绝零或负预算，防止配置产生无限循环或无效修复。"""

        if min(self.maximum_rounds, self.maximum_operations, self.maximum_duration_ms) < 1:
            raise ValueError("Repair 上限必须为正整数")
        if self.minimum_font_size <= 0:
            raise ValueError("最小字号必须为正数")


def shrink_font_patch(patch: PagePatch, scale: float, minimum_font_size: float) -> PagePatch:
    """机械缩小所有 replace_text 操作字号，并重新计算声明载荷哈希。"""

    if not 0 < scale < 1:
        raise ValueError("字号缩放比例必须位于 0 到 1 之间")
    operations: list[PatchOperation] = []
    for operation in patch.operations:
        if (
            operation.owner is None
            or operation.rect is None
            or operation.replacement_text is None
            or operation.font_id is None
            or operation.font_size is None
        ):
            operations.append(operation)
            continue
        font_size = max(minimum_font_size, round(operation.font_size * scale, 4))
        payload_hash = patch_operation_hash(
            owner=operation.owner,
            target_object_ids=operation.target_object_ids,
            rect=operation.rect,
            replacement_text=operation.replacement_text,
            font_id=operation.font_id,
            font_size=font_size,
        )
        operations.append(replace(operation, font_size=font_size, payload_hash=payload_hash))
    LOGGER.info("调用字号 Repair，意图=机械收缩文本 patch_id=%s", patch.patch_id)
    return replace(patch, operations=tuple(operations))


class BoundedRepairController:
    """每轮重新执行完整约束，并只接受硬 Finding 严格减少的候选。"""

    def __init__(self, limits: RepairLimits) -> None:
        """绑定冻结预算；控制器不读取页面分类或 Toolbox 路由。"""

        self._limits = limits

    def run(
        self,
        initial_ref: str,
        initial_findings: tuple[KernelFinding, ...],
        attempts: tuple[tuple[str, int, Callable[[], tuple[KernelFinding, ...]]], ...],
    ) -> RepairDecision:
        """按声明顺序评估候选，达到任何上限或无改善即稳定停止。"""

        started = time.monotonic_ns()
        selected_ref = initial_ref
        selected = initial_findings
        operations_used = 0
        no_improvement = 0
        outcome = "IRREPARABLE"
        for round_index, (candidate_ref, operation_count, evaluate) in enumerate(
            attempts,
            start=1,
        ):
            elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
            if (
                round_index > self._limits.maximum_rounds
                or operations_used + operation_count > self._limits.maximum_operations
                or elapsed_ms >= self._limits.maximum_duration_ms
            ):
                outcome = "BUDGET_EXHAUSTED"
                break
            operations_used += operation_count
            LOGGER.info(
                "调用 Repair 候选复检，意图=重新运行全部机械约束 round=%s ref=%s",
                round_index,
                candidate_ref,
            )
            candidate = evaluate()
            selected_hard = sum(item.blocking for item in selected)
            candidate_hard = sum(item.blocking for item in candidate)
            if candidate_hard < selected_hard:
                selected_ref = candidate_ref
                selected = candidate
                no_improvement = 0
                if candidate_hard == 0:
                    return RepairDecision(
                        "ACCEPTED",
                        True,
                        selected_ref,
                        round_index,
                        operations_used,
                        no_improvement,
                        tuple(item.code for item in selected),
                        "全部硬约束已通过",
                    )
            else:
                no_improvement += 1
                outcome = "NO_IMPROVEMENT"
                break
        return RepairDecision(
            outcome,
            False,
            selected_ref,
            min(len(attempts), self._limits.maximum_rounds),
            operations_used,
            no_improvement,
            tuple(item.code for item in selected),
            "Repair 已按硬上限停止",
        )


def main() -> int:
    """展示 Repair 控制器只消费结构化 Finding 和显式预算。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("BoundedRepairController 示例，意图=防止无限修复循环")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
