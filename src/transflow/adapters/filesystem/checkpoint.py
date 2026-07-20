"""实现版本单调、原子 manifest 和可恢复的 FilesystemCheckpointAdapter。"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.common import (
    atomic_write_json,
    ensure_within,
    inject_crash,
    load_json,
    require_safe_identifier,
    sha256_bytes,
    sha256_file,
)
from transflow.domain.artifacts import ArtifactReference, CheckpointRecord
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.states import CheckpointCompatibility

LOGGER = logging.getLogger("transflow.adapters.filesystem.checkpoint")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent


class FilesystemCheckpointAdapter:
    """以一个 Run 的原子 JSON manifest 实现 CheckpointPort。"""

    def __init__(
        self,
        run_root: Path,
        run_id: str,
        artifact_store: SharedFilesystemArtifactAdapter | None = None,
    ) -> None:
        """绑定真实 run 根、身份及可选 Artifact 完整性检查器。"""

        require_safe_identifier(run_id, "run_id")
        self._run_root = run_root.resolve()
        self._run_id = run_id
        self._artifact_store = artifact_store
        self._manifest_path = self._run_root / "job" / "checkpoint_manifest.json"
        self._pending_path = self._run_root / "job" / "pending_checkpoints.json"
        self._run_root.mkdir(parents=True, exist_ok=True)
        if not self._manifest_path.exists():
            atomic_write_json(
                self._manifest_path,
                {
                    "pages": {},
                    "run": None,
                    "run_id": run_id,
                    "schema_version": "transflow.checkpoint-manifest/v1",
                },
            )

    def _manifest(self) -> dict[str, Any]:
        """读取并校验 Checkpoint manifest 的 Run 身份。"""

        manifest = load_json(self._manifest_path)
        if manifest.get("run_id") != self._run_id:
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "Checkpoint Run 不匹配")
        return manifest

    def resolve_run_relative(self, relative_path: str, *, must_exist: bool = False) -> Path:
        """解析受控 Run 相对路径并拒绝逃逸及外部重解析目标。"""

        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "Checkpoint 路径必须相对 Run",
            )
        try:
            return ensure_within(
                self._run_root / candidate,
                self._run_root,
                must_exist=must_exist,
            )
        except Exception as error:
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "Checkpoint 路径越界",
            ) from error

    def _record_payload(self, record: CheckpointRecord) -> dict[str, Any]:
        """把 CheckpointRecord 转为可跨进程恢复的 JSON 内容。"""

        compatibility = record.compatibility
        return {
            "artifact_refs": [
                {
                    "artifact_id": item.artifact_id,
                    "content_hash": item.content_hash,
                    "label": item.label,
                    "media_type": item.media_type,
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                }
                for item in record.artifact_refs
            ],
            "compatibility": {
                "config_hash": compatibility.config_hash,
                "font_hash": compatibility.font_hash,
                "schema_hash": compatibility.schema_hash,
                "source_hash": compatibility.source_hash,
                "toolbox_catalog_hash": compatibility.toolbox_catalog_hash,
            },
            "payload_base64": base64.b64encode(record.payload).decode("ascii"),
            "run_id": record.run_id,
            "state_hash": record.state_hash,
            "version": record.version,
        }

    def _record_from_file(self, path: Path) -> CheckpointRecord:
        """从已验证 JSON 文件恢复领域 CheckpointRecord。"""

        payload = load_json(path)
        references = tuple(ArtifactReference(**item) for item in payload["artifact_refs"])
        return CheckpointRecord(
            run_id=payload["run_id"],
            version=payload["version"],
            state_hash=payload["state_hash"],
            payload=base64.b64decode(payload["payload_base64"], validate=True),
            compatibility=CheckpointCompatibility(**payload["compatibility"]),
            artifact_refs=references,
        )

    def _validate_record(self, record: CheckpointRecord) -> None:
        """验证 Run、payload 哈希和全部 Artifact 引用后才允许提交。"""

        if record.run_id != self._run_id or sha256_bytes(record.payload) != record.state_hash:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                "Checkpoint 身份或哈希无效",
            )
        if record.artifact_refs and self._artifact_store is None:
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "缺少 Artifact 校验器")
        if self._artifact_store is not None:
            for reference in record.artifact_refs:
                if not self._artifact_store.verify(reference):
                    raise PortCallError(
                        ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                        False,
                        f"Checkpoint 引用无效 artifact_id={reference.artifact_id}",
                    )

    def _load_entry(self, entry: dict[str, Any] | None) -> CheckpointRecord | None:
        """读取 manifest 指向的权威 Checkpoint 并复核双重哈希。"""

        if entry is None:
            return None
        path = self.resolve_run_relative(entry["relative_path"], must_exist=True)
        if sha256_file(path) != entry["file_hash"]:
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "Checkpoint 文件哈希错误")
        record = self._record_from_file(path)
        self._validate_record(record)
        if record.version != entry["version"] or record.state_hash != entry["state_hash"]:
            raise PortCallError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                False,
                "Checkpoint manifest 漂移",
            )
        return record

    def load(self, run_id: str) -> CheckpointRecord | None:
        """读取 Run 级最新权威 Checkpoint。"""

        if run_id != self._run_id:
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "请求 Run 不匹配")
        return self._load_entry(self._manifest()["run"])

    def load_run(self) -> CheckpointRecord | None:
        """读取当前 Adapter 绑定 Run 的最新权威 Checkpoint。"""

        return self.load(self._run_id)

    def load_page(self, page_no: int) -> CheckpointRecord | None:
        """读取指定页最新权威 Checkpoint。"""

        if page_no < 0:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "page_no 不得为负")
        return self._load_entry(self._manifest()["pages"].get(str(page_no)))

    def save(self, record: CheckpointRecord, expected_version: int) -> CheckpointRecord:
        """提交 Run 级 Checkpoint。"""

        return self._commit("run", None, record, expected_version)

    def commit_page(
        self,
        page_no: int,
        record: CheckpointRecord,
        expected_version: int,
        *,
        crash_at: str | None = None,
    ) -> CheckpointRecord:
        """提交页级 Checkpoint，并提供协议边界故障注入点。"""

        if page_no < 0:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "page_no 不得为负")
        return self._commit("page", page_no, record, expected_version, crash_at=crash_at)

    def complete_run(
        self,
        record: CheckpointRecord,
        expected_version: int,
        *,
        crash_at: str | None = None,
    ) -> CheckpointRecord:
        """提交 Run 级完成安全点，供后续文档最终化调用。"""

        return self._commit("run", None, record, expected_version, crash_at=crash_at)

    def _commit(
        self,
        scope: str,
        page_no: int | None,
        record: CheckpointRecord,
        expected_version: int,
        *,
        crash_at: str | None = None,
    ) -> CheckpointRecord:
        """执行验证、partial、rename 和 manifest 的统一提交协议。"""

        LOGGER.info(
            "调用 Checkpoint 提交，意图=推进单调安全点 scope=%s page_no=%s version=%s",
            scope,
            page_no,
            record.version,
        )
        self._validate_record(record)
        manifest = self._manifest()
        current_entry = manifest["run"] if scope == "run" else manifest["pages"].get(str(page_no))
        current = self._load_entry(current_entry)
        actual_version = current.version if current else 0
        if current is not None and record.version == current.version:
            if record == current:
                return current
            raise PortCallError(ErrorCode.CHECKPOINT_CONFLICT, False, "同版本 Checkpoint 内容分叉")
        if expected_version != actual_version or record.version <= actual_version:
            raise PortCallError(
                ErrorCode.CHECKPOINT_CONFLICT,
                False,
                "Checkpoint 版本倒退或期望版本冲突",
            )
        scope_directory = (
            "job/checkpoints" if scope == "run" else f"pages/{page_no:04d}/checkpoints"
        )
        relative_path = f"{scope_directory}/v{record.version}-{record.state_hash}.json"
        final_path = self.resolve_run_relative(relative_path)
        partial_path = final_path.with_name(f"{final_path.name}.partial")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self._record_payload(record)
        content = (
            json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        atomic_write_json(
            self._pending_path,
            {
                "final_path": relative_path,
                "partial_path": partial_path.relative_to(self._run_root).as_posix(),
                "sha256": sha256_bytes(content),
            },
        )
        with partial_path.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        inject_crash(crash_at, "before_checkpoint_rename")
        partial_path.replace(final_path)
        inject_crash(crash_at, "after_checkpoint_rename")
        entry = {
            "file_hash": sha256_file(final_path),
            "relative_path": relative_path,
            "state_hash": record.state_hash,
            "version": record.version,
        }
        if scope == "run":
            manifest["run"] = entry
        else:
            manifest["pages"][str(page_no)] = entry
        atomic_write_json(self._manifest_path, manifest)
        inject_crash(crash_at, "after_checkpoint_manifest")
        if self._pending_path.exists():
            self._pending_path.unlink()
        loaded = self._load_entry(entry)
        if loaded is None:
            raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "Checkpoint 写后丢失")
        return loaded

    def recover(self) -> dict[str, tuple[str, ...]]:
        """清理已登记 partial、验证权威项并报告未引用孤儿。"""

        cleaned: list[str] = []
        if self._pending_path.is_file():
            pending = load_json(self._pending_path)
            partial_relative = str(pending["partial_path"])
            partial = self.resolve_run_relative(partial_relative)
            if partial.is_file() and partial.name.endswith(".partial"):
                partial.unlink()
                cleaned.append(partial_relative)
            self._pending_path.unlink()
        manifest = self._manifest()
        self._load_entry(manifest["run"])
        for entry in manifest["pages"].values():
            self._load_entry(entry)
        return {"cleaned_partials": tuple(cleaned), "orphans": self.scan_orphans()}

    def scan_orphans(self) -> tuple[str, ...]:
        """列出未被 Checkpoint manifest 引用的 checkpoint 文件且不删除。"""

        manifest = self._manifest()
        referenced = {
            entry["relative_path"]
            for entry in ([manifest["run"]] if manifest["run"] else [])
        }
        referenced.update(entry["relative_path"] for entry in manifest["pages"].values())
        candidates = {
            path.relative_to(self._run_root).as_posix()
            for path in self._run_root.rglob("*.json")
            if "checkpoints" in path.parts and not path.name.endswith(".partial")
        }
        return tuple(sorted(candidates - referenced))


def main() -> int:
    """记录文件 Checkpoint Adapter 的调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("FilesystemCheckpointAdapter 需绑定一个已验证的 run workspace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
