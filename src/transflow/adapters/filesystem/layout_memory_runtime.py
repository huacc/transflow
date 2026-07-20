"""实现 DocumentLayoutMemory 的 single-flight 发布、恢复、Context 绑定和 CAS。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.common import inject_crash
from transflow.domain.artifacts import ArtifactPayload, CheckpointRecord
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.layout_memory import DocumentLayoutMemory, DocumentLayoutMemoryRef
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import CheckpointCompatibility

LOGGER = logging.getLogger("transflow.adapters.filesystem.layout_memory_runtime")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_SCHEMA_HASH = hashlib.sha256(b"transflow.layout-memory-checkpoint/v1").hexdigest()


class LayoutMemoryBuildResultPort(Protocol):
    """描述文件运行时只需读取的 Builder 结果字段。"""

    @property
    def memory(self) -> DocumentLayoutMemory | None:
        """返回 READY 记忆；屏障未闭合时返回空。"""

        ...


class LayoutMemoryBuilderPort(Protocol):
    """隔离文件 Adapter 与应用层 Builder 具体实现。"""

    @property
    def build_count(self) -> int:
        """返回当前实例实际构建次数。"""

        ...

    def build(self, request: Any) -> LayoutMemoryBuildResultPort:
        """消费应用层传入的完整构建请求。"""

        ...


class DocumentLayoutMemoryRuntime:
    """在一个 run 内原子冻结唯一记忆，并拒绝版本混用或迟到 Worker 覆盖。"""

    def __init__(
        self,
        run_root: Path,
        run_id: str,
        builder: LayoutMemoryBuilderPort,
    ) -> None:
        """绑定注入的 run 工作区、复用文件 Adapter 并创建进程内 single-flight 锁。"""

        self._run_root = run_root.resolve()
        self._run_id = run_id
        self._builder = builder
        self._artifacts = SharedFilesystemArtifactAdapter(self._run_root, run_id)
        self._checkpoints = FilesystemCheckpointAdapter(self._run_root, run_id, self._artifacts)
        self._lock = Lock()
        self._reuse_count = 0

    @property
    def builder(self) -> LayoutMemoryBuilderPort:
        """暴露只读构建计数接口，供 Gate 证明 single-flight。"""

        return self._builder

    @property
    def reuse_count(self) -> int:
        """返回从已提交 Checkpoint 成功复用的次数。"""

        return self._reuse_count

    def prepare(
        self,
        request: Any,
        *,
        crash_at: str | None = None,
    ) -> DocumentLayoutMemoryRef:
        """优先恢复兼容记忆；否则在锁内构建、发布 Artifact、提交 Checkpoint 后返回。"""

        LOGGER.info("调用文档记忆运行时，意图=页面 ready 前冻结唯一引用 run_id=%s", self._run_id)
        with self._lock:
            recovered = self._recover_committed(request)
            if recovered is not None:
                self._reuse_count += 1
                return recovered
            result = self._builder.build(request)
            if result.memory is None:
                raise DomainContractError(
                    ErrorCode.DOCUMENT_NOT_FINALIZABLE, "全页事实/Route 屏障尚未闭合"
                )
            memory = result.memory
            content = memory.canonical_bytes
            payload = ArtifactPayload(
                artifact_id=f"document-layout-memory-{memory.memory_hash}",
                media_type="application/vnd.transflow.document-layout-memory+json",
                content=content,
                content_hash=memory.memory_hash,
            )
            relative_path = f"artifacts/audit/document-layout-memory/{memory.memory_hash}.json"
            reference = self._artifacts.put_atomic(payload, relative_path, "audit")
            inject_crash(crash_at, "after_memory_artifact")
            memory_ref = DocumentLayoutMemoryRef(
                memory_hash=memory.memory_hash,
                identity_hash=memory.identity.identity_hash,
                artifact_id=reference.artifact_id,
                relative_path=relative_path,
            )
            checkpoint_payload = self._checkpoint_payload(memory_ref)
            checkpoint = CheckpointRecord(
                run_id=self._run_id,
                version=1,
                state_hash=hashlib.sha256(checkpoint_payload).hexdigest(),
                payload=checkpoint_payload,
                compatibility=self._compatibility(request),
                artifact_refs=(reference,),
            )
            self._checkpoints.complete_run(checkpoint, 0, crash_at=crash_at)
            return memory_ref

    def load_readonly(self, memory_ref: DocumentLayoutMemoryRef) -> DocumentLayoutMemory:
        """按内容哈希加载本进程冻结副本，不共享 PyMuPDF 或可变 Python 对象。"""

        LOGGER.info(
            "调用文档记忆加载，意图=按 hash 创建本进程只读副本 hash=%s", memory_ref.memory_hash
        )
        content = self._artifacts.get(memory_ref.artifact_id)
        if hashlib.sha256(content).hexdigest() != memory_ref.memory_hash:
            raise PortCallError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "文档记忆 Artifact hash 漂移"
            )
        payload = json.loads(content.decode("utf-8"))
        payload["memory_hash"] = memory_ref.memory_hash
        memory = DocumentLayoutMemory.from_dict(payload)
        if memory.identity.identity_hash != memory_ref.identity_hash:
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "文档记忆 identity 漂移")
        return memory

    def bind_page_contexts(
        self,
        contexts: tuple[PageExecutionContext, ...],
        memory_ref: DocumentLayoutMemoryRef,
    ) -> tuple[PageExecutionContext, ...]:
        """在页面放行前把同一权威引用复制进每个不可变 PageContext。"""

        self.assert_authoritative(memory_ref)
        if any(
            context.run_id != self._run_id
            or (
                context.document_layout_memory_ref is not None
                and context.document_layout_memory_ref != memory_ref
            )
            for context in contexts
        ):
            raise PortCallError(
                ErrorCode.CHECKPOINT_CONFLICT, False, "PageContext Run 或 memory ref 混用"
            )
        return tuple(
            replace(context, document_layout_memory_ref=memory_ref) for context in contexts
        )

    def assert_authoritative(self, proposed: DocumentLayoutMemoryRef) -> None:
        """以 Checkpoint 做 CAS，拒绝迟到 Worker 提交不同 memory ref。"""

        checkpoint = self._checkpoints.load_run()
        if checkpoint is None:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "文档记忆尚未提交 Checkpoint"
            )
        authoritative = self._ref_from_checkpoint(checkpoint.payload)
        if authoritative != proposed:
            raise PortCallError(
                ErrorCode.CHECKPOINT_CONFLICT, False, "迟到 Worker memory ref 被 CAS 拒绝"
            )

    def recover_filesystem(self) -> dict[str, tuple[str, ...]]:
        """清理已登记 partial 并报告未引用文件，绝不把孤儿直接暴露给页面。"""

        LOGGER.info("调用文档记忆恢复，意图=清理半写并审计孤儿 run_id=%s", self._run_id)
        artifact_result = self._artifacts.recover()
        checkpoint_result = self._checkpoints.recover()
        return {
            "cleaned_partials": tuple(
                sorted(
                    (*artifact_result["cleaned_partials"], *checkpoint_result["cleaned_partials"])
                )
            ),
            "orphans": tuple(sorted((*artifact_result["orphans"], *checkpoint_result["orphans"]))),
        }

    def _recover_committed(
        self,
        request: Any,
    ) -> DocumentLayoutMemoryRef | None:
        """只复用兼容且 Artifact/identity 双重验证通过的已提交 Checkpoint。"""

        checkpoint = self._checkpoints.load_run()
        if checkpoint is None:
            return None
        if checkpoint.compatibility != self._compatibility(request):
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "文档记忆恢复指纹变化")
        memory_ref = self._ref_from_checkpoint(checkpoint.payload)
        memory = self.load_readonly(memory_ref)
        changed = memory.identity.changed_fields(request.identity)
        if changed:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                f"文档记忆完整身份变化 fields={','.join(changed)}",
            )
        return memory_ref

    @staticmethod
    def _checkpoint_payload(memory_ref: DocumentLayoutMemoryRef) -> bytes:
        """规范编码 Checkpoint 中唯一允许的文档记忆引用。"""

        return json.dumps(
            {
                "schema_version": "transflow.layout-memory-checkpoint/v1",
                "memory_ref": {
                    "memory_hash": memory_ref.memory_hash,
                    "identity_hash": memory_ref.identity_hash,
                    "artifact_id": memory_ref.artifact_id,
                    "relative_path": memory_ref.relative_path,
                    "schema_version": memory_ref.schema_version,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _ref_from_checkpoint(payload: bytes) -> DocumentLayoutMemoryRef:
        """从已验证 Checkpoint 恢复严格内容寻址引用。"""

        decoded = json.loads(payload.decode("utf-8"))
        if decoded.get("schema_version") != "transflow.layout-memory-checkpoint/v1":
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "文档记忆 Checkpoint Schema 漂移"
            )
        return DocumentLayoutMemoryRef(**decoded["memory_ref"])

    @staticmethod
    def _compatibility(request: Any) -> CheckpointCompatibility:
        """映射到现有 G3 Checkpoint 五字段兼容合同，其余指纹再由完整 identity 核对。"""

        identity = request.identity
        return CheckpointCompatibility(
            source_hash=identity.source_hash,
            config_hash=identity.config_hash,
            font_hash=identity.font_hash,
            toolbox_catalog_hash=identity.catalog_hash,
            schema_hash=CHECKPOINT_SCHEMA_HASH,
        )


def main() -> int:
    """记录运行时必须绑定调用方注入的 run 工作区。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("DocumentLayoutMemoryRuntime 示例，意图=绑定独立 run 后再构建")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
