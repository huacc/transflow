"""定义 P4 文档编排、页面处理和恢复使用的应用层值对象。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from transflow.domain.artifacts import ArtifactPayload, ArtifactReference, CheckpointRecord
from transflow.domain.jobs import DocumentResult
from transflow.domain.pages import PageExecutionContext, PageOutcome
from transflow.domain.toolbox import PagePatch
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.patch import PatchApplicationResult
from transflow.pdf_kernel.preservation import PreservationPreflightResult, PreservationResult

LOGGER = logging.getLogger("transflow.application.contracts")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


class AtomicArtifactStore(Protocol):
    """描述 P4 在既有 ArtifactPort 之上使用的 standalone 原子发布能力。"""

    def put_atomic(
        self,
        payload: ArtifactPayload,
        relative_path: str,
        label: str,
        *,
        crash_at: str | None = None,
    ) -> ArtifactReference:
        """原子写入指定 Run 相对路径并返回不可变引用。"""

        ...

    def publish_final(
        self,
        reference: ArtifactReference,
        *,
        crash_at: str | None = None,
    ) -> None:
        """原子提交 standalone 最终 Artifact 权威指针。"""

        ...

    def published_final(self) -> ArtifactReference | None:
        """返回已发布且通过完整性校验的最终 Artifact。"""

        ...

    def get(self, artifact_id: str) -> bytes:
        """读取已登记且通过完整性校验的 Artifact 内容。"""

        ...

    def recover(self) -> dict[str, tuple[str, ...]]:
        """恢复已登记的半写文件并报告孤儿。"""

        ...


class PageCheckpointStore(Protocol):
    """描述 P4 在既有 CheckpointPort 之上使用的页级安全点能力。"""

    def load_page(self, page_no: int) -> CheckpointRecord | None:
        """读取指定 1-based 页面最新的权威 Checkpoint。"""

        ...

    def commit_page(
        self,
        page_no: int,
        record: CheckpointRecord,
        expected_version: int,
        *,
        crash_at: str | None = None,
    ) -> CheckpointRecord:
        """按期望版本提交页级安全点。"""

        ...


class PageExecutionPipeline(Protocol):
    """描述 DocumentCoordinator 可调用的页面终态流水线。"""

    def execute(
        self,
        source_path: Path,
        page: EnumeratedPage,
        route: str,
    ) -> ProcessedPage:
        """把一个已枚举页面收敛为唯一终态。"""

        ...


@dataclass(frozen=True, slots=True)
class EnumeratedPage:
    """表示预检后按原始顺序建立的稳定页面计划输入。"""

    context: PageExecutionContext
    facts: ExtractedPageFacts


@dataclass(frozen=True, slots=True)
class ProcessedPage:
    """表示页面终态、批准 Patch、预览和链路证据的聚合结果。"""

    page_no: int
    route: str
    outcome: PageOutcome
    patch: PagePatch | None
    preview: ArtifactReference | None
    unit_ids: tuple[str, ...]
    translated_unit_ids: tuple[str, ...]
    application: PatchApplicationResult | None
    resumed: bool = False
    toolbox_id: str | None = None
    toolbox_version: str | None = None
    catalog_hash: str | None = None
    evidence_attestation_hash: str | None = None
    translation_checkpoint: dict[str, Any] | None = None

    def as_checkpoint_payload(self) -> dict[str, Any]:
        """把页面终态编码为可跨进程恢复的纯 JSON 内容。"""

        from transflow.domain.common import json_ready

        payload = json_ready(self)
        if not isinstance(payload, dict):
            raise TypeError("ProcessedPage 序列化结果必须为对象")
        payload.pop("resumed", None)
        return payload

    @classmethod
    def from_checkpoint_payload(cls, payload: dict[str, Any]) -> ProcessedPage:
        """从已校验 Checkpoint 恢复页面终态，并标记本次未重跑。"""

        application_payload = payload.get("application")
        preview_payload = payload.get("preview")
        patch_payload = payload.get("patch")
        return cls(
            page_no=int(payload["page_no"]),
            route=str(payload["route"]),
            outcome=PageOutcome.from_dict(payload["outcome"]),
            patch=PagePatch.from_dict(patch_payload) if patch_payload is not None else None,
            preview=(
                ArtifactReference.from_dict(preview_payload)
                if preview_payload is not None
                else None
            ),
            unit_ids=tuple(payload["unit_ids"]),
            translated_unit_ids=tuple(payload["translated_unit_ids"]),
            application=(
                PatchApplicationResult(
                    interpreter_id=application_payload["interpreter_id"],
                    patch_id=application_payload["patch_id"],
                    owner=application_payload["owner"],
                    operation_ids=tuple(application_payload["operation_ids"]),
                    applied_count=int(application_payload["applied_count"]),
                    layout_remainders=tuple(application_payload["layout_remainders"]),
                    target_object_ids=tuple(application_payload.get("target_object_ids", ())),
                    patch_manifest_hash=str(application_payload.get("patch_manifest_hash", "")),
                    render_config_hash=str(application_payload.get("render_config_hash", "")),
                )
                if application_payload is not None
                else None
            ),
            resumed=True,
            toolbox_id=payload.get("toolbox_id"),
            toolbox_version=payload.get("toolbox_version"),
            catalog_hash=payload.get("catalog_hash"),
            evidence_attestation_hash=payload.get("evidence_attestation_hash"),
            translation_checkpoint=payload.get("translation_checkpoint"),
        )

    def mark_resumed(self) -> ProcessedPage:
        """返回只改变恢复标记的副本，不改变权威业务内容。"""

        return replace(self, resumed=True)


@dataclass(frozen=True, slots=True)
class DocumentExecution:
    """聚合一轮完整 PDF 执行的文档结果、全部页面和 Preservation 证据。"""

    result: DocumentResult
    pages: tuple[ProcessedPage, ...]
    final_artifact: ArtifactReference
    preservation: PreservationResult
    preflight: PreservationPreflightResult | None = None

    @property
    def resumed_page_count(self) -> int:
        """统计本次运行由 Checkpoint 直接恢复且没有重跑的页面数。"""

        return sum(page.resumed for page in self.pages)


def main() -> int:
    """记录应用值对象不持有打开的 PDF 或网络连接。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P4 应用合同示例，意图=只跨边界传递可序列化页面结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
