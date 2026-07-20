"""按 P6.1～P6.5 验收 SharedPdfKernel 与 PDF Preservation。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from scripts import verify_p4, verify_p5, verify_p6
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import CheckpointCompatibility, ensure_checkpoint_compatible
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel import (
    BoundedRepairController,
    ConstraintChecker,
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PreflightDecision,
    PyMuPdfPageRenderer,
    RepairLimits,
    ReplayPage,
    WorkspaceAllocator,
    build_kernel_fingerprint,
    build_patch_manifest,
    load_support_matrix,
    patch_operation_hash,
    preflight_document,
    shrink_font_patch,
    validate_preservation,
)
from transflow.pdf_kernel.facts import (
    ExtractedPageFacts,
    extract_page_contract_bytes,
    serialize_kernel_contract,
)
from transflow.pdf_kernel.models import KernelFinding
from transflow.pdf_kernel.passthrough import publish_source_passthrough
from transflow.pdf_kernel.preservation import capture_document_structure

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
SUPPORT_MATRIX = REPO_ROOT / "resources" / "manifests" / "p6_preservation_support.json"
MIGRATION_MANIFEST = REPO_ROOT / "docs" / "迁移" / "p6_pdf_kernel_migration.json"
DETERMINISM_MANIFEST = (
    REPO_ROOT / "resources" / "manifests" / "p6_determinism_contract.json"
)
SPIKE_SOURCE = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "src"
FONT_ID = "noto-sans-cjk-sc-regular"
OWNER = "body.flow_text.single"
HASH_A = "a" * 64


def sha256_file(path: Path) -> str:
    """流式计算测试输入或输出的真实 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def terminate_pdf_worker() -> None:
    """仅供故障注入子进程使用：立即终止当前 worker，不在主进程调用。"""

    os._exit(17)


def create_kernel_pdf(path: Path, *, pages: int = 2) -> Path:
    """生成含文本、图片、绘图、表格、旋转和 CropBox 的真实 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    image.clear_with(0x3377AA)
    image_bytes = image.tobytes("png")
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=420, height=600)
            page.insert_text((40, 60), f"Revenue page {index + 1}", fontsize=11)
            page.insert_image(pymupdf.Rect(300, 40, 340, 80), stream=image_bytes)
            page.draw_rect(pymupdf.Rect(40, 120, 180, 190), color=(0, 0, 0))
            for offset in (0, 70, 140):
                page.draw_line((40 + offset, 220), (40 + offset, 300), color=(0, 0, 0))
            for offset in (0, 40, 80):
                page.draw_line((40, 220 + offset), (180, 220 + offset), color=(0, 0, 0))
            page.insert_text((50, 245), "A1", fontsize=9)
            page.insert_text((120, 245), "B1", fontsize=9)
            if index == 0:
                page.set_cropbox(pymupdf.Rect(10, 10, 410, 590))
                page.set_rotation(90)
        document.save(path)
    return path


def create_feature_pdf(path: Path, *, signature: bool = False) -> Path:
    """生成带 metadata、书签、链接、注释、表单和附件的真实 F3 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        first = document.new_page(width=420, height=600)
        first.insert_text((40, 60), "Feature page one", fontsize=11)
        second = document.new_page(width=420, height=600)
        second.insert_text((40, 60), "Feature page two", fontsize=11)
        document.set_metadata({"title": "P6 preservation fixture", "author": "Transflow"})
        document.set_toc([[1, "First", 1], [1, "Second", 2]])
        document.set_page_labels(
            [{"startpage": 0, "prefix": "P6-", "style": "D", "firstpagenum": 1}]
        )
        first = document[0]
        first.insert_link(
            {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(40, 80, 160, 100), "page": 1}
        )
        first.add_text_annot((180, 90), "P6 annotation")
        widget = pymupdf.Widget()
        widget.field_name = "approval"
        widget.field_type = (
            6 if signature else 7
        )
        widget.rect = pymupdf.Rect(40, 120, 180, 145)
        if not signature:
            widget.field_value = "approved"
        first.add_widget(widget)
        document.embfile_add("evidence.txt", b"p6 attachment evidence")
        document.save(path)
    return path


