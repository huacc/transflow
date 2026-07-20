"""执行 P9 第一、二批混合 PDF，并发布项目内最终 PDF、PNG 与摘要。"""

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
from transflow.toolboxes.leaves import SingleFlowTextToolbox, build_p9_toolbox_factories
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

LOGGER = logging.getLogger("scripts.run_p9_acceptance")
REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
WORK_ROOT = REPO_ROOT / "tmp" / "pdfs" / "p9"
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf"
FONT_ID = "noto-sans-cjk-sc-regular"
ROUTES = (
    "visual_only",
    "body.flow_text.single",
    "body.chart",
    "body.diagram",
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)


def _sha256_file(path: Path) -> str:
    """流式计算真实 PDF 或资源文件哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_image_png(label: str) -> bytes:
    """生成内部含文字但只作为受保护像素的真实 PNG。"""

    with pymupdf.open() as document:
        page = document.new_page(width=260, height=130)
        page.insert_text((25, 65), label, fontsize=14)
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")


def build_mixed_source(path: Path) -> Path:
    """构造含 G8 四叶和 P9 六叶的十页完整 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        visual = document.new_page(width=420, height=600)
        visual.insert_image(
            pymupdf.Rect(55, 120, 365, 300),
            stream=_scan_image_png("VISUAL ONLY - NO OCR"),
        )

        single = document.new_page(width=420, height=600)
        single.insert_text((40, 28), "TRANSFLOW P9 HEADER", fontsize=8)
        single.insert_textbox(
            pymupdf.Rect(55, 110, 365, 185),
            "1. This paragraph proves the G8 single leaf still works.",
            fontsize=11,
        )
        single.insert_text((205, 575), "2", fontsize=8)

        chart = document.new_page(width=420, height=600)
        chart.draw_rect(pymupdf.Rect(75, 110, 345, 420), color=(0, 0, 0))
        chart.draw_line((105, 370), (315, 210), color=(0, 0, 1))
        chart.insert_text((170, 250), "Revenue chart", fontsize=11)

        diagram = document.new_page(width=420, height=600)
        diagram.draw_rect(pymupdf.Rect(90, 145, 255, 230), color=(0, 0, 0))
        diagram.draw_rect(pymupdf.Rect(90, 330, 255, 415), color=(0, 0, 0))
        diagram.draw_line((172, 230), (172, 330), color=(0, 0, 0))
        diagram.insert_text((130, 193), "Input", fontsize=11)
        diagram.insert_text((125, 380), "Output", fontsize=11)

        cover = document.new_page(width=420, height=600)
        cover.insert_image(pymupdf.Rect(300, 45, 375, 90), stream=_scan_image_png("LOGO"))
        cover.insert_text((55, 175), "TRANSFLOW ANNUAL REPORT", fontsize=22)
        cover.insert_text((55, 225), "Sustainable growth", fontsize=14)
        cover.insert_text((55, 275), "2026", fontsize=11)

        contents = document.new_page(width=420, height=600)
        contents.insert_text((45, 60), "CONTENTS", fontsize=18)
        for index, title in enumerate(("Overview", "Business Review", "Financial Statements")):
            y = 125 + index * 65
            contents.insert_text((55 + index * 12, y), title, fontsize=11)
            contents.insert_text((255, y), "........", fontsize=10)
            contents.insert_text((350, y), str(index + 7), fontsize=10)

        end = document.new_page(width=420, height=600)
        end.insert_image(pymupdf.Rect(95, 100, 325, 200), stream=_scan_image_png("QR AND LOGO"))
        end.insert_text((65, 300), "Contact: investor@example.com", fontsize=12)

        multi = document.new_page(width=420, height=600)
        multi.insert_textbox(
            pymupdf.Rect(40, 125, 190, 285),
            "Left column first paragraph.\nLeft column second paragraph.",
            fontsize=10,
        )
        multi.insert_textbox(
            pymupdf.Rect(230, 125, 380, 285),
            "Right column first paragraph.\nRight column second paragraph.",
            fontsize=10,
        )

        table = document.new_page(width=420, height=600)
        x_values, y_values = (45, 160, 275, 375), (120, 190, 260, 330)
        for x in x_values:
            table.draw_line((x, y_values[0]), (x, y_values[-1]), color=(0, 0, 0))
        for y in y_values:
            table.draw_line((x_values[0], y), (x_values[-1], y), color=(0, 0, 0))
        for row in range(3):
            for column in range(3):
                table.insert_text(
                    (x_values[column] + 10, y_values[row] + 35),
                    f"R{row + 1}C{column + 1}",
                    fontsize=9,
                )

        anchored = document.new_page(width=420, height=600)
        anchored.draw_rect(pymupdf.Rect(45, 120, 120, 200), color=(0, 0, 0))
        anchored.draw_rect(pymupdf.Rect(285, 310, 365, 390), color=(0, 0, 0))
        anchored.insert_textbox(
            pymupdf.Rect(130, 125, 245, 190),
            "First anchored block",
            fontsize=10,
        )
        anchored.insert_textbox(
            pymupdf.Rect(180, 340, 285, 390),
            "Second anchored block",
            fontsize=10,
        )

        # 新增页面后重新取得第六页，避免持有被 PyMuPDF 失效的旧 Page 句柄。
        contents_page = document[5]
        contents_page.insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": pymupdf.Rect(45, 110, 380, 145),
                "page": 6,
            }
        )
        document.save(path)
    return path


