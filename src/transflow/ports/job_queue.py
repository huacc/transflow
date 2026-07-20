"""声明任务取得、控制读取和结果回写的 JobQueuePort。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from transflow.domain.jobs import ControlSignal, DocumentResult, JobSnapshot

LOGGER = logging.getLogger("transflow.ports.job_queue")


@runtime_checkable
class JobQueuePort(Protocol):
    """隔离不同任务来源，同时保持同一应用合同。"""

    def acquire(self) -> JobSnapshot | None:
        """幂等取得一个可处理任务；无任务时返回 ``None``。"""

        ...

    def read_control(self, job_id: str) -> ControlSignal:
        """读取指定 Job 的最新控制信号，不暴露队列表或传输协议。"""

        ...

    def publish_result(self, result: DocumentResult) -> None:
        """以 run_id 为幂等键发布最终结果；重复相同结果必须无副作用。"""

        ...


def main() -> int:
    """记录 JobQueuePort 的边界用途。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 Port 示例，意图=说明 JobQueuePort 只承担任务和控制边界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
