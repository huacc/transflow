"""定义完整 PDF 运行请求、Job 快照、控制信号和文档结果合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from transflow.domain.common import require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.states import DocumentOutcome, JobControlState

LOGGER = logging.getLogger("transflow.domain.jobs")


@dataclass(frozen=True, slots=True)
class DocumentRunRequest:
    """表示 Transflow 唯一生产入口接受的一份完整只读 PDF。"""

    source_pdf_path: str
    source_hash: str
    source_language: str
    target_language: str
    config_snapshot_hash: str
    job_id: str
    run_id: str

    def __post_init__(self) -> None:
        """拒绝列表、目录形态、非 PDF 路径和不稳定身份。"""

        source_path = require_non_empty(self.source_pdf_path, "source_pdf_path")
        if PurePath(source_path).suffix.casefold() != ".pdf":
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "DocumentRunRequest 只接受单个完整 PDF path",
            )
        require_sha256(self.source_hash, "source_hash")
        require_sha256(self.config_snapshot_hash, "config_snapshot_hash")
        for field_name in ("source_language", "target_language", "job_id", "run_id"):
            require_non_empty(getattr(self, field_name), field_name)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DocumentRunRequest:
        """从 JSON 字典恢复完整 PDF 请求并重新执行全部不变量。"""

        return cls(
            source_pdf_path=payload["source_pdf_path"],
            source_hash=payload["source_hash"],
            source_language=payload["source_language"],
            target_language=payload["target_language"],
            config_snapshot_hash=payload["config_snapshot_hash"],
            job_id=payload["job_id"],
            run_id=payload["run_id"],
        )


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """表示 JobQueuePort 交给 Application 的不可变任务快照。"""

    request: DocumentRunRequest
    control_state: JobControlState
    attempt_no: int
    checkpoint_version: int

    def __post_init__(self) -> None:
        """校验 attempt 和 checkpoint 版本非负。"""

        if self.attempt_no < 1 or self.checkpoint_version < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "JobSnapshot 版本字段无效")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobSnapshot:
        """从 JSON 字典恢复 Job 快照。"""

        return cls(
            request=DocumentRunRequest.from_dict(payload["request"]),
            control_state=JobControlState(payload["control_state"]),
            attempt_no=payload["attempt_no"],
            checkpoint_version=payload["checkpoint_version"],
        )


@dataclass(frozen=True, slots=True)
class ControlSignal:
    """表示 JobQueuePort 读取到的暂停、恢复、取消或运行控制快照。"""

    job_id: str
    state: JobControlState
    observed_checkpoint_version: int

    def __post_init__(self) -> None:
        """校验控制信号的 Job 身份和版本。"""

        require_non_empty(self.job_id, "job_id")
        if self.observed_checkpoint_version < 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "控制版本不得为负")


@dataclass(frozen=True, slots=True)
class DocumentResult:
    """表示状态和质量分离后的文档最终结果。"""

    run_id: str
    outcome: DocumentOutcome
    final_artifact_id: str | None
    degradation_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """保证成功类结果有 Artifact，流程失败不伪造产物。"""

        require_non_empty(self.run_id, "run_id")
        if self.outcome is DocumentOutcome.PROCESS_FAILED and self.final_artifact_id is not None:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "流程失败不得声明最终 Artifact")
        if self.outcome is not DocumentOutcome.PROCESS_FAILED:
            require_non_empty(self.final_artifact_id, "final_artifact_id")


def main() -> int:
    """展示完整 PDF 请求合同的最小构造方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    request = DocumentRunRequest(
        source_pdf_path="fixtures/report.pdf",
        source_hash="0" * 64,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="1" * 64,
        job_id="job-example",
        run_id="run-example",
    )
    LOGGER.info("调用请求示例，意图=展示完整 PDF 单输入合同 run_id=%s", request.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