def create_encrypted_pdf(path: Path, password: str) -> Path:
    """生成需要密码认证、但使用正确密码可读取的真实加密 PDF。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), "Encrypted P6 fixture")
        document.save(
            path,
            encryption=5,
            owner_pw=password,
            user_pw=password,
            permissions=512,
        )
    return path


def facts_context(
    path: Path,
    page_no: int = 1,
) -> tuple[ExtractedPageFacts, PageExecutionContext]:
    """提取真实 Facts，并建立与其严格绑定的页面上下文。"""

    source_hash = sha256_file(path)
    facts = PageFactsExtractor().extract_page(path, source_hash, page_no)
    context = PageExecutionContext(
        job_id="job-p6",
        run_id="run-p6",
        source_hash=source_hash,
        page_no=page_no,
        geometry_hash=facts.page.geometry_hash,
        config_snapshot_hash=HASH_A,
    )
    return facts, context


def make_patch(
    facts: ExtractedPageFacts,
    context: PageExecutionContext,
    *,
    text: str = "译文",
    font_id: str = FONT_ID,
    owner: str = OWNER,
    rect: tuple[float, float, float, float] | None = None,
    target_id: str | None = None,
) -> PagePatch:
    """基于首个真实文本对象建立一个声明式 replace_text Patch。"""

    source_object = next(item for item in facts.objects if not item.protected and item.text)
    selected_rect = rect or source_object.bbox
    selected_target = target_id or source_object.object_id
    payload_hash = patch_operation_hash(
        owner=owner,
        target_object_ids=(selected_target,),
        rect=selected_rect,
        replacement_text=text,
        font_id=font_id,
        font_size=9.0,
    )
    operation = PatchOperation(
        operation_id="p6-operation",
        region_id="p6-region",
        kind="replace_text",
        payload_hash=payload_hash,
        owner=owner,
        target_object_ids=(selected_target,),
        rect=selected_rect,
        replacement_text=text,
        font_id=font_id,
        font_size=9.0,
    )
    return PagePatch(
        patch_id="p6-patch",
        source_hash=context.source_hash,
        page_no=context.page_no,
        geometry_hash=context.geometry_hash,
        owner=owner,
        operations=(operation,),
    )


def spike_facts(path: Path) -> dict[str, object]:
    """在隔离子进程调用 spike Kernel，只返回可比较的机械事实摘要。"""

    code = """
import json
import sys
from pathlib import Path
from shared_pdf_kernel.facts import extract_page_facts
facts = extract_page_facts(Path(sys.argv[1]))
print(json.dumps({
    'source_hash': facts.source_pdf_sha256,
    'rotation': facts.rotation,
    'text_count': len(facts.text_objects),
    'image_count': len(facts.image_objects),
    'drawing_count': len(facts.drawing_objects),
    'first_text': facts.text_objects[0].text,
    'first_bbox': facts.text_objects[0].bbox,
}, sort_keys=True))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SPIKE_SOURCE)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.contract
def test_p6_1_t01_spike_and_production_facts_match_with_declared_differences(
    tmp_path: Path,
) -> None:
    """P6.1-T01：spike 与生产机械事实等价，身份和边界差异均在迁移表声明。"""

    source = create_kernel_pdf(tmp_path / "source.pdf", pages=1)
    production, _ = facts_context(source)
    spike = spike_facts(source)
    migration = json.loads(MIGRATION_MANIFEST.read_text(encoding="utf-8"))
    assert spike["source_hash"] == production.page.source_hash
    assert spike["rotation"] == production.rotation
    assert spike["text_count"] == len(production.text_spans)
    assert spike["image_count"] == len(production.image_objects)
    assert isinstance(spike["drawing_count"], int)
    assert 0 < spike["drawing_count"] <= len(production.drawing_objects)
    assert spike["first_text"] == production.text_spans[0].text
    first_bbox = spike["first_bbox"]
    assert isinstance(first_bbox, list)
    assert tuple(first_bbox) == production.text_spans[0].bbox
    assert len(migration["approved_differences"]) == 6
    for unit in migration["units"]:
        source_file = SPIKE_SOURCE / "shared_pdf_kernel" / unit["source"]
        assert sha256_file(source_file) == unit["source_sha256"]


@pytest.mark.contract
def test_p6_1_t02_facts_ids_and_serialization_are_stable_across_processes(
    tmp_path: Path,
) -> None:
    """P6.1-T02：同页跨运行、跨进程返回相同身份和规范序列化字节。"""

    source = create_kernel_pdf(tmp_path / "stable.pdf", pages=2)
    source_hash = sha256_file(source)
    local = extract_page_contract_bytes(str(source), source_hash, 1)
    with ProcessPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                extract_page_contract_bytes,
                (str(source), str(source)),
                (source_hash, source_hash),
                (1, 1),
            )
        )
    assert results == (local, local)


@pytest.mark.integration
def test_p6_1_t03_workspace_isolates_runs_pages_logs_and_temp(tmp_path: Path) -> None:
    """P6.1-T03：同名 PDF 的并发 run 与多页目录完全隔离。"""

    allocator = WorkspaceAllocator(tmp_path / "workspace")
    with ThreadPoolExecutor(max_workers=2) as executor:
        workspaces = tuple(
            executor.map(lambda run_id: allocator.allocate("job-p6", run_id), ("run-a", "run-b"))
        )
    first, second = workspaces
    assert first.run_root != second.run_root
    assert first.page_root(1) != first.page_root(2)
    assert first.temp_root != second.temp_root
    assert first.reports_root != second.reports_root
    assert first.final_root != second.final_root


