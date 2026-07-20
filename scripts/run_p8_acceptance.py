"""执行 P8 第一批混合 PDF，并发布项目内最终 PDF、PNG 与摘要。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import pymupdf

from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import CheckpointCompatibility
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaves import SingleFlowTextToolbox, build_p8_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

LOGGER = logging.getLogger("scripts.run_p8_acceptance")
REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v3.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
WORK_ROOT = REPO_ROOT / "tmp" / "pdfs" / "p8"
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf"
ROUTES = (
    "visual_only",
    "body.flow_text.single",
    "body.chart",
    "body.diagram",
)
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    """流式计算文件哈希，避免把 PDF 一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_image_png() -> bytes:
    """生成一张文字只存在于像素中的 visual_only 测试图。"""

    with pymupdf.open() as document:
        page = document.new_page(width=320, height=180)
        page.insert_text((32, 70), "VISUAL ONLY - NO OCR", fontsize=17)
        page.draw_circle((160, 125), 28, color=(0, 0, 1))
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")


def build_mixed_source(path: Path) -> Path:
    """构造含 visual/single/chart/diagram 的四页完整 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        visual = document.new_page(width=420, height=600)
        visual.insert_image(pymupdf.Rect(45, 100, 375, 290), stream=_scan_image_png())

        single = document.new_page(width=420, height=600)
        single.insert_text((40, 28), "TRANSFLOW P8 HEADER", fontsize=8)
        single.insert_textbox(
            pymupdf.Rect(55, 110, 365, 185),
            "1. This paragraph is owned by the single-column toolbox.",
            fontsize=11,
        )
        single.insert_text((205, 575), "2", fontsize=8)
        single.draw_rect(pymupdf.Rect(350, 510, 390, 550), color=(0, 0, 0))

        chart = document.new_page(width=420, height=600)
        chart.draw_rect(pymupdf.Rect(75, 110, 345, 420), color=(0, 0, 0))
        chart.draw_line((105, 370), (315, 210), color=(0, 0, 1))
        chart.insert_text((170, 250), "Revenue chart", fontsize=11)
        chart.insert_text((115, 395), "2026", fontsize=9)

        diagram = document.new_page(width=420, height=600)
        diagram.draw_rect(pymupdf.Rect(90, 145, 255, 230), color=(0, 0, 0))
        diagram.draw_rect(pymupdf.Rect(90, 330, 255, 415), color=(0, 0, 0))
        diagram.draw_line((172, 230), (172, 330), color=(0, 0, 0))
        diagram.insert_text((130, 193), "Input", fontsize=11)
        diagram.insert_text((125, 380), "Output", fontsize=11)
        document.save(path)
    return path


def execute_acceptance() -> dict[str, object]:
    """执行真实 P8 Pipeline 并把最终 Artifact 复制到固定项目输出目录。"""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"run-p8-acceptance-{timestamp}"
    source = build_mixed_source(WORK_ROOT / f"P8_first_batch_mixed_source_{timestamp}.pdf")
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-p8-acceptance",
        run_id=run_id,
    )
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    renderer = PyMuPdfPageRenderer(interpreter)
    document_coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = document_coordinator.enumerate_pages(request)
    policy = load_p8_toolbox_policy(POLICY_PATH)
    single = SingleFlowTextToolbox(policy, fonts.resolve(FONT_ID).path)
    template = single.prepare(pages[1].context, pages[1].facts)
    batch = single.build_translation_request(template)
    if batch is None:
        raise RuntimeError("P8 接受样本未形成 single TranslationBatch")
    translations = {unit.unit_id: "1. 这是 P8 单栏正文的真实固定译文。" for unit in batch.units}
    factories = build_p8_toolbox_factories(POLICY_PATH, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    if not startup.ready:
        raise RuntimeError(f"P8 Catalog 启动校验失败: {startup.violations}")
    run_root = WORK_ROOT / "runs" / run_id
    artifacts = SharedFilesystemArtifactAdapter(run_root, run_id)
    checkpoints = FilesystemCheckpointAdapter(run_root, run_id, artifacts)
    compatibility = CheckpointCompatibility(
        source_hash=request.source_hash,
        config_hash=request.config_snapshot_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=catalog.catalog_hash,
        schema_hash=_sha256_file(SCHEMA_PATH),
    )
    pipeline = ToolboxPagePipeline(
        catalog,
        ToolboxPageCoordinator(FixedTranslationAdapter(translations)),
        renderer,
        PreviewPublisher(artifacts),
        checkpoints,
        compatibility,
    )
    route_by_page = dict(enumerate(ROUTES, start=1))
    execution = document_coordinator.run(
        request,
        lambda page: route_by_page[page.context.page_no],
        pipeline,
        DocumentFinalizer(interpreter, artifacts, run_root),
    )
    output_pdf = OUTPUT_ROOT / "P8_first_batch_mixed_final.pdf"
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.write_bytes(artifacts.get(execution.final_artifact.artifact_id))
    preview_root = OUTPUT_ROOT / "P8_first_batch_mixed_preview"
    preview_root.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(output_pdf) as document:
        for page_number, page in enumerate(document, start=1):
            png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
            (preview_root / f"page-{page_number:02d}.png").write_bytes(png)
    summary: dict[str, object] = {
        "schema_version": "transflow.p8-runtime-acceptance/v1",
        "run_id": run_id,
        "source_path": source.relative_to(REPO_ROOT).as_posix(),
        "source_hash": request.source_hash,
        "output_path": output_pdf.relative_to(REPO_ROOT).as_posix(),
        "output_hash": _sha256_file(output_pdf),
        "catalog_hash": catalog.catalog_hash,
        "document_outcome": execution.result.outcome.value,
        "page_count": len(execution.pages),
        "preservation_passed": execution.preservation.passed,
        "pages": [
            {
                "page_no": page.page_no,
                "route": page.route,
                "toolbox_id": page.toolbox_id,
                "toolbox_version": page.toolbox_version,
                "fallback": page.outcome.fallback.value,
                "finding_codes": page.outcome.finding_codes,
                "patch_operations": 0 if page.patch is None else len(page.patch.operations),
            }
            for page in execution.pages
        ],
    }
    (OUTPUT_ROOT / "P8_first_batch_mixed_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    """运行 P8 项目内可视验收并打印完整结构摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    summary = execute_acceptance()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
