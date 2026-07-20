"""生成可长期查看的 P5 分类接线 PDF 与真实 Gate 结果快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pymupdf

from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.page_pipeline import MinimalPagePipeline, PreviewPublisher, build_unit_id
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import CheckpointCompatibility
from transflow.pdf_kernel.facts import PageFactsExtractor
from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.pdf_kernel.patch import PagePatchInterpreter
from transflow.pdf_kernel.renderer import PyMuPdfPageRenderer

LOGGER = logging.getLogger("transflow.p5.showcase")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "p5"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
RUN_ID = "run-p5-showcase"
JOB_ID = "job-p5-showcase"


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，供请求绑定和展示 manifest 使用。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(label: str) -> str:
    """由版本标签生成稳定兼容性指纹，避免展示运行依赖随机值。"""

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ShowcaseModelDecisionAdapter:
    """为展示运行提供可复现的本地 ModelDecisionPort，不伪装真实质量结果。"""

    _SELECTIONS: ClassVar[dict[str, str]] = {
        "page.role": "body",
        "body.layout_owner": "flow_text",
        "body.flow.topology": "single",
    }

    def __init__(self) -> None:
        """初始化只用于展示 manifest 的真实调用计数。"""

        self.call_count = 0

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """按当前节点返回固定且位于 allow-list 内的确定性判定。"""

        node_key = str(request.node_spec["node_key"])
        selected = self._SELECTIONS.get(node_key)
        self.call_count += 1
        LOGGER.info(
            "调用展示模型判定，意图=验证 P5 Port 接线 node=%s stage=%s",
            node_key,
            request.node_spec["stage"],
        )
        if selected not in request.allowed_actions:
            return ModelDecision(
                decision_id=request.decision_id,
                decision_kind=request.decision_kind,
                result_code="INCONCLUSIVE",
                evidence_ids=(),
                confidence=0.0,
                reason_summary="展示节点没有登记确定性动作",
            )
        evidence_ids = tuple(request.evidence_ids[:1])
        return ModelDecision(
            decision_id=request.decision_id,
            decision_kind=request.decision_kind,
            result_code=selected,
            evidence_ids=evidence_ids,
            confidence=0.95,
            reason_summary="展示输入按冻结的单栏正文路径执行",
        )


def create_showcase_source(path: Path) -> Path:
    """生成只含可验证英文源文字的单页 PDF，避免默认字体写入中文问号。"""

    LOGGER.info("调用展示源 PDF 生成，意图=建立无缺字的 P5 输入 path=%s", path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    source_text = (
        "P5 CLASSIFICATION WIRING DEMONSTRATION. "
        "This document is a durable Transflow P5 showcase. It verifies that a complete PDF "
        "is enumerated, classified through ModelDecisionPort, routed to the single-column "
        "page pipeline, translated by a deterministic fixed test adapter, rendered with the "
        "controlled font registry, and finalized as one immutable PDF artifact. "
        "The real Qwen anonymous quality result is stored separately under test-results. "
        "The paragraph is intentionally long enough to exercise the body and flow-text rules. "
        "No file name, company name, sample identifier, page label, or expected route is exposed "
        "to the classification evidence. This source page contains native PDF text and does not "
        "depend on OCR, HTML, Chrome, system-font discovery, or page-level PDF concatenation. "
        "The final Chinese text is fixed test data for wiring verification only."
    )
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        remainder = page.insert_textbox(
            pymupdf.Rect(72, 90, 523, 720),
            source_text,
            fontname="helv",
            fontsize=12,
            lineheight=1.35,
            color=(0.08, 0.08, 0.08),
        )
        if remainder < 0:
            raise ValueError("展示源文字未完整写入页面")
        document.save(path, garbage=4, deflate=True)
    return path


def _build_request(source_path: Path) -> DocumentRunRequest:
    """为展示源 PDF 构造稳定且绑定真实内容哈希的完整文档请求。"""

    return DocumentRunRequest(
        source_pdf_path=str(source_path.resolve()),
        source_hash=_sha256_file(source_path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=_stable_hash("transflow.p5-showcase-config/v1"),
        job_id=JOB_ID,
        run_id=RUN_ID,
    )


def _fixed_translation(unit_id: str) -> FixedTranslationAdapter:
    """构造可读中文固定译文，明确其用途仅为接线而非真实翻译质量。"""

    translated_text = (
        "P5 页面分类接线演示\n\n"
        "分类路径：body.flow_text.single。\n\n"
        "本页使用固定测试译文和受控中文字体，验证分类、Patch、预览及完整 PDF 最终化。\n\n"
        "真实千问分类指标见 test-results。"
    )
    return FixedTranslationAdapter({unit_id: translated_text})


def _copy_test_results(output_root: Path) -> tuple[str, ...]:
    """复制本次 P5 的真实 Gate、分类指标和唯一阶段报告到展示目录。"""

    result_root = output_root / "test-results"
    result_root.mkdir(parents=True, exist_ok=True)
    sources = (
        REPO_ROOT / "docs" / "reports" / "gates" / "G5_evidence.json",
        REPO_ROOT / "docs" / "reports" / "gates" / "P5_classification_metrics.json",
    )
    copied: list[str] = []
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"缺少 P5 展示证据:{source.name}")
        target = result_root / source.name
        shutil.copyfile(source, target)
        copied.append(target.relative_to(output_root).as_posix())
    reports = sorted((REPO_ROOT / "docs" / "reports").glob("P5阶段_页面分类引擎迁移_*.md"))
    if len(reports) != 1:
        raise ValueError("P5 展示要求恰好存在一份阶段报告")
    report_target = result_root / reports[0].name
    shutil.copyfile(reports[0], report_target)
    copied.append(report_target.relative_to(output_root).as_posix())
    return tuple(copied)


def export_showcase(output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    """执行真实文件 Adapter 闭环并发布稳定 PDF、PNG、manifest 与测试结果。"""

    output_root = output_root.resolve()
    source_path = output_root / "source" / "p5_classification_wiring_source.pdf"
    final_path = output_root / "final" / "p5_classification_wiring_demo.pdf"
    preview_path = output_root / "final" / "p5_classification_wiring_demo.png"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    create_showcase_source(source_path)
    request = _build_request(source_path)
    coordinator = DocumentCoordinator(PageFactsExtractor())
    initial_pages = coordinator.enumerate_pages(request, include_classification=True)
    text_object = next(
        item for item in initial_pages[0].facts.objects if not item.protected and item.text
    )
    unit_id = build_unit_id(initial_pages[0], text_object.object_id)
    temp_root = REPO_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p5-showcase-", dir=temp_root) as temporary:
        run_root = Path(temporary) / "runs" / RUN_ID
        artifacts = SharedFilesystemArtifactAdapter(run_root, RUN_ID)
        checkpoints = FilesystemCheckpointAdapter(run_root, RUN_ID, artifacts)
        fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
        interpreter = PagePatchInterpreter(fonts)
        renderer = PyMuPdfPageRenderer(interpreter)
        compatibility = CheckpointCompatibility(
            source_hash=request.source_hash,
            config_hash=request.config_snapshot_hash,
            font_hash=_sha256_file(FONT_MANIFEST),
            toolbox_catalog_hash=_stable_hash("transflow.p5-showcase-toolbox/v1"),
            schema_hash=_stable_hash("transflow.p5-showcase-schema/v1"),
        )
        pipeline = MinimalPagePipeline(
            _fixed_translation(unit_id),
            renderer,
            interpreter,
            PreviewPublisher(artifacts),
            checkpoints,
            compatibility,
            FONT_ID,
        )
        model_adapter = ShowcaseModelDecisionAdapter()
        execution = coordinator.run_classified(
            request,
            ClassificationEngine(BoundedDecisionRunner(model_adapter)),
            1,
            pipeline,
            DocumentFinalizer(interpreter, artifacts, run_root),
        )
        LOGGER.info(
            "展示页面完成，意图=核对 Route、降级和 Finding route=%s fallback=%s findings=%s",
            execution.pages[0].route,
            execution.pages[0].outcome.fallback.value,
            execution.pages[0].outcome.finding_codes,
        )
        if execution.final_artifact is None or execution.pages[0].preview is None:
            raise RuntimeError("P5 展示运行没有发布最终 PDF 或页面 PNG")
        final_path.write_bytes(artifacts.get(execution.final_artifact.artifact_id))
        preview_path.write_bytes(artifacts.get(execution.pages[0].preview.artifact_id))

    with pymupdf.open(final_path) as document:
        extracted_text = "\n".join(page.get_text() for page in document)
        page_count = document.page_count
    normalized_text = unicodedata.normalize("NFKC", extracted_text).replace("\u00a0", " ")
    if "P5 页面分类接线演示" not in normalized_text or "?" in normalized_text:
        raise ValueError("最终展示 PDF 的中文提取结果异常")
    copied_results = _copy_test_results(output_root)
    manifest: dict[str, object] = {
        "schema_version": "transflow.p5-showcase/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stage": "P5",
        "gate": "G5",
        "gate_conclusion": "PASS",
        "classification_route": execution.pages[0].route,
        "document_outcome": execution.result.outcome.value,
        "model_decision_mode": "deterministic_local_test_port",
        "translation_mode": "fixed_test_translation",
        "real_quality_source": "test-results/P5_classification_metrics.json",
        "source_pdf": source_path.relative_to(output_root).as_posix(),
        "final_pdf": final_path.relative_to(output_root).as_posix(),
        "final_preview": preview_path.relative_to(output_root).as_posix(),
        "final_pdf_sha256": _sha256_file(final_path),
        "final_pdf_bytes": final_path.stat().st_size,
        "page_count": page_count,
        "question_mark_replacement_count": normalized_text.count("?"),
        "test_results": list(copied_results),
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    readme = output_root / "README.md"
    readme.write_text(
        """# Transflow P5 展示输出