def execute_acceptance() -> dict[str, object]:
    """执行真实 P9 Pipeline，并把最终 Artifact 发布到固定项目输出目录。"""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"run-p9-acceptance-{timestamp}"
    source = build_mixed_source(WORK_ROOT / f"P9_second_batch_mixed_source_{timestamp}.pdf")
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-p9-acceptance",
        run_id=run_id,
    )
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    document_coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = document_coordinator.enumerate_pages(request)
    single = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY),
        fonts.resolve(FONT_ID).path,
    )
    batch = single.build_translation_request(single.prepare(pages[1].context, pages[1].facts))
    if batch is None:
        raise RuntimeError("P9 接受样本未形成 G8 single TranslationBatch")
    translations = {
        unit.unit_id: "1. 这是 P9 回归中真实执行的单栏固定译文。" for unit in batch.units
    }
    factories = build_p9_toolbox_factories(
        P8_POLICY,
        P9_POLICY,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    if not startup.ready:
        raise RuntimeError(f"P9 Catalog 启动校验失败: {startup.violations}")
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
        PyMuPdfPageRenderer(interpreter),
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
    output_pdf = OUTPUT_ROOT / "P9_second_batch_mixed_final.pdf"
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.write_bytes(artifacts.get(execution.final_artifact.artifact_id))
    preview_root = OUTPUT_ROOT / "P9_second_batch_mixed_preview"
    preview_root.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(output_pdf) as document:
        for page_number, page in enumerate(document, start=1):
            png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
            (preview_root / f"page-{page_number:02d}.png").write_bytes(png)
        link_target = document[5].get_links()[0]["page"] if document[5].get_links() else None
    summary: dict[str, object] = {
        "schema_version": "transflow.p9-runtime-acceptance/v1",
        "run_id": run_id,
        "source_path": source.relative_to(REPO_ROOT).as_posix(),
        "source_hash": request.source_hash,
        "output_path": output_pdf.relative_to(REPO_ROOT).as_posix(),
        "output_hash": _sha256_file(output_pdf),
        "catalog_hash": catalog.catalog_hash,
        "document_outcome": execution.result.outcome.value,
        "page_count": len(execution.pages),
        "preservation_passed": execution.preservation.passed,
        "contents_link_target_zero_based": link_target,
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
    (OUTPUT_ROOT / "P9_second_batch_mixed_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    """运行 P9 项目内可视验收并打印完整结构摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(json.dumps(execute_acceptance(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
