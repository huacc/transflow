"""实现完整 PDF 的只读预检、稳定枚举和有界顺序编排。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from transflow.application.contracts import (
    DocumentExecution,
    EnumeratedPage,
    PageExecutionPipeline,
    ProcessedPage,
)
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuildInput,
    LayoutMemoryPolicyConfig,
)
from transflow.application.page_pipeline import ROUTE_PASSTHROUGH
from transflow.classification.engine import ClassificationEngine, ClassifiedPage
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.jobs import DocumentResult, DocumentRunRequest
from transflow.domain.layout_memory import DocumentLayoutMemoryIdentity, DocumentLayoutMemoryRef
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import DocumentOutcome, Fallback
from transflow.pdf_kernel.facts import PageFactsExtractor
from transflow.pdf_kernel.preservation import (
    PreflightDecision,
    PreservationPreflightResult,
)

LOGGER = logging.getLogger("transflow.application.document_coordinator")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
RouteResolver = Callable[[EnumeratedPage], str]


class DocumentLayoutMemoryRuntimePort(Protocol):
    """描述协调器冻结记忆所需的最小运行时能力，隔离文件 Adapter 实现。"""

    def prepare(
        self,
        request: DocumentLayoutMemoryBuildInput,
        *,
        crash_at: str | None = None,
    ) -> DocumentLayoutMemoryRef:
        """构建或恢复唯一权威文档记忆引用。"""

        ...

    def bind_page_contexts(
        self,
        contexts: tuple[PageExecutionContext, ...],
        memory_ref: DocumentLayoutMemoryRef,
    ) -> tuple[PageExecutionContext, ...]:
        """把同一不可变引用绑定到全部 PageContext。"""

        ...


def _passthrough_route(_page: EnumeratedPage) -> str:
    """不安全 PDF 跳过分类并强制选择页级透传路线。"""

    return ROUTE_PASSTHROUGH


def _sha256_file(path: Path) -> str:
    """流式计算完整 PDF 哈希，供枚举前后双重一致性检查。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DocumentCoordinator:
    """只负责编排完整 PDF，不实现分类语义或具体页面布局算法。"""

    def __init__(self, facts_extractor: PageFactsExtractor) -> None:
        """绑定最终 SharedPdfKernel 中的唯一事实提取器。"""

        self._facts_extractor = facts_extractor

    def enumerate_pages(
        self,
        request: DocumentRunRequest,
        *,
        include_classification: bool = False,
    ) -> tuple[EnumeratedPage, ...]:
        """核对源哈希，按原始页序创建 1-based 页面上下文并执行后置复核。"""

        source_path = Path(request.source_pdf_path)
        LOGGER.info("调用文档预检，意图=建立完整有序页面清单 run_id=%s", request.run_id)
        if _sha256_file(source_path) != request.source_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "枚举前源哈希变化")
        if include_classification:
            extracted = self._facts_extractor.extract_all(
                source_path,
                request.source_hash,
                include_classification=True,
            )
        else:
            # 普通 P4 链保持旧签名调用，兼容已有提取器子类和故障注入实现。
            extracted = self._facts_extractor.extract_all(source_path, request.source_hash)
        if _sha256_file(source_path) != request.source_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "枚举期间源哈希变化")
        pages = tuple(
            EnumeratedPage(
                context=PageExecutionContext(
                    job_id=request.job_id,
                    run_id=request.run_id,
                    source_hash=request.source_hash,
                    page_no=item.page.page_no,
                    geometry_hash=item.page.geometry_hash,
                    config_snapshot_hash=request.config_snapshot_hash,
                ),
                facts=item,
            )
            for item in extracted
        )
        expected = tuple(range(1, len(pages) + 1))
        if not pages or tuple(item.context.page_no for item in pages) != expected:
            raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "页枚举遗漏、重复或乱序")
        return pages

    def classify_pages(
        self,
        pages: tuple[EnumeratedPage, ...],
        classification_engine: ClassificationEngine,
        page_concurrency: int,
    ) -> tuple[ClassifiedPage, ...]:
        """按显式并发预算分类全部页面，并按 page_no 归并乱序完成结果。"""

        if page_concurrency < 1:
            raise ValueError("page_concurrency 必须为正整数")
        LOGGER.info(
            "调用文档分类，意图=受控并发生成全页 Route pages=%s concurrency=%s",
            len(pages),
            page_concurrency,
        )
        completed: list[ClassifiedPage] = []
        with ThreadPoolExecutor(max_workers=page_concurrency) as executor:
            futures = {
                executor.submit(classification_engine.classify_page, page.facts, len(pages)): page
                for page in pages
            }
            for future in as_completed(futures):
                classified = future.result()
                source_page = futures[future]
                if (
                    classified.page_no != source_page.context.page_no
                    or classified.page_identity != source_page.facts.page_identity
                ):
                    raise PortCallError(
                        ErrorCode.PORT_CONTRACT_VIOLATION,
                        False,
                        "分类结果页面身份串页",
                    )
                completed.append(classified)
        ordered = tuple(sorted(completed, key=lambda item: item.page_no))
        if tuple(item.page_no for item in ordered) != tuple(range(1, len(pages) + 1)):
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "分类结果遗漏、重复或乱序未收敛",
            )
        return ordered

    def freeze_document_layout_memory(
        self,
        pages: tuple[EnumeratedPage, ...],
        routes: tuple[tuple[int, str], ...],
        identity: DocumentLayoutMemoryIdentity,
        policy: LayoutMemoryPolicyConfig,
        runtime: DocumentLayoutMemoryRuntimePort,
        *,
        crash_at: str | None = None,
    ) -> tuple[tuple[EnumeratedPage, ...], DocumentLayoutMemoryRef]:
        """在页面 ready 前闭合全页事实/Route 屏障、原子冻结记忆并绑定同一引用。"""

        LOGGER.info(
            "调用文档记忆屏障，意图=冻结后再放行全部页面 pages=%s",
            len(pages),
        )
        request = DocumentLayoutMemoryBuildInput(
            expected_page_count=len(pages),
            page_facts=tuple(page.facts for page in pages),
            routes=routes,
            identity=identity,
            policy=policy,
        )
        memory_ref = runtime.prepare(request, crash_at=crash_at)
        contexts = runtime.bind_page_contexts(tuple(page.context for page in pages), memory_ref)
        bound = tuple(
            EnumeratedPage(context=context, facts=page.facts)
            for context, page in zip(contexts, pages, strict=True)
        )
        return bound, memory_ref

    def run_classified(
        self,
        request: DocumentRunRequest,
        classification_engine: ClassificationEngine,
        page_concurrency: int,
        pipeline: PageExecutionPipeline,
        finalizer: DocumentFinalizer,
        *,
        final_crash_at: str | None = None,
    ) -> DocumentExecution:
        """用真实分类引擎替代测试 Route fixture 后执行完整 PDF。"""

        LOGGER.info("调用分类文档编排，意图=分类后执行全部原始页面 run_id=%s", request.run_id)
        preflight = finalizer.preflight(request)
        pages = self.enumerate_pages(request, include_classification=True)
        selected_route_resolver: RouteResolver
        if preflight.decision is PreflightDecision.PASSTHROUGH:
            selected_route_resolver = _passthrough_route
        else:
            classified_pages = self.classify_pages(
                pages,
                classification_engine,
                page_concurrency,
            )
            routes = {item.page_identity: item.route.route for item in classified_pages}

            def classified_route(page: EnumeratedPage) -> str:
                """按分类结果的稳定页面身份返回已经校验的 Route。"""

                return routes[page.facts.page_identity]

            selected_route_resolver = classified_route
        return self._execute_pages(
            request,
            pages,
            selected_route_resolver,
            pipeline,
            finalizer,
            preflight,
            final_crash_at=final_crash_at,
        )

    def run(
        self,
        request: DocumentRunRequest,
        route_resolver: RouteResolver,
        pipeline: PageExecutionPipeline,
        finalizer: DocumentFinalizer,
        *,
        final_crash_at: str | None = None,
    ) -> DocumentExecution:
        """顺序执行每个页面并在全页终态屏障后最终化完整文档。"""

        LOGGER.info("调用完整 PDF 编排，意图=从枚举收敛到单一目标 PDF run_id=%s", request.run_id)
        preflight = finalizer.preflight(request)
        pages = self.enumerate_pages(request)
        effective_route_resolver = (
            (lambda _page: ROUTE_PASSTHROUGH)
            if preflight.decision is PreflightDecision.PASSTHROUGH
            else route_resolver
        )
        return self._execute_pages(
            request,
            pages,
            effective_route_resolver,
            pipeline,
            finalizer,
            preflight,
            final_crash_at=final_crash_at,
        )

    def _execute_pages(
        self,
        request: DocumentRunRequest,
        pages: tuple[EnumeratedPage, ...],
        route_resolver: RouteResolver,
        pipeline: PageExecutionPipeline,
        finalizer: DocumentFinalizer,
        preflight: PreservationPreflightResult,
        *,
        final_crash_at: str | None,
    ) -> DocumentExecution:
        """按已绑定 Route 顺序执行页面，并在终态屏障后最终化文档。"""

        source_path = Path(request.source_pdf_path)
        processed: list[ProcessedPage] = []
        for page in pages:
            route = route_resolver(page)
            processed.append(pipeline.execute(source_path, page, route))
        page_results = tuple(processed)
        finalization = finalizer.finalize(
            request,
            pages,
            page_results,
            crash_at=final_crash_at,
            preflight=preflight,
        )
        degraded = finalization.document_passthrough or any(
            item.outcome.fallback is not Fallback.NONE for item in page_results
        )
        outcome = (
            DocumentOutcome.COMPLETED_WITH_DEGRADATION if degraded else DocumentOutcome.COMPLETED
        )
        degradation_codes = (
            ("P6_DOCUMENT_PREFLIGHT_PASSTHROUGH", *preflight.reason_codes)
            if preflight.decision is PreflightDecision.PASSTHROUGH
            else (("P4_DOCUMENT_DEGRADED",) if degraded else ())
        )
        result = DocumentResult(
            run_id=request.run_id,
            outcome=outcome,
            final_artifact_id=finalization.artifact.artifact_id,
            degradation_codes=degradation_codes,
        )
        return DocumentExecution(
            result=result,
            pages=page_results,
            final_artifact=finalization.artifact,
            preservation=finalization.preservation,
            preflight=preflight,
        )


def main() -> int:
    """记录 DocumentCoordinator 仅接受完整 PDF 请求和显式 Route resolver。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("DocumentCoordinator 示例，意图=有界顺序执行全部原始页面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