本目录用于长期查看 P5 页面分类接线产物和真实 G5 测试结果。

- `source/`：无中文缺字问题的英文源 PDF。
- `final/`：使用固定测试译文、P1 受控字体和正式 Patch/Finalizer 生成的 PDF 与 PNG。
- `test-results/`：真实千问匿名分类指标、G5 原始命令证据和唯一 P5 阶段报告。
- `manifest.json`：文件哈希、Route、文档终态和展示能力边界。

注意：`final/` 证明 P5 分类与 P4 PDF 闭环可以生成可读文件，
但固定测试译文不代表真实翻译质量。真实千问在 P5 只用于页面分类质量验收。
""",
        encoding="utf-8",
    )
    LOGGER.info(
        "P5 展示导出完成，意图=提供稳定可读产物 route=%s pdf=%s",
        execution.pages[0].route,
        final_path.relative_to(output_root),
    )
    return manifest


def parse_args() -> argparse.Namespace:
    """解析仓库相对输出目录，默认遵循 PDF 产物目录约定。"""

    parser = argparse.ArgumentParser(description="导出 Transflow P5 展示产物")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT.relative_to(REPO_ROOT),
        help="相对仓库根的展示目录",
    )
    return parser.parse_args()


def main() -> int:
    """校验输出目录不越出仓库后执行展示导出并打印可审计摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    requested = args.output
    if requested.is_absolute():
        raise ValueError("展示输出目录必须是仓库相对路径")
    output_root = (REPO_ROOT / requested).resolve()
    try:
        output_root.relative_to(REPO_ROOT)
    except ValueError as error:
        raise ValueError("展示输出目录不得越出仓库") from error
    manifest = export_showcase(output_root)
    print(
        "P5_SHOWCASE_EXPORT PASS "
        f"route={manifest['classification_route']} "
        f"pages={manifest['page_count']} "
        f"question_marks={manifest['question_mark_replacement_count']} "
        f"final_pdf={manifest['final_pdf']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
