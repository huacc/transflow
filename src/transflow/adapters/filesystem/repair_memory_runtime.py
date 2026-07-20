"""实现 P9B 每轮候选 Artifact、页记忆 Checkpoint、CAS 与崩溃恢复。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.adapters.filesystem.common import inject_crash
from transflow.domain.artifacts import ArtifactPayload, ArtifactReference, CheckpointRecord
from transflow.domain.common import canonical_json_bytes
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.repair_memory import (
    PageRepairMemory,
    RepairAttemptStatus,
    RepairMemoryIdentity,
    canonical_page_memory_bytes,
)
from transflow.domain.states import CheckpointCompatibility

LOGGER = logging.getLogger("transflow.adapters.filesystem.repair_memory_runtime")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent


class PageRepairMemoryRuntime:
    """把一个页面的动作证据和 append-only 账本提交到既有 G3 文件协议。"""

    def __init__(self, run_root: Path, identity: RepairMemoryIdentity) -> None:
        """绑定调用方提供的受控 run 根与完整页记忆身份。"""

        self._run_root = run_root.resolve()
        self._identity = identity
        self._artifacts = SharedFilesystemArtifactAdapter(self._run_root, identity.run_id)
        self._checkpoints = FilesystemCheckpointAdapter(
            self._run_root,
            identity.run_id,
            self._artifacts,
        )

    def put_candidate(
        self,
        action_key: str,
        content: bytes,
        *,
        crash_at: str | None = None,
    ) -> ArtifactReference:
        """按 action identity 幂等写入真实 PDF 候选并返回受控相对引用。"""

        LOGGER.info(
            "调用修复候选写入，意图=先持久化再提交页记忆 page_no=%s action=%s",
            self._identity.page_no,
            action_key,
        )
        content_hash = hashlib.sha256(content).hexdigest()
        artifact_id = f"repair-candidate-{action_key}"
        relative_path = (
            f"pages/{self._identity.page_no:04d}/repair/{action_key}/candidate.pdf"
        )
        return self._artifacts.put_atomic(
            ArtifactPayload(artifact_id, "application/pdf", content, content_hash),
            relative_path,
            "audit",
            crash_at=crash_at,
        )

    def put_candidate_zero(self, content: bytes) -> ArtifactReference:
        """写入 candidate-0 真实 PDF，作为多轮比较的不可变基线。"""

        content_hash = hashlib.sha256(content).hexdigest()
        return self._artifacts.put_atomic(
            ArtifactPayload(
                f"repair-candidate-zero-p{self._identity.page_no:04d}",
                "application/pdf",
                content,
                content_hash,
            ),
            f"pages/{self._identity.page_no:04d}/repair/candidate-0.pdf",
            "audit",
        )

    def commit(
        self,
        memory: PageRepairMemory,
        *,
        crash_at: str | None = None,
    ) -> None:
        """校验身份和候选后以当前版本 CAS 提交页记忆，不接受迟到 Worker。"""

        LOGGER.info(
            "调用页修复 Checkpoint，意图=提交权威失败账本 page_no=%s attempts=%s",
            self._identity.page_no,
            len(memory.attempts),
        )
        changed = self._identity.changed_fields(memory.identity)
        if changed:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                f"页记忆提交身份变化 fields={','.join(changed)}",
            )
        candidate_refs = self._candidate_references(memory)
        self._write_failure_evidence(memory)
        payload = canonical_page_memory_bytes(memory)
        memory_ref = self._artifacts.put_atomic(
            ArtifactPayload(
                artifact_id=f"page-repair-memory-{memory.memory_hash}",
                media_type="application/vnd.transflow.page-repair-memory+json",
                content=payload,
                content_hash=hashlib.sha256(payload).hexdigest(),
            ),
            (
                f"pages/{self._identity.page_no:04d}/repair/memory/"
                f"{memory.memory_hash}.json"
            ),
            "audit",
        )
        inject_crash(crash_at, "after_page_memory_artifact")
        current = self._checkpoints.load_page(self._identity.page_no)
        if current is not None:
            restored = PageRepairMemory.from_dict(json.loads(current.payload.decode("utf-8")))
            if restored.memory_hash == memory.memory_hash:
                return
            changed = self._identity.changed_fields(restored.identity)
            if changed or restored.identity.run_token != self._identity.run_token:
                raise PortCallError(
                    ErrorCode.CHECKPOINT_CONFLICT,
                    False,
                    "迟到 Worker 的 run_token 或页记忆身份被 CAS 拒绝",
                )
        expected_version = current.version if current is not None else 0
        record = CheckpointRecord(
            run_id=self._identity.run_id,
            version=expected_version + 1,
            state_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            compatibility=self._compatibility(),
            artifact_refs=(*candidate_refs, memory_ref),
        )
        self._checkpoints.commit_page(
            self._identity.page_no,
            record,
            expected_version,
            crash_at=crash_at,
        )

    def restore(self, expected_identity: RepairMemoryIdentity) -> PageRepairMemory | None:
        """只恢复同 run 完整兼容身份的页记忆，并复核全部已物化候选。"""

        LOGGER.info(
            "调用页修复恢复，意图=读取同 run 已提交动作 page_no=%s",
            expected_identity.page_no,
        )
        requested_changes = self._identity.changed_fields(expected_identity)
        if requested_changes:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                f"恢复请求身份变化 fields={','.join(requested_changes)}",
            )
        checkpoint = self._checkpoints.load_page(expected_identity.page_no)
        if checkpoint is None:
            return None
        if checkpoint.compatibility != self._compatibility(expected_identity):
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "页记忆恢复指纹变化")
        memory = PageRepairMemory.from_dict(json.loads(checkpoint.payload.decode("utf-8")))
        changed = memory.identity.changed_fields(expected_identity)
        if changed:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                f"陈旧页记忆拒绝 fields={','.join(changed)}",
            )
        self._candidate_references(memory)
        return memory

    def recover_filesystem(self) -> dict[str, tuple[str, ...]]:
        """按既有 G3 journal 清理 partial，并报告未引用而不擅自发布。"""

        artifact_result = self._artifacts.recover()
        checkpoint_result = self._checkpoints.recover()
        return {
            "cleaned_partials": tuple(
                sorted(
                    (*artifact_result["cleaned_partials"], *checkpoint_result["cleaned_partials"])
                )
            ),
            "orphans": tuple(
                sorted((*artifact_result["orphans"], *checkpoint_result["orphans"]))
            ),
        }

    def _candidate_references(
        self,
        memory: PageRepairMemory,
    ) -> tuple[ArtifactReference, ...]:
        """复核所有成功物化 Attempt 的不可变候选内容与相对路径。"""

        references: list[ArtifactReference] = []
        for attempt in memory.attempts:
            if attempt.status is RepairAttemptStatus.MATERIALIZATION_FAILED:
                continue
            artifact_id = f"repair-candidate-{attempt.proposal.action_key}"
            content = self._artifacts.get(artifact_id)
            content_hash = hashlib.sha256(content).hexdigest()
            if (
                attempt.candidate_artifact_ref is None
                or attempt.evidence_hash != content_hash
            ):
                raise PortCallError(
                    ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                    False,
                    "修复候选内容或引用与 Attempt 不一致",
                )
            references.append(
                ArtifactReference(
                    artifact_id=artifact_id,
                    media_type="application/pdf",
                    content_hash=content_hash,
                    size_bytes=len(content),
                    relative_path=attempt.candidate_artifact_ref,
                    label="audit",
                )
            )
        return tuple(references)

    def _write_failure_evidence(self, memory: PageRepairMemory) -> None:
        """为每个真实物化失败动作幂等写入独立 JSON 证据，不伪造 candidate ref。"""

        for attempt in memory.attempts:
            if attempt.status is not RepairAttemptStatus.MATERIALIZATION_FAILED:
                continue
            content = canonical_json_bytes(
                {
                    "action_key": attempt.proposal.action_key,
                    "error_code": attempt.error_code,
                    "evidence_hash": attempt.evidence_hash,
                    "schema_version": "transflow.repair-materialization-failure/v1",
                }
            )
            action_key = attempt.proposal.action_key
            self._artifacts.put_atomic(
                ArtifactPayload(
                    artifact_id=f"repair-failure-{action_key}",
                    media_type="application/json",
                    content=content,
                    content_hash=hashlib.sha256(content).hexdigest(),
                ),
                (
                    f"pages/{self._identity.page_no:04d}/repair/{action_key}/"
                    "materialization_failed.json"
                ),
                "audit",
            )

    def _compatibility(
        self,
        identity: RepairMemoryIdentity | None = None,
    ) -> CheckpointCompatibility:
        """把 P9B 完整身份映射到既有五字段合同，其余字段由 payload 再校验。"""

        current = identity or self._identity
        return CheckpointCompatibility(
            source_hash=current.source_hash,
            config_hash=current.config_hash,
            font_hash=current.implementation_hash,
            toolbox_catalog_hash=current.atom_catalog_hash,
            schema_hash=current.schema_hash,
        )


def main() -> int:
    """记录页修复运行时必须绑定调用方提供的独立 run 工作区。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PageRepairMemoryRuntime 示例，意图=复用 G3 Artifact/Checkpoint 协议")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
