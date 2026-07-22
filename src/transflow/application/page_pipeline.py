"""实现 single、visual_only 与透传的最小页面终态流水线。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from transflow.application.contracts import (
    AtomicArtifactStore,
    EnumeratedPage,
    PageCheckpointStore,
    ProcessedPage,
)
from transflow.domain.artifacts import ArtifactPayload, ArtifactReference, CheckpointRecord
from transflow.domain.classification import ClassificationRoute
from transflow.domain.common import canonical_json_bytes
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
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
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.domain.translation import TranslationBatch, TranslationUnit
from transflow.pdf_kernel.patch import PagePatchInterpreter, patch_operation_hash
from transflow.pdf_kernel.renderer import PyMuPdfPageRenderer
from transflow.ports.translation import TranslationPort

LOGGER = logging.getLogger("transflow.application.page_pipeline")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
ROUTE_SINGLE = "body.flow_text.single"
ROUTE_VISUAL_ONLY = "visual_only"
ROUTE_PASSTHROUGH = "passthrough"


def build_unit_id(page: EnumeratedPage, object_id: str) -> str:
    """由稳定页面身份和文本对象身份派生可恢复的翻译单元 ID。"""

    return hashlib.sha256(f"{page.facts.page_identity}\0{object_id}".encode("ascii")).hexdigest()


class PreviewPublisher:
    """验证并原子发布 PNG；任何失败都不返回或提交预览指针。"""

    def __init__(self, artifact_store: AtomicArtifactStore) -> None:
        """绑定当前 Run 的不可变 Artifact 存储。"""

        self._artifact_store = artifact_store

    def publish(
        self,
        page_no: int,
        png_bytes: bytes,
        *,
        crash_at: str | None = None,
    ) -> ArtifactReference | None:
        """真实解码 PNG 后执行原子写入，异常时清理 partial 并返回空指针。"""

        LOGGER.info("调用预览发布，意图=只提交可解码 PNG page_no=%s", page_no)
        try:
            PyMuPdfPageRenderer.validate_png(png_bytes)
            content_hash = hashlib.sha256(png_bytes).hexdigest()
            artifact_id = f"preview-p{page_no:04d}-{content_hash[:16]}"
            payload = ArtifactPayload(artifact_id, "image/png", png_bytes, content_hash)
            return self._artifact_store.put_atomic(
                payload,
                f"previews/page-{page_no:04d}-{content_hash}.png",
                "preview",
                crash_at=crash_at,
            )
        except DomainContractError, PortCallError, ValueError, RuntimeError:
            self._artifact_store.recover()
            LOGGER.exception("预览发布失败，意图=保持 preview 指针为空 page_no=%s", page_no)
            return None


class MinimalPagePipeline:
    """把每个已枚举页面收敛为批准 Patch 或原页透传终态。"""

    def __init__(
        self,
        translation: TranslationPort,
        renderer: PyMuPdfPageRenderer,
        interpreter: PagePatchInterpreter,
        previews: PreviewPublisher,
        checkpoints: PageCheckpointStore,
        compatibility: CheckpointCompatibility,
        font_id: str,
    ) -> None:
        """绑定 P3 Ports、唯一 Kernel、恢复指纹和受控字体 ID。"""

        self._translation = translation
        self._renderer = renderer
        self._interpreter = interpreter
        self._previews = previews
        self._checkpoints = checkpoints
        self._compatibility = compatibility
        self._font_id = font_id

    def execute(
        self,
        source_path: Path,
        page: EnumeratedPage,
        route: str | ClassificationRoute,
    ) -> ProcessedPage:
        """优先恢复已提交页，否则执行最小页面链并原子提交 Checkpoint。"""

        if isinstance(route, ClassificationRoute):
            classification_route: ClassificationRoute | None = route
            route_name = route.route
        else:
            classification_route = None
            route_name = route
        LOGGER.info(
            "调用页面流水线，意图=让页面进入唯一终态 page_no=%s route=%s",
            page.context.page_no,
            route_name,
        )
        stored = self._checkpoints.load_page(page.context.page_no)
        if stored is not None:
            payload = json.loads(stored.payload.decode("utf-8"))
            resumed = ProcessedPage.from_checkpoint_payload(payload)
            if resumed.route != route_name or resumed.classification_route != classification_route:
                raise PortCallError(ErrorCode.CHECKPOINT_INCOMPATIBLE, False, "恢复 Route 不一致")
            return resumed
        if route_name == ROUTE_SINGLE:
            processed = self._execute_single(source_path, page, classification_route)
        elif route_name in {ROUTE_VISUAL_ONLY, ROUTE_PASSTHROUGH}:
            processed = self._fallback_page(
                source_path,
                page,
                route_name,
                "P4_SOURCE_PASSTHROUGH",
                classification_route=classification_route,
            )
        else:
            processed = self._fallback_page(
                source_path,
                page,
                route_name,
                "P4_ROUTE_UNSUPPORTED",
                classification_route=classification_route,
            )
        self._commit(page.context.run_id, processed)
        return processed

    def _execute_single(
        self,
        source_path: Path,
        page: EnumeratedPage,
        classification_route: ClassificationRoute | None,
    ) -> ProcessedPage:
        """执行文本单元、固定翻译、Patch、candidate、Judge 与预览发布。"""

        text_object = next(
            (item for item in page.facts.objects if not item.protected and item.text),
            None,
        )
        if text_object is None:
            return self._fallback_page(
                source_path,
                page,
                ROUTE_SINGLE,
                "P4_TEXT_UNIT_MISSING",
                classification_route=classification_route,
            )
        unit_id = build_unit_id(page, text_object.object_id)
        unit = TranslationUnit(
            unit_id=unit_id,
            page_no=page.context.page_no,
            ordinal=0,
            source_text=text_object.text,
            region_id=f"region-{page.context.page_no:04d}-single",
        )
        batch = TranslationBatch(
            batch_id=f"batch-{page.context.run_id}-p{page.context.page_no:04d}",
            source_language="en",
            target_language="zh-CN",
            units=(unit,),
        )
        try:
            bundle = self._translation.translate(batch)
            translated = bundle.units[0].translated_text
            operation_hash = patch_operation_hash(
                owner=ROUTE_SINGLE,
                target_object_ids=(text_object.object_id,),
                rect=text_object.bbox,
                replacement_text=translated,
                font_id=self._font_id,
                font_size=max(6.0, min(12.0, (text_object.bbox[3] - text_object.bbox[1]) * 0.55)),
            )
            operation = PatchOperation(
                operation_id=f"op-{unit_id[:16]}",
                region_id=unit.region_id,
                kind="replace_text",
                payload_hash=operation_hash,
                owner=ROUTE_SINGLE,
                target_object_ids=(text_object.object_id,),
                rect=text_object.bbox,
                replacement_text=translated,
                font_id=self._font_id,
                font_size=max(6.0, min(12.0, (text_object.bbox[3] - text_object.bbox[1]) * 0.55)),
            )
            patch = PagePatch(
                patch_id=f"patch-{page.facts.page_identity[:24]}",
                source_hash=page.context.source_hash,
                page_no=page.context.page_no,
                geometry_hash=page.context.geometry_hash,
                owner=ROUTE_SINGLE,
                operations=(operation,),
            )
            candidate = self._renderer.render_candidate(
                source_path,
                page.context,
                page.facts,
                patch,
                ROUTE_SINGLE,
            )
            if candidate.application is None or not candidate.application.fits:
                raise PortCallError(ErrorCode.PRESERVATION_FAILED, False, "Judge 判定文本框溢出")
            preview = self._previews.publish(page.context.page_no, candidate.png_bytes)
            if preview is None:
                raise PortCallError(ErrorCode.PREVIEW_INVALID, True, "候选预览未发布")
            return ProcessedPage(
                page_no=page.context.page_no,
                route=ROUTE_SINGLE,
                outcome=PageOutcome(
                    page.context.page_no,
                    PagePipelineState.FINALIZED,
                    ArtifactProduced.YES,
                    ArtifactIntegrity.PASS,
                    TranslationCoverage.FULL,
                    Capability.SUPPORTED,
                    Quality.PASS,
                    Fallback.NONE,
                ),
                patch=patch,
                preview=preview,
                unit_ids=(unit_id,),
                translated_unit_ids=bundle.requested_unit_ids,
                application=candidate.application,
                classification_route=classification_route,
            )
        except (DomainContractError, PortCallError, ValueError, RuntimeError) as error:
            LOGGER.warning(
                "页面 single 链路降级，意图=保证页面最终化 page_no=%s error=%s",
                page.context.page_no,
                type(error).__name__,
            )
            return self._fallback_page(
                source_path,
                page,
                ROUTE_SINGLE,
                f"P4_SINGLE_FALLBACK_{type(error).__name__.upper()}",
                unit_ids=(unit_id,),
                classification_route=classification_route,
            )

    def _fallback_page(
        self,
        source_path: Path,
        page: EnumeratedPage,
        route: str,
        finding_code: str,
        *,
        unit_ids: tuple[str, ...] = (),
        classification_route: ClassificationRoute | None = None,
    ) -> ProcessedPage:
        """渲染原页预览并形成不会进入 Patch 回放的降级终态。"""

        preview: ArtifactReference | None = None
        try:
            source_render = self._renderer.render_passthrough(source_path, page.context.page_no)
            preview = self._previews.publish(page.context.page_no, source_render.png_bytes)
        except PortCallError, ValueError, RuntimeError:
            LOGGER.exception(
                "原页预览失败，意图=仍保留无预览页面终态 page_no=%s", page.context.page_no
            )
        produced = ArtifactProduced.YES if preview is not None else ArtifactProduced.NO
        integrity = ArtifactIntegrity.PASS if preview is not None else ArtifactIntegrity.FAIL
        return ProcessedPage(
            page_no=page.context.page_no,
            route=route,
            outcome=PageOutcome(
                page.context.page_no,
                PagePipelineState.FINALIZED,
                produced,
                integrity,
                TranslationCoverage.NONE,
                Capability.SUPPORTED if route == ROUTE_VISUAL_ONLY else Capability.PARTIAL,
                Quality.PASS if route == ROUTE_VISUAL_ONLY else Quality.FAIL,
                Fallback.PAGE_PASSTHROUGH,
                (finding_code,),
            ),
            patch=None,
            preview=preview,
            unit_ids=unit_ids,
            translated_unit_ids=(),
            application=None,
            classification_route=classification_route,
        )

    def _commit(self, run_id: str, processed: ProcessedPage) -> None:
        """在预览 Artifact 已确认后原子提交页级 FINALIZED Checkpoint。"""

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
    """记录页面流水线仅处理已枚举页面且所有出口均为 FINALIZED。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("MinimalPagePipeline 示例，意图=执行 single/visual/passthrough 最小闭环")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
