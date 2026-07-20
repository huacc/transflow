"""实现 run 私有、不可变且可验证的文件 ArtifactPort。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from transflow.adapters.filesystem.common import (
    atomic_write_json,
    ensure_within,
    inject_crash,
    load_json,
    require_safe_identifier,
    sha256_bytes,
    sha256_file,
)
from transflow.domain.artifacts import ArtifactPayload, ArtifactReference
from transflow.domain.delivery import ReleaseArtifactGuard, ReleaseSurface
from transflow.domain.errors import ErrorCode, PortCallError

LOGGER = logging.getLogger("transflow.adapters.filesystem.artifact_store")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_LABELS = frozenset(
    {"final", "diagnostic", "audit", "rebuildable-temp", "page", "preview", "report"}
)


class SharedFilesystemArtifactAdapter:
    """以内容哈希和原子 manifest 管理一个 Run 的不可变 Artifact。"""

    def __init__(self, run_root: Path, run_id: str) -> None:
        """固定真实 run 根并初始化空 Artifact manifest。"""

        require_safe_identifier(run_id, "run_id")
        self._run_root = run_root.resolve()
        self._run_id = run_id
        self._manifest_path = self._run_root / "job" / "artifact_manifest.json"
        self._pending_path = self._run_root / "job" / "pending_artifacts.json"
        self._final_manifest_path = self._run_root / "job" / "final_manifest.json"
        self._run_root.mkdir(parents=True, exist_ok=True)
        if not self._manifest_path.exists():
            atomic_write_json(
                self._manifest_path,
                {
                    "entries": {},
                    "run_id": run_id,
                    "schema_version": "transflow.artifact-manifest/v1",
                },
            )

    def _manifest(self) -> dict[str, Any]:
        """读取并验证当前 Run 的 Artifact manifest 身份。"""

        manifest = load_json(self._manifest_path)
        if manifest.get("run_id") != self._run_id:
            raise PortCallError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                False,
                "Artifact manifest Run 不匹配",
            )
        return manifest

    def _resolve_relative(self, relative_path: str) -> Path:
        """解析一个 run 相对 Artifact 路径并拒绝绝对路径和逃逸。"""

        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "Artifact 路径必须相对 Run",
            )
        try:
            return ensure_within(self._run_root / candidate, self._run_root)
        except Exception as error:
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "Artifact 路径越界",
            ) from error

    def _reference_from_entry(self, entry: dict[str, Any]) -> ArtifactReference:
        """把 manifest 条目恢复为领域 ArtifactReference。"""

        return ArtifactReference(
            artifact_id=entry["artifact_id"],
            media_type=entry["media_type"],
            content_hash=entry["content_hash"],
            size_bytes=entry["size_bytes"],
            relative_path=entry["relative_path"],
            label=entry["label"],
        )

    def put(self, payload: ArtifactPayload) -> ArtifactReference:
        """按内容地址生成默认审计路径并执行不可变写入。"""

        relative = f"artifacts/audit/{payload.artifact_id}-{payload.content_hash}.bin"
        return self.put_atomic(payload, relative, "audit")

    def put_atomic(
        self,
        payload: ArtifactPayload,
        relative_path: str,
        label: str,
        *,
        crash_at: str | None = None,
    ) -> ArtifactReference:
        """按 partial、rename、manifest 顺序写入不可变 Artifact。"""

        LOGGER.info(
            "调用 Artifact 原子写入，意图=发布可验证不可变产物 artifact_id=%s label=%s",
            payload.artifact_id,
            label,
        )
        require_safe_identifier(payload.artifact_id, "artifact_id")
        if label not in ALLOWED_LABELS:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "Artifact 标签不受支持")
        if sha256_bytes(payload.content) != payload.content_hash:
            raise PortCallError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                False,
                "Artifact 内容哈希不匹配",
            )
        final_path = self._resolve_relative(relative_path)
        partial_path = final_path.with_name(f"{final_path.name}.partial")
        manifest = self._manifest()
        existing = manifest["entries"].get(payload.artifact_id)
        if existing is not None:
            reference = self._reference_from_entry(existing)
            if (
                reference.content_hash == payload.content_hash
                and reference.relative_path == relative_path
                and self.verify(reference)
            ):
                return reference
            raise PortCallError(
                ErrorCode.ARTIFACT_IMMUTABLE_CONFLICT,
                False,
                "Artifact 身份已绑定其他内容或路径",
            )
        if final_path.exists():
            if sha256_file(final_path) != payload.content_hash:
                raise PortCallError(
                    ErrorCode.ARTIFACT_IMMUTABLE_CONFLICT,
                    False,
                    "目标不可变路径已有不同内容",
                )
        else:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                self._pending_path,
                {
                    "final_path": relative_path,
                    "partial_path": partial_path.relative_to(self._run_root).as_posix(),
                    "sha256": payload.content_hash,
                },
            )
            with partial_path.open("wb") as stream:
                stream.write(payload.content)
                stream.flush()
                os.fsync(stream.fileno())
            inject_crash(crash_at, "before_artifact_rename")
            partial_path.replace(final_path)
            inject_crash(crash_at, "after_artifact_rename")
        entry = {
            "artifact_id": payload.artifact_id,
            "content_hash": payload.content_hash,
            "label": label,
            "media_type": payload.media_type,
            "relative_path": relative_path,
            "size_bytes": len(payload.content),
        }
        manifest["entries"][payload.artifact_id] = entry
        atomic_write_json(self._manifest_path, manifest)
        inject_crash(crash_at, "after_artifact_manifest")
        if self._pending_path.exists():
            self._pending_path.unlink()
        reference = self._reference_from_entry(entry)
        if not self.verify(reference):
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "Artifact 写后校验失败")
        return reference

    def verify(self, reference: ArtifactReference) -> bool:
        """验证 manifest 元数据、真实文件大小和内容哈希完全一致。"""

        manifest = self._manifest()
        entry = manifest["entries"].get(reference.artifact_id)
        if entry is None or reference.relative_path is None:
            return False
        expected = self._reference_from_entry(entry)
        if expected != reference:
            return False
        path = self._resolve_relative(reference.relative_path)
        return (
            path.is_file()
            and path.stat().st_size == reference.size_bytes
            and sha256_file(path) == reference.content_hash
        )

    def get(self, artifact_id: str) -> bytes:
        """读取已登记且通过完整性校验的 Artifact 内容。"""

        entry = self._manifest()["entries"].get(artifact_id)
        if entry is None:
            raise PortCallError(ErrorCode.ARTIFACT_NOT_FOUND, False, "Artifact 不存在")
        reference = self._reference_from_entry(entry)
        if not self.verify(reference) or reference.relative_path is None:
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "Artifact 已损坏")
        return self._resolve_relative(reference.relative_path).read_bytes()

    def publish_final(
        self,
        reference: ArtifactReference,
        *,
        crash_at: str | None = None,
    ) -> None:
        """原子更新 standalone 最终发布指针，且只允许已验证 final Artifact。"""

        ReleaseArtifactGuard.assert_allowed(reference, ReleaseSurface.STANDALONE_FINAL)
        if reference.label != "final" or not self.verify(reference):
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "最终 Artifact 无效")
        inject_crash(crash_at, "before_final_manifest")
        atomic_write_json(
            self._final_manifest_path,
            {
                "artifact_id": reference.artifact_id,
                "content_hash": reference.content_hash,
                "relative_path": reference.relative_path,
                "run_id": self._run_id,
                "schema_version": "transflow.final-manifest/v1",
            },
        )
        inject_crash(crash_at, "after_final_manifest")

    def published_final(self) -> ArtifactReference | None:
        """返回当前发布权威指针，未发布时返回 ``None``。"""

        if not self._final_manifest_path.is_file():
            return None
        artifact_id = load_json(self._final_manifest_path)["artifact_id"]
        entry = self._manifest()["entries"].get(artifact_id)
        if entry is None:
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "最终指针悬空")
        reference = self._reference_from_entry(entry)
        if not self.verify(reference):
            raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "最终指针损坏")
        return reference

    def recover(self) -> dict[str, tuple[str, ...]]:
        """只清理 pending journal 登记的 partial，并报告未引用孤儿。"""

        cleaned: list[str] = []
        if self._pending_path.is_file():
            pending = load_json(self._pending_path)
            partial_relative = str(pending["partial_path"])
            partial = self._resolve_relative(partial_relative)
            if partial.is_file() and partial.name.endswith(".partial"):
                partial.unlink()
                cleaned.append(partial_relative)
            self._pending_path.unlink()
        return {"cleaned_partials": tuple(cleaned), "orphans": self.scan_orphans()}

    def scan_orphans(self) -> tuple[str, ...]:
        """列出未被 Artifact manifest 引用的文件，但不删除任何文件。"""

        referenced = {
            entry["relative_path"] for entry in self._manifest()["entries"].values()
        }
        artifact_root = self._run_root / "artifacts"
        if not artifact_root.is_dir():
            return ()
        candidates = {
            path.relative_to(self._run_root).as_posix()
            for path in artifact_root.rglob("*")
            if path.is_file() and not path.name.endswith(".partial")
        }
        return tuple(sorted(candidates - referenced))


def main() -> int:
    """记录 Artifact Adapter 的调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("SharedFilesystemArtifactAdapter 需绑定一个已验证的 run workspace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
