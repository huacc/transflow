"""把 P8 Catalog、PageToolbox、SharedPdfKernel 与页级 Checkpoint 接成生产链。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from transflow.application.contracts import (
    EnumeratedPage,
    PageCheckpointStore,
    ProcessedPage,
)
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.artifacts import CheckpointRecord
from transflow.domain.common import canonical_json_bytes
from transflow.domain.pages import PageOutcome
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    CheckpointCompatibility,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.pdf_kernel.renderer import PyMuPdfPageRenderer
from transflow.toolboxes.catalog import CatalogResolution, ToolboxCatalog

LOGGER = logging.getLogger("transflow.application.toolbox_page_pipeline")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


class ToolboxPagePipeline:
    """让每页按 Catalog 唯一解析，并保证失败只降级当前页面。"""

    def __init__(
        self,
        catalog: ToolboxCatalog,
        coordinator: ToolboxPageCoordinator,
        renderer: PyMuPdfPageRenderer,
        previews: PreviewPublisher,
        checkpoints: PageCheckpointStore,
        compatibility: CheckpointCompatibility,
    ) -> None:
        """绑定不可变 Catalog、六阶段协调器、唯一 Kernel 和恢复合同。"""

        self._catalog = catalog
        self._coordinator = coordinator
        self._renderer = renderer
        self._previews = previews
        self._checkpoints = checkpoints
        self._compatibility = compatibility

    def execute(self, source_path: Path, page: EnumeratedPage, route: str) -> ProcessedPage:
        """恢复或执行单页；禁用、初始化和运行异常均形成显式透传终态。"""

        LOGGER.info(
            "调用 P8 页面流水线，意图=按 Catalog 执行或降级 page_no=%s route=%s",
            page.context.page_no,
            route,
        )
        stored = self._checkpoints.load_page(page.context.page_no)
        if stored is not None:
            restored = ProcessedPage.from_checkpoint_payload(
                json.loads(stored.payload.decode("utf-8"))
            )
            if restored.route != route or restored.catalog_hash != self._catalog.catalog_hash:
                raise ValueError("P8 恢复 Route 或 Catalog hash 不一致")
            return restored
        self._catalog.assert_source_unchanged()
        resolution = self._catalog.resolve_enabled(route, page.context.page_no)
        if resolution.toolbox is None:
            code = resolution.finding.code if resolution.finding is not None else "TOOLBOX_DISABLED"
            processed = self._fallback_page(source_path, page, route, code, resolution)
        else:
            processed = self._execute_enabled(source_path, page, resolution)
        self._commit(page.context.run_id, processed)
        return processed

    def _execute_enabled(
        self,
        source_path: Path,
        page: EnumeratedPage,
        resolution: CatalogResolution,
    ) -> ProcessedPage:
        """执行已启用 Toolbox，再用唯一解释器生成真实候选预览。"""

        if resolution.toolbox is None:
            raise AssertionError("enabled 分支必须包含 Toolbox")
        route = resolution.route
        try:
            execution = self._coordinator.execute(
                ToolboxPageWork(page.context, page.facts, resolution.toolbox)
            )
            if execution.patch is not None:
                candidate = self._renderer.render_candidate(
                    source_path,
                    page.context,
                    page.facts,
                    execution.patch,
                    route,
                )
                if candidate.application is None or not candidate.application.fits:
                    raise RuntimeError("真实候选硬约束失败")
            else:
                candidate = self._renderer.render_passthrough(
                    source_path,
                    page.context.page_no,
                )
            preview = self._previews.publish(page.context.page_no, candidate.png_bytes)
            if preview is None:
                raise RuntimeError("候选预览未能原子发布")
            entry = resolution.entry
            return ProcessedPage(
                page_no=page.context.page_no,
                route=route,
                outcome=execution.outcome,
                patch=execution.patch,
                preview=preview,
                unit_ids=execution.ordered_unit_ids,
                translated_unit_ids=(
                    execution.ordered_unit_ids
                    if execution.outcome.translation_coverage is TranslationCoverage.FULL
                    else ()
                ),
                application=candidate.application,
                toolbox_id=resolution.toolbox.descriptor.toolbox_id,
                toolbox_version=entry.toolbox_version if entry is not None else None,
                catalog_hash=self._catalog.catalog_hash,
                evidence_attestation_hash=(
                    entry.evidence_attestation_hash if entry is not None else None
                ),
            )
        except Exception as error:
            LOGGER.exception(
                "已启用 Toolbox 执行失败，意图=仅当前页透传 route=%s page_no=%s",
                route,
                page.context.page_no,
            )
            return self._fallback_page(
                source_path,
                page,
                route,
                f"TOOLBOX_EXECUTION_FAILED_{type(error).__name__.upper()}",
                resolution,
            )

    def _fallback_page(
        self,
        source_path: Path,
        page: EnumeratedPage,
        route: str,
        finding_code: str,
        resolution: CatalogResolution,
    ) -> ProcessedPage:
        """真实渲染原页预览并保留 Catalog 版本和证据身份。"""

        preview = None
        try:
            candidate = self._renderer.render_passthrough(source_path, page.context.page_no)
            preview = self._previews.publish(page.context.page_no, candidate.png_bytes)
        except Exception:
            LOGGER.exception(
                "P8 原页预览失败，意图=保留无预览终态 page_no=%s",
                page.context.page_no,
            )
        produced = ArtifactProduced.YES if preview is not None else ArtifactProduced.NO
        integrity = ArtifactIntegrity.PASS if preview is not None else ArtifactIntegrity.FAIL
        entry = resolution.entry
        return ProcessedPage(
            page_no=page.context.page_no,
            route=route,
            outcome=PageOutcome(
                page_no=page.context.page_no,
                state=PagePipelineState.FINALIZED,
                artifact_produced=produced,
                integrity=integrity,
                translation_coverage=TranslationCoverage.NONE,
                capability=Capability.PARTIAL,
                quality=Quality.FAIL,
                fallback=Fallback.PAGE_PASSTHROUGH,
                finding_codes=(finding_code,),
            ),
            patch=None,
            preview=preview,
            unit_ids=(),
            translated_unit_ids=(),
            application=None,
            toolbox_id=entry.toolbox_key if entry is not None else None,
            toolbox_version=entry.toolbox_version if entry is not None else None,
            catalog_hash=self._catalog.catalog_hash,
            evidence_attestation_hash=(
                entry.evidence_attestation_hash if entry is not None else None
            ),
        )

    def _commit(self, run_id: str, processed: ProcessedPage) -> None:
        """在预览引用确定后原子提交含 Catalog 身份的页级 Checkpoint。"""

        payload = canonical_json_bytes(processed.as_checkpoint_payload())
        references = (processed.preview,) if processed.preview is not None else ()
        record = CheckpointRecord(
            run_id=run_id,
            version=1,
            state_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            compatibility=self._compatibility,
            artifact_refs=references,
        )
        self._checkpoints.commit_page(processed.page_no, record, 0)


def main() -> int:
    """记录 P8 页面流水线只允许唯一 Toolbox 或确定透传出口。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ToolboxPagePipeline 示例，意图=接通 Catalog 与完整 PDF 最终化")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
