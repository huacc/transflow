"""导出项目内可直接查看的 P6 Preservation PDF 与 Gate 证据副本。"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import pymupdf

from transflow.pdf_kernel.passthrough import publish_source_passthrough
from transflow.pdf_kernel.preservation import DEFAULT_SUPPORT_MATRIX, preflight_document

LOGGER = logging.getLogger("transflow.p6.export")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "p6"
GATE_EVIDENCE = REPO_ROOT / "docs" / "reports" / "gates" / "G6_evidence.json"


def _create_feature_fixture(path: Path) -> None:
    """创建含书签、链接、批注、表单、附件和页面标签的真实 F3 PDF。"""

    with pymupdf.open() as document:
        first = document.new_page(width=420, height=600)
        first.insert_text((40, 60), "P6 PDF Preservation", fontsize=18)
        first.insert_text((40, 90), "Metadata / bookmark / link / annotation / form / attachment")
        second = document.new_page(width=420, height=600)
        second.insert_text((40, 60), "Preserved second page", fontsize=14)
        document.set_metadata({"title": "Transflow P6 showcase", "author": "Transflow"})
        document.set_toc([[1, "Preservation", 1], [1, "Second page", 2]])
        document.set_page_labels(
            [{"startpage": 0, "prefix": "P6-", "style": "D", "firstpagenum": 1}]
        )
        first = document[0]
        first.insert_link(
            {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(40, 110, 180, 130), "page": 1}
        )
        first.add_text_annot((200, 120), "P6 annotation")
        widget = pymupdf.Widget()
        widget.field_name = "preservation_status"
        widget.field_type = 7
        widget.field_value = "verified"
        widget.rect = pymupdf.Rect(40, 150, 220, 180)
        first.add_widget(widget)
        document.embfile_add("evidence.txt", b"Transflow P6 Preservation evidence")
        document.save(path)


def _sha256_file(path: Path) -> str:
    """流式计算导出文件哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_results() -> dict[str, object]:
    """生成源/透传 PDF、首屏 PNG、Gate 证据副本和可复算 manifest。"""

    source_root = OUTPUT_ROOT / "source"
    final_root = OUTPUT_ROOT / "final"
    result_root = OUTPUT_ROOT / "test-results"
    for directory in (source_root, final_root, result_root):
        directory.mkdir(parents=True, exist_ok=True)
    source = source_root / "p6_preservation_showcase_source.pdf"
    target = final_root / "p6_preservation_showcase_passthrough.pdf"
    preview = final_root / "p6_preservation_showcase_passthrough.png"
    LOGGER.info("调用 P6 结果导出，意图=生成可视 Preservation 夹具")
    _create_feature_fixture(source)
    preflight = preflight_document(source, support_matrix_path=DEFAULT_SUPPORT_MATRIX)
    evidence = publish_source_passthrough(source, target, final_root)
    with pymupdf.open(target) as document:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        pixmap.save(preview)
        page_count = document.page_count
    gate_target = result_root / GATE_EVIDENCE.name
    shutil.copyfile(GATE_EVIDENCE, gate_target)
    manifest = {
        "schema_version": "transflow.p6-export/v1",
        "preflight_decision": preflight.decision,
        "preflight_reason_codes": preflight.reason_codes,
        "source_pdf": source.relative_to(REPO_ROOT).as_posix(),
        "source_sha256": _sha256_file(source),
        "final_pdf": target.relative_to(REPO_ROOT).as_posix(),
        "final_sha256": _sha256_file(target),
        "preview_png": preview.relative_to(REPO_ROOT).as_posix(),
        "page_count": page_count,
        "byte_identity": evidence.source_hash == evidence.target_hash,
        "gate_evidence": gate_target.relative_to(REPO_ROOT).as_posix(),
    }
    (OUTPUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "README.md").write_text(
        "# P6 可查看结果\n\n"
        "本目录展示 Preservation F3 PDF 的项目内源文件、字节级整文透传结果、首屏 PNG "
        "以及 G6 原始 Gate 证据。该 PDF 用于验证 P6 文档特性保留，不代表 P7 之后的排版能力。\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    """导出结果并仅记录无秘密的相对路径与哈希摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    manifest = export_results()
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
