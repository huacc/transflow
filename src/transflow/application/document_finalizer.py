"""从源 PDF 副本串行回放批准 Patch 并原子发布完整目标 PDF。"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from transflow.application.contracts import AtomicArtifactStore, EnumeratedPage, ProcessedPage
from transflow.domain.artifacts import ArtifactPayload, ArtifactReference
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import ensure_document_finalizable
from transflow.pdf_kernel.patch import PagePatchInterpreter, ReplayPage
from transflow.pdf_kernel.preservation import (
    DEFAULT_SUPPORT_MATRIX,
    PreflightDecision,
    PreservationPreflightResult,
    PreservationResult,
    capture_document_structure,
    load_support_matrix,
    preflight_document,
    validate_minimal_preservation,
    validate_preservation,
)

LOGGER = logging.getLogger("transflow.application.document_finalizer")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
LEGACY_PAGE_SUFFIX = ".legacy-page.pdf"


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """记录最终 Artifact、Preservation 结果、预检和是否整本降级。"""

    artifact: ArtifactReference
    preservation: PreservationResult
    document_passthrough: bool
    preflight: PreservationPreflightResult


class DocumentFinalizer:
    """只在一份源副本上串行回放，不使用页级 PDF 合并或页面重建。"""

    def __init__(
        self,
        interpreter: PagePatchInterpreter,
        artifacts: AtomicArtifactStore,
        run_root: Path,
        support_matrix_path: Path = DEFAULT_SUPPORT_MATRIX,
    ) -> None:
        """绑定唯一解释器、当前 Run Artifact 存储和私有工作根。"""

        self._interpreter = interpreter
        self._artifacts = artifacts
        self._run_root = run_root.resolve()
        self._support_matrix_path = support_matrix_path.resolve()

    def preflight(self, request: DocumentRunRequest) -> PreservationPreflightResult:
        """在任何页面写入或 AI 调用前执行整文 Preservation 预检。"""

        LOGGER.info("调用最终化预检，意图=先决定写路径或整文透传 run_id=%s", request.run_id)
        source_path = Path(request.source_pdf_path)
        if source_path.name.endswith(LEGACY_PAGE_SUFFIX):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "legacy 单页兼容物不得作为 DocumentFinalizer 输入",
            )
        result = preflight_document(
            source_path,
            support_matrix_path=self._support_matrix_path,
        )
        if result.decision is PreflightDecision.PROCESS_FAILED:
            raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "源 PDF 预检无法完成")
        return result

    def finalize(
        self,
        request: DocumentRunRequest,
        pages: tuple[EnumeratedPage, ...],
        processed_pages: tuple[ProcessedPage, ...],
        *,
        crash_at: str | None = None,
        preflight: PreservationPreflightResult | None = None,
    ) -> FinalizationResult:
        """验证全页屏障、顺序回放、应急透传并发布唯一不可变 PDF。"""

        LOGGER.info("调用文档最终化，意图=发布单一完整 PDF run_id=%s", request.run_id)
        ensure_document_finalizable(tuple(item.outcome.state for item in processed_pages))
        expected_numbers = tuple(range(1, len(pages) + 1))
        ordered = tuple(sorted(processed_pages, key=lambda item: item.page_no))
        if tuple(item.page_no for item in ordered) != expected_numbers:
            raise DomainContractError(
                ErrorCode.DOCUMENT_NOT_FINALIZABLE, "页面结果遗漏、重复或越界"
            )
        page_by_number = {item.context.page_no: item for item in pages}
        source_path = Path(request.source_pdf_path)
        preflight_result = preflight or self.preflight(request)
        if preflight_result.decision is PreflightDecision.PROCESS_FAILED:
            raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "源 PDF 预检失败")
        source_structure = preflight_result.structure or capture_document_structure(source_path)
        support_matrix = load_support_matrix(self._support_matrix_path)
        work_directory = self._run_root / "final"
        work_directory.mkdir(parents=True, exist_ok=True)
        work_path = work_directory / f"{request.run_id}.pdf.partial"
        existing = self._artifacts.published_final()
        if existing is not None:
            LOGGER.info("复用已发布最终文件，意图=恢复时不重复回放 run_id=%s", request.run_id)
            work_path.write_bytes(self._artifacts.get(existing.artifact_id))
            published_content = self._artifacts.get(existing.artifact_id)
            if preflight_result.decision is PreflightDecision.PASSTHROUGH:
                if hashlib.sha256(published_content).hexdigest() != request.source_hash:
                    raise PortCallError(
                        ErrorCode.PRESERVATION_FAILED,
                        False,
                        "已发布整文透传文件与源字节不一致",
                    )
                preservation = validate_minimal_preservation(
                    source_structure,
                    source_structure,
                    frozenset(),
                )
            else:
                preservation = validate_preservation(
                    source_structure,
                    capture_document_structure(work_path, support_matrix=support_matrix),
                    frozenset(item.page_no for item in ordered if item.patch is not None),
                    support_matrix,
                )
            work_path.unlink()
            if not preservation.passed:
                raise PortCallError(ErrorCode.PRESERVATION_FAILED, False, "已发布最终文件失效")
            return FinalizationResult(
                existing,
                preservation,
                preflight_result.decision is PreflightDecision.PASSTHROUGH,
                preflight_result,
            )
        modified: frozenset[int] = frozenset()
        document_passthrough = False
        try:
            shutil.copyfile(source_path, work_path)
            if preflight_result.decision is PreflightDecision.PASSTHROUGH:
                document_passthrough = True
                preservation = validate_minimal_preservation(
                    source_structure,
                    source_structure,
                    frozenset(),
                )
            else:
                replay_pages = tuple(
                    ReplayPage(
                        page_by_number[item.page_no].context,
                        page_by_number[item.page_no].facts,
                        item.patch,
                        item.route,
                    )
                    for item in ordered
                    if item.patch is not None
                )
                modified = self._interpreter.replay_document(work_path, replay_pages)
                preservation = validate_preservation(
                    source_structure,
                    capture_document_structure(work_path, support_matrix=support_matrix),
                    modified,
                    support_matrix,
                )
            if not preservation.passed:
                raise PortCallError(ErrorCode.PRESERVATION_FAILED, False, "Preservation 合同失败")
        except Exception as error:
            LOGGER.warning(
                "Patch 回放或校验失败，意图=重新复制源 PDF 形成整本透传 error=%s",
                type(error).__name__,
            )
            document_passthrough = True
            shutil.copyfile(source_path, work_path)
            modified = frozenset()
            preservation = validate_preservation(
                source_structure,
                capture_document_structure(work_path, support_matrix=support_matrix),
                modified,
                support_matrix,
            )
            if not preservation.passed:
                raise PortCallError(
                    ErrorCode.PRESERVATION_FAILED,
                    False,
                    "源 PDF 应急副本也未通过结构校验",
                ) from error
        content = work_path.read_bytes()
        work_path.unlink()
        content_hash = hashlib.sha256(content).hexdigest()
        artifact_id = f"final-{request.run_id}"
        reference = self._artifacts.put_atomic(
            ArtifactPayload(artifact_id, "application/pdf", content, content_hash),
            f"final/{artifact_id}-{content_hash}.pdf",
            "final",
            crash_at=crash_at,
        )
        self._artifacts.publish_final(reference)
        return FinalizationResult(
            reference,
            preservation,
            document_passthrough,
            preflight_result,
        )


def main() -> int:
    """记录最终化只能在全部页面终态后从源副本执行。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("DocumentFinalizer 示例，意图=串行回放并原子发布完整 PDF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
