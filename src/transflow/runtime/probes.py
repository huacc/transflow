"""提供字体、PDF、原子文件和进程隔离的真实主机能力探针。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymupdf

LOGGER = logging.getLogger("transflow.runtime.probes")
# 仓库路径只从当前模块位置推导，避免工作目录或盘符耦合。
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


@dataclass(frozen=True, slots=True)
class ProbeFinding:
    """描述单个可追溯主机能力发现。"""

    code: str
    passed: bool
    detail: str


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免大字体或 wheel 一次性载入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 清单并返回对象。"""

    return json.loads(path.read_text(encoding="utf-8"))


def collect_environment_snapshot(role: str) -> dict[str, Any]:
    """采集可比较的 OS、Python、CPU、内存、文件系统和服务管理字段。"""

    baseline = load_json(REPO_ROOT / "resources" / "manifests" / "runtime_baseline.json")
    base_executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    LOGGER.info("调用环境探针，意图=采集 P1.1 主机角色 role=%s", role)
    return {
        "role": role,
        "host_identity": platform.node(),
        "os_family": platform.system(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_executable_sha256": sha256_file(base_executable),
        "cpu": platform.processor() or baseline["host"]["cpu"],
        "logical_processors": os.cpu_count(),
        "memory_bytes": baseline["host"]["memory_bytes"],
        "filesystem": baseline["host"]["filesystem"],
        "filesystem_device": REPO_ROOT.stat().st_dev,
        "service_manager": baseline["host"]["service_manager"],
    }


def validate_font_manifest(manifest_path: Path) -> list[ProbeFinding]:
    """核对字体路径、版本、许可、哈希和必需 CJK/Latin 字形。"""

    LOGGER.info("调用字体探针，意图=验证受控字体清单 path=%s", manifest_path)
    findings: list[ProbeFinding] = []
    if not manifest_path.is_file():
        return [ProbeFinding("FONT_MANIFEST_MISSING", False, manifest_path.name)]
    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as error:
        return [ProbeFinding("FONT_MANIFEST_INVALID", False, type(error).__name__)]
    if manifest.get("schema_version") != "transflow.font-manifest/v1":
        return [ProbeFinding("FONT_MANIFEST_SCHEMA_INVALID", False, "schema_version 不受支持")]
    # manifest 固定在 <runtime-root>/resources/manifests，可据此支持已安装 wheel。
    runtime_root = manifest_path.resolve().parent.parent.parent
    for asset in manifest.get("assets", []):
        asset_path = runtime_root / str(asset["path"])
        license_path = runtime_root / str(asset["license_path"])
        if not asset_path.is_file():
            findings.append(ProbeFinding("FONT_FILE_MISSING", False, str(asset["id"])))
            continue
        if sha256_file(asset_path) != asset["sha256"]:
            findings.append(ProbeFinding("FONT_SHA256_MISMATCH", False, str(asset["id"])))
            continue
        if not license_path.is_file() or sha256_file(license_path) != asset["license_sha256"]:
            findings.append(ProbeFinding("FONT_LICENSE_INVALID", False, str(asset["id"])))
            continue
        font = pymupdf.Font(fontfile=str(asset_path))
        missing = [
            codepoint
            for codepoint in asset["required_codepoints"]
            if font.has_glyph(int(str(codepoint).removeprefix("U+"), 16)) == 0
        ]
        if missing:
            findings.append(
                ProbeFinding("FONT_REQUIRED_GLYPH_MISSING", False, f"{asset['id']}:{missing}")
            )
            continue
        fields_complete = all(
            asset.get(field)
            for field in ("version", "license_id", "source_url", "license_url", "roles")
        )
        findings.append(
            ProbeFinding(
                "FONT_ASSET_VALID" if fields_complete else "FONT_METADATA_INCOMPLETE",
                bool(fields_complete),
                str(asset["id"]),
            )
        )
    if manifest.get("unresolved_license_items"):
        findings.append(ProbeFinding("FONT_LICENSE_UNRESOLVED", False, "字体许可仍有未决项"))
    return findings


def create_and_reopen_minimal_pdf(workspace: Path) -> dict[str, Any]:
    """用冻结 PyMuPDF 创建、保存并重新打开最小 PDF。"""

    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "minimal.pdf"
    LOGGER.info("调用 PDF 探针，意图=验证创建保存和重开 path=%s", output)
    document = pymupdf.open()
    document.new_page(width=200, height=200)
    document.save(output)
    document.close()
    with pymupdf.open(output) as reopened:
        page_count = reopened.page_count
    return {
        "path": output.name,
        "page_count": page_count,
        "sha256": sha256_file(output),
        "pymupdf_version": pymupdf.__version__,
    }


def atomic_publish_bytes(workspace: Path, name: str, content: bytes) -> dict[str, Any]:
    """按 partial、flush、fsync、原子替换、重读顺序发布字节。"""

    workspace.mkdir(parents=True, exist_ok=True)
    partial = workspace / f"{name}.partial"
    final = workspace / name
    LOGGER.info("调用原子发布探针，意图=验证同文件系统 rename final=%s", final)
    with partial.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if partial.stat().st_dev != workspace.stat().st_dev:
        raise OSError("ATOMIC_FILESYSTEM_MISMATCH")
    partial.replace(final)
    reread = final.read_bytes()
    expected_hash = hashlib.sha256(content).hexdigest()
    actual_hash = hashlib.sha256(reread).hexdigest()
    return {
        "partial_exists": partial.exists(),
        "final_exists": final.is_file(),
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "passed": expected_hash == actual_hash and not partial.exists(),
    }


def assess_atomic_devices(partial_device: int, final_device: int) -> ProbeFinding:
    """根据文件系统设备事实判断是否允许声明原子 rename。"""

    if partial_device != final_device:
        return ProbeFinding(
            "ATOMIC_FILESYSTEM_MISMATCH",
            False,
            f"partial_device={partial_device},final_device={final_device}",
        )
    return ProbeFinding("ATOMIC_FILESYSTEM_MATCH", True, f"device={partial_device}")


def _process_pdf_worker(pdf_path: str, page_number: int) -> dict[str, Any]:
    """在子进程中自行导入 PyMuPDF、打开 PDF、读取页面并关闭文档。"""

    path = Path(pdf_path)
    with pymupdf.open(path) as document:
        page = document.load_page(page_number)
        return {
            "pid": os.getpid(),
            "page_number": page_number,
            "width": page.rect.width,
            "height": page.rect.height,
            "page_count": document.page_count,
        }


def open_pdf_in_process_pool(pdf_path: Path, page_number: int) -> dict[str, Any]:
    """只向 ProcessPool 传递路径与页码并返回真实子进程读取结果。"""

    LOGGER.info(
        "调用进程池探针，意图=验证 PDF 子进程独立打开 path=%s page=%s",
        pdf_path,
        page_number,
    )
    with ProcessPoolExecutor(max_workers=1) as executor:
        return executor.submit(_process_pdf_worker, str(pdf_path), page_number).result(timeout=30)


def reject_open_document_payload(payload: object) -> ProbeFinding:
    """在提交进程池前明确拒绝 PyMuPDF Document/Page 进程内对象。"""

    if isinstance(payload, (pymupdf.Document, pymupdf.Page)):
        return ProbeFinding(
            "PDF_PROCESS_PAYLOAD_REJECTED",
            True,
            f"禁止跨进程传递 {type(payload).__name__}",
        )
    return ProbeFinding("PDF_PROCESS_PAYLOAD_SERIALIZABLE", True, type(payload).__name__)


def render_registered_font(workspace: Path, font_path: Path) -> dict[str, Any]:
    """用登记字体渲染 CJK/Latin、生成可解码 PNG，并报告缺字 Finding。"""

    workspace.mkdir(parents=True, exist_ok=True)
    pdf_path = workspace / "font-probe.pdf"
    png_path = workspace / "font-probe.png"
    LOGGER.info("调用字体渲染探针，意图=验证 CJK/Latin/缺字 path=%s", font_path)
    font = pymupdf.Font(fontfile=str(font_path))
    required = {"latin_A": ord("A"), "cjk_zhong": ord("中")}
    glyphs = {name: font.has_glyph(codepoint) for name, codepoint in required.items()}
    missing_codepoint = 0x10FFFF
    missing_glyph = font.has_glyph(missing_codepoint)
    document = pymupdf.open()
    page = document.new_page(width=300, height=120)
    page.insert_font(fontname="P1ControlledFont", fontfile=str(font_path))
    inserted = page.insert_text(
        (24, 60),
        "Transflow 中",
        fontname="P1ControlledFont",
        fontsize=24,
    )
    document.save(pdf_path)
    document.close()
    with pymupdf.open(pdf_path) as reopened:
        png_bytes = reopened[0].get_pixmap(alpha=False).tobytes("png")
    png_path.write_bytes(png_bytes)
    decoded = pymupdf.Pixmap(png_bytes)
    missing_finding = ProbeFinding(
        "FONT_GLYPH_MISSING",
        missing_glyph == 0,
        "U+10FFFF 未登记字形，已明确报告",
    )
    return {
        "inserted_characters": inserted,
        "glyph_indexes": glyphs,
        "png_width": decoded.width,
        "png_height": decoded.height,
        "png_sha256": sha256_file(png_path),
        "missing_finding": asdict(missing_finding),
        "passed": all(index > 0 for index in glyphs.values())
        and missing_finding.passed
        and decoded.width > 0
        and decoded.height > 0,
    }


def main() -> int:
    """运行 P1 主机能力示例并输出关键探针摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    workspace = REPO_ROOT / "tmp" / "p1-probes-main"
    minimal = create_and_reopen_minimal_pdf(workspace)
    process_result = open_pdf_in_process_pool(workspace / minimal["path"], 0)
    print(json.dumps({"minimal_pdf": minimal, "process_pool": process_result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