@pytest.mark.contract
def test_p6_1_t04_facts_extraction_never_modifies_source(tmp_path: Path) -> None:
    """P6.1-T04：重复单页与整本提取前后源 PDF 字节哈希不变。"""

    source = create_kernel_pdf(tmp_path / "readonly.pdf")
    before = sha256_file(source)
    before_mtime_ns = source.stat().st_mtime_ns
    extractor = PageFactsExtractor()
    extractor.extract_all(source, before)
    extractor.extract_page(source, before, 1)
    assert sha256_file(source) == before
    assert source.stat().st_mtime_ns == before_mtime_ns


@pytest.mark.integration
def test_p6_1_t05_rotation_crop_image_drawing_table_and_locked_facts_exist(
    tmp_path: Path,
) -> None:
    """P6.1-T05：真实组合夹具覆盖旋转、裁剪、图片、绘图、表格和锁定哈希。"""

    source = create_kernel_pdf(tmp_path / "facts.pdf", pages=1)
    facts, _ = facts_context(source)
    assert facts.rotation == 90
    assert facts.crop_box != facts.media_box
    assert facts.image_objects
    assert facts.drawing_objects
    assert facts.table_objects
    assert len(facts.locked_objects_hash) == 64


@pytest.mark.contract
def test_p6_1_t06_open_document_and_page_are_rejected_at_serialization_boundary(
    tmp_path: Path,
) -> None:
    """P6.1-T06：Kernel 序列化边界拒绝 PyMuPDF Document 与 Page。"""

    source = create_kernel_pdf(tmp_path / "boundary.pdf", pages=1)
    with pymupdf.open(source) as document:
        with pytest.raises(DomainContractError) as document_error:
            serialize_kernel_contract(document)
        with pytest.raises(DomainContractError) as page_error:
            serialize_kernel_contract(document[0])
    assert document_error.value.code is ErrorCode.PORT_CONTRACT_VIOLATION
    assert page_error.value.code is ErrorCode.PORT_CONTRACT_VIOLATION


@pytest.mark.contract
def test_p6_2_t01_candidate_and_final_share_patch_manifest_semantics(tmp_path: Path) -> None:
    """P6.2-T01：candidate 与 final 使用同一解释器、owner 和操作顺序。"""

    source = create_kernel_pdf(tmp_path / "shared.pdf", pages=2)
    facts, context = facts_context(source, page_no=2)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    patch = make_patch(facts, context, text="OK", rect=(40, 40, 180, 80))
    interpreter = PagePatchInterpreter(fonts)
    renderer = PyMuPdfPageRenderer(interpreter)
    candidate = renderer.render_candidate(source, context, facts, patch, OWNER)
    target = tmp_path / "final.pdf"
    with pymupdf.open(source) as final_document:
        final_application = interpreter.apply(
            final_document,
            context,
            facts,
            patch,
            OWNER,
        )
        final_text = final_document[1].get_text()
        final_geometry = tuple(final_document[1].rect)
        final_document.save(target)
    manifest = build_patch_manifest(patch)
    assert candidate.application is not None
    assert candidate.application == final_application
    assert candidate.application.patch_manifest_hash == manifest.manifest_hash
    assert candidate.application.operation_ids == manifest.operation_ids
    assert "OK" in final_text
    with pymupdf.open(target) as published:
        assert tuple(published[1].rect) == final_geometry


@pytest.mark.contract
def test_p6_2_t02_manifest_font_covers_cjk_latin_numeric_and_financial_symbols() -> None:
    """P6.2-T02：受控字体真实覆盖中英文、数字和常用财务符号。"""

    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    probe = fonts.probe(FONT_ID, "收入 Revenue 2026 ¥$€%")
    assert probe.covers_text
    assert probe.glyph_count is not None and probe.glyph_count > 0
    assert fonts.system_probe_count == 0


@pytest.mark.contract
def test_p6_2_t03_missing_font_hash_file_and_glyph_fail_without_system_fallback(
    tmp_path: Path,
) -> None:
    """P6.2-T03：缺登记、错哈希、缺文件、缺字形均显式失败且系统探测为零。"""

    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    assert not fonts.probe("missing-font", "中文").registered
    assert fonts.probe(FONT_ID, "\U0010ffff").missing_codepoints
    original = json.loads(FONT_MANIFEST.read_text(encoding="utf-8"))
    original["assets"][0]["sha256"] = "0" * 64
    bad_manifest = tmp_path / "bad-fonts.json"
    bad_manifest.write_text(json.dumps(original), encoding="utf-8")
    invalid = ControlledFontRegistry(bad_manifest, REPO_ROOT)
    assert not invalid.probe(FONT_ID, "A").integrity_passed
    original["assets"][0]["path"] = "resources/fonts/missing.otf"
    missing_manifest = tmp_path / "missing-fonts.json"
    missing_manifest.write_text(json.dumps(original), encoding="utf-8")
    missing = ControlledFontRegistry(missing_manifest, REPO_ROOT)
    assert not missing.probe(FONT_ID, "A").integrity_passed
    assert fonts.system_probe_count == invalid.system_probe_count == missing.system_probe_count == 0


