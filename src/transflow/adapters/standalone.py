"""实现只接受一份完整 PDF 的 StandaloneRunAdapter。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from transflow.adapters.filesystem.common import atomic_write_json, ensure_within, sha256_file
from transflow.domain.common import content_sha256, json_ready, require_non_empty
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import ControlSignal, DocumentResult, DocumentRunRequest, JobSnapshot
from transflow.domain.states import JobControlState

LOGGER = logging.getLogger("transflow.adapters.standalone")
ADAPTERS_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class StandaloneRun:
    """返回已验证请求及其唯一私有工作区。"""

    request: DocumentRunRequest
    workspace: Path


class StandaloneRunAdapter:
    """把开发/测试提交转换为 JobQueuePort 可取得的独立 Run。"""

    def __init__(self, workspace: Path, source_roots: tuple[Path, ...]) -> None:
        """保存由 composition root 提供的集中配置值并初始化待运行队列。"""

        if not source_roots:
            raise ValueError("Standalone 至少需要一个允许源目录")
        self._workspace = workspace.resolve()
        self._source_roots = tuple(root.resolve() for root in source_roots)
        self._queue: list[JobSnapshot] = []
        self._states: dict[str, JobControlState] = {}
        self._results: dict[str, DocumentResult] = {}

    def _validate_source(self, source_pdf: object) -> Path:
        """验证输入形态、允许根、普通文件、PDF 签名和可打开性。"""

        if isinstance(source_pdf, list | tuple) or not isinstance(source_pdf, str | Path):
            raise DomainContractError(ErrorCode.INPUT_SHAPE_INVALID, "只接受单个 PDF 路径")
        candidate = Path(source_pdf)
        if not candidate.is_absolute():
            raise DomainContractError(ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT, "源路径必须是绝对路径")
        resolved: Path | None = None
        for allowed_root in self._source_roots:
            try:
                resolved = ensure_within(candidate, allowed_root, must_exist=True)
                break
            except DomainContractError:
                continue
        if resolved is None:
            raise DomainContractError(ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT, "源文件越出全部允许根")
        if not resolved.is_file():
            raise DomainContractError(ErrorCode.SOURCE_NOT_REGULAR_FILE, "源路径必须是普通文件")
        if resolved.suffix.casefold() != ".pdf" or resolved.stat().st_size == 0:
            raise DomainContractError(ErrorCode.SOURCE_UNSUPPORTED, "扩展名或文件大小不支持")
        try:
            with resolved.open("rb") as stream:
                signature = stream.read(5)
            if signature != b"%PDF-":
                raise DomainContractError(ErrorCode.SOURCE_UNSUPPORTED, "PDF 文件签名无效")
            with pymupdf.open(resolved) as document:
                if document.needs_pass or document.page_count < 1:
                    raise DomainContractError(ErrorCode.SOURCE_UNSUPPORTED, "PDF 加密或没有页面")
                document.load_page(0)
        except DomainContractError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise DomainContractError(
                ErrorCode.SOURCE_NOT_READABLE,
                type(error).__name__,
            ) from error
        return resolved

    def _validate_submission_fields(
        self,
        source_language: object,
        target_language: object,
        config_snapshot: object,
    ) -> tuple[str, str, dict[str, Any]]:
        """校验语言和无秘密 JSON 配置快照的输入类型。"""

        source = require_non_empty(source_language, "source_language")
        target = require_non_empty(target_language, "target_language")
        if not isinstance(config_snapshot, dict) or not config_snapshot:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "config_snapshot 必须是非空对象")
        try:
            content_sha256(config_snapshot)
        except (TypeError, ValueError) as error:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "config_snapshot 不是 JSON 值",
            ) from error
        return source, target, config_snapshot

    def submit(
        self,
        source_pdf: object,
        source_language: object,
        target_language: object,
        config_snapshot: object,
    ) -> StandaloneRun:
        """完成全部校验后创建独立 run workspace 和可取得任务。"""

        LOGGER.info("调用 Standalone 提交，意图=创建单完整 PDF 的独立 Transflow run")
        source_path = self._validate_source(source_pdf)
        source, target, snapshot = self._validate_submission_fields(
            source_language,
            target_language,
            config_snapshot,
        )
        source_hash = sha256_file(source_path)
        snapshot_hash = content_sha256(snapshot)
        job_id = f"standalone-{uuid.uuid4().hex}"
        run_id = uuid.uuid4().hex
        request = DocumentRunRequest(
            source_pdf_path=str(source_path),
            source_hash=source_hash,
            source_language=source,
            target_language=target,
            config_snapshot_hash=snapshot_hash,
            job_id=job_id,
            run_id=run_id,
        )
        workspace = self._workspace / "standalone-job" / job_id / "transflow" / run_id
        manifest_path = workspace / "job" / "run_manifest.json"
        atomic_write_json(
            manifest_path,
            {
                "config_snapshot": snapshot,
                "request": json_ready(request),
                "schema_version": "transflow.standalone-run/v1",
            },
        )
        self._queue.append(JobSnapshot(request, JobControlState.QUEUED, 1, 0))
        self._states[job_id] = JobControlState.QUEUED
        LOGGER.info("Standalone run 已创建 job_id=%s run_id=%s", job_id, run_id)
        return StandaloneRun(request, workspace)

    def acquire(self) -> JobSnapshot | None:
        """取得最早提交的任务并把控制状态推进到 RUNNING。"""

        if not self._queue:
            return None
        snapshot = self._queue.pop(0)
        self._states[snapshot.request.job_id] = JobControlState.RUNNING
        return JobSnapshot(
            snapshot.request,
            JobControlState.RUNNING,
            snapshot.attempt_no,
            snapshot.checkpoint_version,
        )

    def read_control(self, job_id: str) -> ControlSignal:
        """读取独立 run 的进程内控制状态。"""

        try:
            state = self._states[job_id]
        except KeyError as error:
            raise PortCallError(
                ErrorCode.PORT_UNAVAILABLE,
                False,
                "Standalone Job 不存在",
            ) from error
        return ControlSignal(job_id, state, 0)

    def publish_result(self, result: DocumentResult) -> None:
        """按 run_id 幂等保存最终结果并拒绝同身份分叉。"""

        existing = self._results.get(result.run_id)
        if existing is not None and existing != result:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "Standalone 结果冲突")
        self._results[result.run_id] = result


def main() -> int:
    """记录 Standalone Adapter 只接受完整 PDF 的调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("StandaloneRunAdapter 示例需要由集中 RuntimeConfig 和完整 PDF 路径调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