@pytest.mark.contract
def test_p6_2_t04_protected_owner_and_bounds_reject_before_source_commit(tmp_path: Path) -> None:
    """P6.2-T04：保护对象、错误 owner 和越界 Patch 被拒绝，源文件零修改。"""

    source = create_kernel_pdf(tmp_path / "reject.pdf", pages=1)
    before = sha256_file(source)
    facts, context = facts_context(source)
    checker = ConstraintChecker(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    protected = facts.drawing_objects[0].object_id
    protected_patch = make_patch(facts, context, target_id=protected)
    owner_patch = make_patch(facts, context, owner="wrong-owner")
    outside_patch = make_patch(facts, context, rect=(20, 20, 430, 100))
    codes = {
        item.code
        for patch, expected in (
            (protected_patch, OWNER),
            (owner_patch, OWNER),
            (outside_patch, OWNER),
        )
        for item in checker.check_patch(context, facts, patch, expected)
    }
    assert {"PROTECTED_OBJECT", "PATCH_OWNER_MISMATCH", "PAGE_BOUNDS_EXCEEDED"} <= codes
    assert sha256_file(source) == before


@pytest.mark.contract
def test_p6_2_t05_forbidden_renderers_and_system_fonts_are_absent(tmp_path: Path) -> None:
    """P6.2-T05：生产路径禁止 HTML、Chrome、系统字体与页级 PDF 合并。"""

    assert verify_p4.kernel_boundary_violations() == []
    assert verify_p4.finalizer_boundary_violations() == []
    injected = tmp_path / "kernel"
    injected.mkdir()
    (injected / "bad.py").write_text("page.insert_htmlbox(rect, text)\n", encoding="utf-8")
    assert verify_p6.forbidden_api_violations(injected) == ["HTML_RENDERER:bad.py"]


@pytest.mark.integration
def test_p6_2_t06_repeated_candidate_render_has_identical_manifest_and_pixels(
    tmp_path: Path,
) -> None:
    """P6.2-T06：相同输入重复渲染的 PNG、操作 manifest 和配置哈希一致。"""

    source = create_kernel_pdf(tmp_path / "repeat.pdf", pages=1)
    facts, context = facts_context(source)
    patch = make_patch(facts, context)
    renderer = PyMuPdfPageRenderer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    )
    first = renderer.render_candidate(source, context, facts, patch, OWNER)
    second = renderer.render_candidate(source, context, facts, patch, OWNER)
    final_paths = (tmp_path / "first-final.pdf", tmp_path / "second-final.pdf")
    for final_path in final_paths:
        shutil.copyfile(source, final_path)
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)).replay_document(
            final_path,
            (ReplayPage(context, facts, patch, OWNER),),
        )
    first_structure = capture_document_structure(final_paths[0])
    second_structure = capture_document_structure(final_paths[1])
    determinism = json.loads(DETERMINISM_MANIFEST.read_text(encoding="utf-8"))
    assert first.png_bytes == second.png_bytes
    assert first.application == second.application
    assert first_structure == second_structure
    assert determinism["tolerances"] == {
        "png_outside_allowed_changed_pixel_ratio_max": 0.0,
        "pdf_page_count_difference_max": 0,
        "pdf_geometry_difference_max": 0,
        "pdf_feature_hash_difference_max": 0,
    }
    assert determinism["allowed_pdf_byte_differences"]


@pytest.mark.contract
def test_p6_3_t01_hard_constraints_emit_structured_findings(tmp_path: Path) -> None:
    """P6.3-T01：边界、溢出、重叠、错误字体和错误绑定均产生结构化 Finding。"""

    source = create_kernel_pdf(tmp_path / "constraints.pdf", pages=1)
    facts, context = facts_context(source)
    checker = ConstraintChecker(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    patch = make_patch(
        facts,
        context,
        text="very long translated content " * 80,
        font_id="missing-font",
        rect=(20, 20, 430, 35),
    )
    patch = replace(patch, source_hash="b" * 64, owner="wrong-owner")
    codes = {item.code for item in checker.check_patch(context, facts, patch, OWNER)}
    overflow_patch = make_patch(
        facts,
        context,
        text="overflow content " * 40,
        rect=(30, 40, 90, 58),
    )
    codes.update(item.code for item in checker.check_patch(context, facts, overflow_patch, OWNER))
    first_operation = make_patch(facts, context).operations[0]
    overlapping_patch = replace(
        make_patch(facts, context),
        operations=(first_operation, replace(first_operation, operation_id="p6-operation-2")),
    )
    codes.update(
        item.code for item in checker.check_patch(context, facts, overlapping_patch, OWNER)
    )
    residual_candidate = tmp_path / "residual.pdf"
    shutil.copyfile(source, residual_candidate)
    codes.update(
        item.code
        for item in checker.check_candidate(
            source,
            residual_candidate,
            facts,
            make_patch(facts, context),
            None,
        )
    )
    locked_candidate = tmp_path / "locked-changed.pdf"
    shutil.copyfile(source, locked_candidate)
    with pymupdf.open(locked_candidate) as document:
        document[0].draw_rect(pymupdf.Rect(210, 350, 260, 390), color=(1, 0, 0))
        document.saveIncr()
    codes.update(
        item.code
        for item in checker.check_candidate(
            source,
            locked_candidate,
            facts,
            make_patch(facts, context),
            None,
        )
    )
    broken_candidate = tmp_path / "broken.pdf"
    broken_candidate.write_bytes(b"broken candidate")
    codes.update(
        item.code
        for item in checker.check_candidate(
            source,
            broken_candidate,
            facts,
            make_patch(facts, context),
            None,
        )
    )
    assert {"SOURCE_HASH_MISMATCH", "PATCH_OWNER_MISMATCH", "PAGE_BOUNDS_EXCEEDED"} <= codes
    assert {
        "FONT_NOT_REGISTERED",
        "TEXT_FIT_OVERFLOW",
        "WRITE_OVERLAP",
        "SOURCE_TEXT_RESIDUAL",
        "LOCKED_OBJECTS_CHANGED",
        "CANDIDATE_UNREADABLE",
    } <= codes


@pytest.mark.contract
def test_p6_3_t02_multiple_findings_are_retained_and_stably_sorted(tmp_path: Path) -> None:
    """P6.3-T02：同一错误 Patch 的多个 Finding 全部保留且重复检查顺序稳定。"""

    source = create_kernel_pdf(tmp_path / "stable-findings.pdf", pages=1)
    facts, context = facts_context(source)
    checker = ConstraintChecker(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    patch = replace(make_patch(facts, context, font_id="missing"), owner="wrong")
    first = checker.check_patch(context, facts, patch, OWNER)
    second = checker.check_patch(context, facts, patch, OWNER)
    assert first == second
    assert len(first) >= 2
    assert tuple(item.code for item in first) == tuple(sorted(item.code for item in first))


@pytest.mark.contract
def test_p6_3_t03_wrong_source_page_owner_and_protected_target_never_write(
    tmp_path: Path,
) -> None:
    """P6.3-T03：错误 source/page/owner/protected 绑定在解释器写入前拒绝。"""

    source = create_kernel_pdf(tmp_path / "preapply.pdf", pages=1)
    before = sha256_file(source)
    facts, context = facts_context(source)
    interpreter = PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    valid = make_patch(facts, context)
    variants = (
        replace(valid, source_hash="b" * 64),
        replace(valid, page_no=2),
        replace(valid, owner="wrong"),
        make_patch(facts, context, target_id=facts.drawing_objects[0].object_id),
    )
    for patch in variants:
        with pymupdf.open(source) as document, pytest.raises(DomainContractError):
            interpreter.apply(document, context, facts, patch, OWNER)
    assert sha256_file(source) == before


@pytest.mark.contract
def test_p6_3_t04_bounded_repair_rechecks_full_constraints_before_accepting(
    tmp_path: Path,
) -> None:
    """P6.3-T04：每轮调用完整检查，只有硬 Finding 严格减少到零才接受。"""

    source = create_kernel_pdf(tmp_path / "repair.pdf", pages=1)
    facts, context = facts_context(source)
    checker = ConstraintChecker(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    base = make_patch(facts, context, text="修复", rect=(30, 40, 160, 70))
    operation = base.operations[0]
    assert operation.owner and operation.rect and operation.font_id
    initial_size = 36.0
    initial_operation = replace(
        operation,
        font_size=initial_size,
        payload_hash=patch_operation_hash(
            owner=operation.owner,
            target_object_ids=operation.target_object_ids,
            rect=operation.rect,
            replacement_text="修复",
            font_id=operation.font_id,
            font_size=initial_size,
        ),
    )
    other_object = next(
        item
        for item in facts.objects
        if not item.protected and item.object_id != operation.target_object_ids[0]
    )
    invalid_hash = patch_operation_hash(
        owner=OWNER,
        target_object_ids=(other_object.object_id,),
        rect=other_object.bbox,
        replacement_text="invalid",
        font_id="missing-font",
        font_size=9.0,
    )
    invalid_operation = PatchOperation(
        operation_id="p6-invalid-font",
        region_id="p6-invalid-region",
        kind="replace_text",
        payload_hash=invalid_hash,
        owner=OWNER,
        target_object_ids=(other_object.object_id,),
        rect=other_object.bbox,
        replacement_text="invalid",
        font_id="missing-font",
        font_size=9.0,
    )
    initial = replace(base, operations=(initial_operation, invalid_operation))
    # 第一轮局部撤销非法字体操作，第二轮机械缩放剩余操作字号。
    first_patch = replace(initial, operations=(initial_operation,))
    second_patch = shrink_font_patch(first_patch, 0.25, 6.0)
    initial_findings = checker.check_patch(context, facts, initial, OWNER)
    calls: list[str] = []

    def first_check() -> tuple[KernelFinding, ...]:
        """返回一个真实结构化硬 Finding，记录完整复检发生。"""

        calls.append("first")
        return checker.check_patch(context, facts, first_patch, OWNER)

    def second_check() -> tuple[KernelFinding, ...]:
        """返回空 Finding，表示全部机械约束已经重新通过。"""

        calls.append("second")
        return checker.check_patch(context, facts, second_patch, OWNER)

    controller = BoundedRepairController(RepairLimits(3, 3, 1000, 6.0))
    decision = controller.run(
        "initial",
        initial_findings,
        (("candidate-1", 1, first_check), ("candidate-2", 1, second_check)),
    )
    assert decision.accepted
    assert decision.selected_candidate_ref in {"candidate-1", "candidate-2"}
    assert calls in (["first"], ["first", "second"])


@pytest.mark.contract
def test_p6_3_t05_irreparable_and_looping_repair_stop_within_budget(
    tmp_path: Path,
) -> None:
    """P6.3-T05：无改善和超预算 Repair 均在固定轮次与操作数内停止。"""

    source = create_kernel_pdf(tmp_path / "irreparable.pdf", pages=1)
    facts, context = facts_context(source)
    checker = ConstraintChecker(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT))
    patch = make_patch(facts, context, font_id="missing-font")
    findings = checker.check_patch(context, facts, patch, OWNER)
    controller = BoundedRepairController(RepairLimits(2, 2, 1000, 6.0))
    no_change = controller.run(
        "source",
        findings,
        (("same", 1, lambda: checker.check_patch(context, facts, patch, OWNER)),),
    )
    over_budget = controller.run(
        "source",
        findings,
        (("too-large", 3, lambda: ()),),
    )
    assert no_change.outcome == "NO_IMPROVEMENT" and no_change.operations_used == 1
    assert over_budget.outcome == "BUDGET_EXHAUSTED" and over_budget.operations_used == 0


@pytest.mark.contract
def test_p6_3_t06_kernel_constraints_have_no_classification_or_toolbox_semantics() -> None:
    """P6.3-T06：约束与 Repair 不导入分类、Toolbox 或具体页面 Route。"""

    paths = (
        REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "constraints.py",
        REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "repair.py",
    )
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "transflow.classification" not in content
    assert "transflow.toolboxes" not in content
    assert "body.flow_text" not in content


@pytest.mark.integration
def test_p6_4_t01_f3_features_match_support_matrix_after_byte_copy(tmp_path: Path) -> None:
    """P6.4-T01：F3 的几何、metadata、书签、链接、注释、表单、附件逐项保持。"""

    source = create_feature_pdf(tmp_path / "features.pdf")
    target = tmp_path / "features-copy.pdf"
    shutil.copyfile(source, target)
    matrix = load_support_matrix(SUPPORT_MATRIX)
    source_structure = capture_document_structure(source, support_matrix=matrix)
    target_structure = capture_document_structure(target, support_matrix=matrix)
    result = validate_preservation(source_structure, target_structure, frozenset(), matrix)
    assert result.passed
    assert set(result.verified_features) == {
        "metadata", "bookmarks", "page_labels", "links", "annotations", "forms", "attachments"
    }


@pytest.mark.integration
def test_p6_4_t02_signature_preflight_forces_whole_source_passthrough(tmp_path: Path) -> None:
    """P6.4-T02：签名预检强制整文透传，发布结果与源字节完全一致。"""

    source = create_feature_pdf(tmp_path / "signed.pdf", signature=True)
    preflight = preflight_document(source, support_matrix_path=SUPPORT_MATRIX)
    target_root = tmp_path / "published"
    evidence = publish_source_passthrough(source, target_root / "signed.pdf", target_root)
    assert preflight.decision is PreflightDecision.PASSTHROUGH
    assert "UNSAFE_DIGITAL_SIGNATURES" in preflight.reason_codes
    assert evidence.source_hash == evidence.target_hash == sha256_file(source)


@pytest.mark.integration
def test_p6_4_t03_encrypted_readable_unreadable_and_unknown_critical_are_explicit(
    tmp_path: Path,
) -> None:
    """P6.4-T03：可认证加密透传，不可认证失败，未知关键特征透传。"""

    password = "p6-test-password"
    encrypted = create_encrypted_pdf(tmp_path / "encrypted.pdf", password)
    readable = preflight_document(
        encrypted,
        support_matrix_path=SUPPORT_MATRIX,
        password=password,
    )
    unreadable = preflight_document(encrypted, support_matrix_path=SUPPORT_MATRIX)
    encrypted_output_root = tmp_path / "encrypted-output"
    encrypted_evidence = publish_source_passthrough(
        encrypted,
        encrypted_output_root / "encrypted.pdf",
        encrypted_output_root,
    )
    unknown = create_kernel_pdf(tmp_path / "unknown.pdf", pages=1)
    with pymupdf.open(unknown) as document:
        document.xref_set_key(document.pdf_catalog(), "TransflowCritical", "true")
        document.saveIncr()
    unknown_result = preflight_document(unknown, support_matrix_path=SUPPORT_MATRIX)
    assert readable.decision is PreflightDecision.PASSTHROUGH
    assert unreadable.decision is PreflightDecision.PROCESS_FAILED
    assert encrypted_evidence.source_hash == encrypted_evidence.target_hash
    assert unknown_result.decision is PreflightDecision.PASSTHROUGH


@pytest.mark.integration
def test_p6_4_t04_bookmark_link_order_rotation_and_crop_mutations_are_detected(
    tmp_path: Path,
) -> None:
    """P6.4-T04：目标书签、链接、页序、旋转和 CropBox 变更均被检测。"""

    source = create_feature_pdf(tmp_path / "source.pdf")
    target = tmp_path / "mutated.pdf"
    with pymupdf.open(source) as document:
        document.set_toc([[1, "Changed", 1]])
        page = document[0]
        for link in page.get_links():
            page.delete_link(link)
        page.set_rotation(90)
        page.set_cropbox(pymupdf.Rect(10, 10, 400, 580))
        document.select([1, 0])
        document.save(target)
    matrix = load_support_matrix(SUPPORT_MATRIX)
    result = validate_preservation(
        capture_document_structure(source, support_matrix=matrix),
        capture_document_structure(target, support_matrix=matrix),
        frozenset(),
        matrix,
    )
    assert not result.passed
    assert "PAGE_ORDER_CHANGED" in result.failure_codes
    assert "PAGE_GEOMETRY_CHANGED" in result.failure_codes
    assert "FEATURE_BOOKMARKS_CHANGED" in result.failure_codes
    assert "FEATURE_LINKS_CHANGED" in result.failure_codes


@pytest.mark.integration
def test_p6_4_t05_validation_failure_falls_back_and_unpublishable_source_fails(
    tmp_path: Path,
) -> None:
    """P6.4-T05：校验失败可回退源副本；源本身不可发布时稳定失败。"""

    source = create_feature_pdf(tmp_path / "source.pdf")
    bad_target = tmp_path / "bad.pdf"
    shutil.copyfile(source, bad_target)
    with pymupdf.open(bad_target) as document:
        document.set_metadata({"title": "changed"})
        document.saveIncr()
    matrix = load_support_matrix(SUPPORT_MATRIX)
    failed = validate_preservation(
        capture_document_structure(source, support_matrix=matrix),
        capture_document_structure(bad_target, support_matrix=matrix),
        frozenset(),
        matrix,
    )
    assert not failed.passed
    output_root = tmp_path / "fallback"
    evidence = publish_source_passthrough(source, output_root / "final.pdf", output_root)
    assert evidence.source_hash == evidence.target_hash
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(PortCallError) as error:
        publish_source_passthrough(corrupt, output_root / "corrupt.pdf", output_root)
    assert error.value.code is ErrorCode.SOURCE_NOT_READABLE


@pytest.mark.contract
def test_p6_4_t06_support_matrix_never_promises_unverified_features(tmp_path: Path) -> None:
    """P6.4-T06：每项能力都有 detector、validator、fixture 和明确处置。"""

    matrix = load_support_matrix(SUPPORT_MATRIX)
    assert {item.name for item in matrix.features} == {
        "metadata", "bookmarks", "page_labels", "links", "annotations", "forms",
        "attachments", "digital_signatures", "encryption", "structured_tags",
        "unknown_critical",
    }
    assert all(item.detector and item.validator and item.fixture_id for item in matrix.features)
    payload = json.loads(SUPPORT_MATRIX.read_text(encoding="utf-8"))
    payload["features"].append(
        {
            "name": "unproven_feature",
            "disposition": "VERIFY",
            "detector": "unimplemented_detector",
            "validator": "unimplemented_validator",
            "fixture_id": "missing-fixture",
        }
    )
    expanded = tmp_path / "expanded-support.json"
    expanded.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DomainContractError):
        load_support_matrix(expanded)


@pytest.mark.integration
def test_p6_5_t01_multi_process_same_name_runs_have_no_facts_crosstalk(tmp_path: Path) -> None:
    """P6.5-T01：多进程处理不同目录同名 PDF 时，事实身份和内容不会串 run。"""

    first = create_kernel_pdf(tmp_path / "run-a" / "same.pdf", pages=1)
    second = create_kernel_pdf(tmp_path / "run-b" / "same.pdf", pages=2)
    hashes = (sha256_file(first), sha256_file(second))
    with ProcessPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                extract_page_contract_bytes,
                (str(first), str(second)),
                hashes,
                (1, 1),
            )
        )
    payloads = tuple(json.loads(item) for item in results)
    assert payloads[0]["page"]["source_hash"] == hashes[0]
    assert payloads[1]["page"]["source_hash"] == hashes[1]
    assert payloads[0]["page_identity"] != payloads[1]["page_identity"]


@pytest.mark.contract
def test_p6_5_t02_approved_roles_share_frozen_font_and_kernel_fingerprint() -> None:
    """P6.5-T02：当前批准的开发/目标角色使用同一字体和统一内核指纹。"""

    local = build_kernel_fingerprint(FONT_MANIFEST, SUPPORT_MATRIX)
    determinism = json.loads(DETERMINISM_MANIFEST.read_text(encoding="utf-8"))
    runtime_baseline = json.loads(
        (REPO_ROOT / determinism["runtime_baseline"]).read_text(encoding="utf-8")
    )
    code = """
import sys
from pathlib import Path
from transflow.pdf_kernel import build_kernel_fingerprint
root = Path(sys.argv[1]).resolve()
font_manifest = root / 'resources/manifests/font_manifest.json'
support_matrix = root / 'resources/manifests/p6_preservation_support.json'
print(build_kernel_fingerprint(font_manifest, support_matrix).fingerprint)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip().splitlines()[-1] == local.fingerprint
    assert determinism["approved_roles"] == ["development", "target"]
    assert set(determinism["approved_roles"]) <= set(runtime_baseline["environment_roles"])


@pytest.mark.integration
def test_p6_5_t03_rebuilt_worker_replays_same_facts_without_affecting_other_run(
    tmp_path: Path,
) -> None:
    """P6.5-T03：销毁并重建 worker 后同页可重放，另一个 run 的结果不受影响。"""

    source = create_kernel_pdf(tmp_path / "restart.pdf", pages=2)
    source_hash = sha256_file(source)
    before = extract_page_contract_bytes(str(source), source_hash, 1)
    with ProcessPoolExecutor(max_workers=1) as other_worker:
        other_future = other_worker.submit(
            extract_page_contract_bytes,
            str(source),
            source_hash,
            2,
        )
        with ProcessPoolExecutor(max_workers=1) as failed_worker:
            failed_future = failed_worker.submit(terminate_pdf_worker)
            with pytest.raises(BrokenProcessPool):
                failed_future.result()
        other = other_future.result()
    with ProcessPoolExecutor(max_workers=1) as rebuilt_worker:
        after = rebuilt_worker.submit(
            extract_page_contract_bytes, str(source), source_hash, 1
        ).result()
    assert before == after
    assert json.loads(other)["page"]["page_no"] == 2


@pytest.mark.integration
def test_p6_5_t04_g4_static_and_real_fixture_checks_remain_green() -> None:
    """P6.5-T04：复算 G4 架构、真实年报 fixture 与最终化边界。"""

    checks = verify_p4.all_checks()
    assert checks == {name: [] for name in checks}


@pytest.mark.integration
def test_p6_5_t05_g5_anonymous_baseline_identity_and_routes_have_no_drift() -> None:
    """P6.5-T05：复算 G5 匿名基线、迁移和质量冻结证据。"""

    checks = verify_p5.all_checks()
    assert checks == {name: [] for name in checks}


@pytest.mark.contract
def test_p6_5_t06_kernel_fingerprint_mismatch_rejects_old_checkpoint() -> None:
    """P6.5-T06：Kernel/字体/Facts/支持矩阵指纹漂移时旧 Checkpoint 被拒绝。"""

    fingerprint = build_kernel_fingerprint(FONT_MANIFEST, SUPPORT_MATRIX)
    stored = CheckpointCompatibility(HASH_A, HASH_A, HASH_A, HASH_A, fingerprint.fingerprint)
    current = replace(stored, schema_hash="b" * 64)
    with pytest.raises(DomainContractError) as error:
        ensure_checkpoint_compatible(stored, current)
    assert error.value.code is ErrorCode.CHECKPOINT_INCOMPATIBLE


def main() -> int:
    """记录 P6 测试必须通过 pytest 执行并保留真实命令输出。"""

    print("请运行: python -m pytest tests/test_p6.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
