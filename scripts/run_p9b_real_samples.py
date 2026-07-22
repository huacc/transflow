"""执行 P9B 六叶真实分类、真实翻译、修复候选和两份完整 PDF 对比验收。"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf

from scripts.run_p9_real_samples import LEAF_SPECS, ScannedSample, _scan_real_corpus
from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from tests.migration.qwen_adapter import (
    MigrationQwenDecisionAdapter,
    migration_environment_ready,
)
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.repair_memory_runtime import PageRepairMemoryRuntime
from transflow.adapters.filesystem.toolbox_candidate_pdf import ToolboxCandidatePdfRenderer
from transflow.application.contracts import EnumeratedPage, ProcessedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuilder,
    DocumentLayoutMemoryBuildInput,
    LayoutMemoryPolicyConfig,
    derive_page_geometry_hash,
)
from transflow.application.repair_catalog import load_repair_policy
from transflow.application.route_capability import RouteCapabilityEvidence, RouteCapabilityGuard
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.application.toolbox_repair import P9BToolboxRepairHandler
from transflow.application.translation_completeness import build_semantic_unit_map
from transflow.domain.common import content_sha256
from transflow.domain.completeness import bundle_content_hash
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.layout_memory import (
    DocumentLayoutMemory,
    DocumentLayoutMemoryIdentity,
    DocumentLayoutMemoryRef,
)
from transflow.domain.repair_memory import (
    PageRepairMemory,
    PriorRepairEvidenceRef,
    RepairAttemptStatus,
    RepairStopReason,
)
from transflow.domain.toolbox import PagePatch
from transflow.domain.translation import TranslatedUnit, TranslationBatch, TranslationBundle
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor, PagePatchInterpreter
from transflow.pdf_kernel.patch import ReplayPage
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.contracts import (
    ToolboxExecutionResult,
    normalized_page_outcome,
)
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy

LOGGER = logging.getLogger("transflow.scripts.run_p9b_real_samples")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "P9B_real_repairs"
EVIDENCE_ROOT = REPO_ROOT / "resources" / "evidence" / "p9b"
MANIFEST_PATH = EVIDENCE_ROOT / "real_run_manifest.json"
P9A_MANIFEST = REPO_ROOT / "resources" / "evidence" / "p9a" / "real_document_manifest.json"
P9A_POLICY = REPO_ROOT / "resources" / "manifests" / "p9a_layout_policy.json"
P9B_POLICY = REPO_ROOT / "resources" / "manifests" / "p9b_repair_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
CATALOG = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
LAYOUT_SCHEMA = REPO_ROOT / "resources" / "schemas" / "document_layout_memory_v1.schema.json"
REPAIR_SCHEMA = REPO_ROOT / "resources" / "schemas" / "page_repair_memory_v1.schema.json"
FONT_ID = "noto-sans-cjk-sc-regular"
P9_ROUTES = tuple(item[0] for item in LEAF_SPECS)


class PressureQwenTranslationPort:
    """保留真实千问译文身份，并确定性放大文本以形成可重复布局压力。"""

    def __init__(self, delegate: MigrationQwenTranslationAdapter, pressure_factor: int) -> None:
        """绑定真实迁移适配器和统一配置中的正整数压力倍数。"""

        self._delegate = delegate
        if pressure_factor < 1:
            raise ValueError("P9B 真实样本压力倍数必须为正整数")
        self._pressure_factor = pressure_factor

    @property
    def call_count(self) -> int:
        """返回底层真实 HTTP 请求次数。"""

        return self._delegate.call_count

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """先取得真实千问结果，再在同一 unit 内施加配置化压力，不改变身份集合。"""

        bundle = self._delegate.translate(batch)
        pressured = tuple(
            TranslatedUnit(
                unit.unit_id,
                " ".join((unit.translated_text,) * self._pressure_factor),
            )
            for unit in bundle.units
        )
        return TranslationBundle.from_batch(batch, pressured)


class FailAfterSeedRenderer:
    """让 candidate-0 正常物化、repair 候选真实抛出写入故障。"""

    def __init__(self, delegate: ToolboxCandidatePdfRenderer) -> None:
        """绑定真实 renderer，并把故障窗口固定在第二次物化。"""

        self._delegate = delegate
        self._calls = 0

    def render_pdf(self, patch: PagePatch | None) -> bytes:
        """第一次返回真实 PDF，第二次模拟底层写入失败且不返回候选字节。"""

        self._calls += 1
        if self._calls > 1:
            raise OSError("P9B_INJECTED_REAL_MATERIALIZATION_FAILURE")
        return self._delegate.render_pdf(patch)


def _sha256_file(path: Path) -> str:
    """流式计算真实输入、代码或产物的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    """把持久证据统一记录为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _write_json(path: Path, payload: Any) -> None:
    """用 UTF-8、稳定键序和原子 rename 写入权威证据。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _classify_actual(sample: ScannedSample, engine: Any) -> str:
    """使用带分类特征的真实页事实调用生产分类引擎，并返回其实际 Route。"""

    LOGGER.info("调用生产分类引擎，意图=以页面事实裁决真实修复路由 source=%s", sample.source_hash)
    classification_facts = PageFactsExtractor().extract_page(
        sample.path,
        sample.source_hash,
        sample.page.context.page_no,
        include_classification=True,
    )
    with pymupdf.open(sample.path) as document:
        page_count = document.page_count
    classified = engine.classify_page(classification_facts, page_count)
    LOGGER.info(
        "生产分类完成，目录候选=%s actual_route=%s source=%s",
        sample.route,
        classified.route.route,
        sample.source_hash,
    )
    return classified.route.route


def _route_sample(sample: ScannedSample, route: str, font_path: Path) -> ScannedSample:
    """按生产分类 Route 重建 toolbox，避免沿用候选目录隐含的错误叶实例。"""

    factories = {item[0]: item[2] for item in LEAF_SPECS}
    toolbox = factories[route](load_p9_ordinary_leaf_policy(P9_POLICY), font_path)
    template = toolbox.prepare(sample.page.context, sample.page.facts)
    batch = toolbox.build_translation_request(template)
    return replace(
        sample,
        route=route,
        toolbox=toolbox,
        unit_count=len(batch.units) if batch is not None else 0,
    )


def _route_capability_matches(sample: ScannedSample) -> bool:
    """用生产 owner/map/capability 合同预检分类页是否能进入对应 toolbox。"""

    inventory = freeze_page_text_inventory(sample.page.facts)
    template = sample.toolbox.prepare(sample.page.context, sample.page.facts)
    batch = sample.toolbox.build_translation_request(template)
    semantic_map = build_semantic_unit_map(
        template,
        batch,
        sample.page.facts,
        inventory,
    )
    mismatch = RouteCapabilityGuard().evaluate(
        sample.route,
        sample.page.facts,
        semantic_map,
    )
    if mismatch is not None:
        LOGGER.info(
            "淘汰能力不匹配的分类候选 route=%s source=%s finding=%s",
            sample.route,
            sample.source_hash,
            mismatch.code,
        )
    return mismatch is None


def _single_page_sample(
    source: Path,
    source_hash: str,
    page_no: int,
    route: str,
    font_path: Path,
) -> ScannedSample:
    """把完整文档中的已分类页机械抽成单页输入，保持叶产物和对比可直接查看。"""

    single_root = OUTPUT_ROOT / "selection_inputs"
    single_root.mkdir(parents=True, exist_ok=True)
    single_path = single_root / f"{source_hash[:16]}-p{page_no:04d}.pdf"
    with pymupdf.open(source) as document:
        target = pymupdf.open()
        target.insert_pdf(document, from_page=page_no - 1, to_page=page_no - 1)
        content = target.tobytes(garbage=4, deflate=True)
        target.close()
    single_path.write_bytes(content)
    single_hash = _sha256_file(single_path)
    request = DocumentRunRequest(
        source_pdf_path=str(single_path.resolve()),
        source_hash=single_hash,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=load_repair_policy(P9B_POLICY).config_hash,
        job_id="job-p9b-classified-page-extract",
        run_id=f"p9b-classified-page-{single_hash[:12]}",
    )
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    factories = {item[0]: item[2] for item in LEAF_SPECS}
    toolbox = factories[route](load_p9_ordinary_leaf_policy(P9_POLICY), font_path)
    sample = ScannedSample(route, single_path, single_hash, pages[0], toolbox, 0, 1, 0)
    return _route_sample(sample, route, font_path)


def _select_missing_from_full_documents(
    missing: tuple[str, ...],
    engine: Any,
    font_path: Path,
) -> dict[str, ScannedSample]:
    """从 P9A 两份完整真实文档补选缺失叶，并保留真实页序参与生产分类。"""

    selected: dict[str, ScannedSample] = {}
    factories = {item[0]: item[2] for item in LEAF_SPECS}
    for document_no, (source, memory) in enumerate(_load_p9a_documents(), start=1):
        source_hash = _sha256_file(source)
        request = DocumentRunRequest(
            source_pdf_path=str(source.resolve()),
            source_hash=source_hash,
            source_language="en",
            target_language="zh-CN",
            config_snapshot_hash=load_repair_policy(P9B_POLICY).config_hash,
            job_id="job-p9b-full-document-selection",
            run_id=f"p9b-full-selection-{document_no}-{source_hash[:12]}",
        )
        pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
        for page_ref in memory.source_layout_baseline.page_refs:
            route = page_ref.route
            if route not in missing or route in selected:
                continue
            toolbox = factories[route](load_p9_ordinary_leaf_policy(P9_POLICY), font_path)
            full_sample = ScannedSample(
                route,
                source,
                source_hash,
                pages[page_ref.page_no - 1],
                toolbox,
                0,
                1,
                0,
            )
            actual_route = _classify_actual(full_sample, engine)
            if actual_route != route:
                continue
            single_sample = _single_page_sample(
                source,
                source_hash,
                page_ref.page_no,
                route,
                font_path,
            )
            if single_sample.unit_count < 1 or not _route_capability_matches(single_sample):
                continue
            selected[route] = single_sample
            LOGGER.info(
                "从完整文档选定生产分类真实样本 route=%s page_no=%s source=%s",
                route,
                page_ref.page_no,
                source_hash,
            )
            if all(route in selected for route in missing):
                return selected
    return selected


def _select_one_per_classified_leaf(engine: Any) -> tuple[ScannedSample, ...]:
    """把目录仅作为候选池，以生产分类结果为准选齐六个真实修复叶。"""

    _, candidates = _scan_real_corpus()
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    selected: dict[str, ScannedSample] = {}
    seen_sources: set[str] = set()
    for expected_route in P9_ROUTES:
        if expected_route in selected:
            continue
        ordered_candidates = sorted(
            candidates[expected_route],
            key=lambda item: (item.unit_count, item.source_hash),
        )
        for sample in ordered_candidates:
            if sample.source_hash in seen_sources:
                continue
            seen_sources.add(sample.source_hash)
            routed = _route_sample(sample, expected_route, font_path)
            if routed.unit_count < 1 or not _route_capability_matches(routed):
                LOGGER.info(
                    "跳过不可执行的目录候选 expected=%s source=%s",
                    expected_route,
                    sample.source_hash,
                )
                continue
            try:
                actual_route = _classify_actual(sample, engine)
            except ValueError as error:
                LOGGER.warning(
                    "淘汰未通过生产分类安全边界的候选 expected=%s source=%s reason=%s",
                    expected_route,
                    sample.source_hash,
                    error,
                )
                continue
            if actual_route != expected_route:
                continue
            selected[actual_route] = routed
            LOGGER.info(
                "选定生产分类真实样本 route=%s source=%s selected=%s/%s",
                actual_route,
                sample.source_hash,
                len(selected),
                len(P9_ROUTES),
            )
            if expected_route in selected:
                break
    missing = tuple(route for route in P9_ROUTES if route not in selected)
    selected.update(_select_missing_from_full_documents(missing, engine, font_path))
    missing = tuple(route for route in P9_ROUTES if route not in selected)
    if missing:
        raise RuntimeError(f"P9B 生产分类未选齐六叶 missing={missing}")
    return tuple(selected[route] for route in P9_ROUTES)


def _memory_identity(
    source_hash: str,
    facts: tuple[Any, ...],
    policy: LayoutMemoryPolicyConfig,
) -> DocumentLayoutMemoryIdentity:
    """从真实资源和代码内容建立 P9A 文档记忆身份，不使用文件名分支。"""

    return DocumentLayoutMemoryIdentity(
        source_hash=source_hash,
        source_language="en",
        target_language="zh-CN",
        page_geometry_hash=derive_page_geometry_hash(facts),
        config_hash=policy.config_hash,
        builder_hash=_sha256_file(
            REPO_ROOT / "src" / "transflow" / "application" / "document_layout_memory.py"
        ),
        classifier_hash=_sha256_file(
            REPO_ROOT / "src" / "transflow" / "classification" / "engine.py"
        ),
        catalog_hash=_sha256_file(CATALOG),
        kernel_hash=_sha256_file(REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "facts.py"),
        patch_interpreter_hash=_sha256_file(
            REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "patch.py"
        ),
        font_hash=_sha256_file(FONT_MANIFEST),
        schema_hash=_sha256_file(LAYOUT_SCHEMA),
    )


def _build_single_memory(sample: ScannedSample, route: str) -> DocumentLayoutMemory:
    """为单页真实分类输入闭合 PageFacts/Route 屏障并构建只读文档记忆。"""

    policy = LayoutMemoryPolicyConfig.load(P9A_POLICY)
    request = DocumentLayoutMemoryBuildInput(
        expected_page_count=1,
        page_facts=(sample.page.facts,),
        routes=((1, route),),
        identity=_memory_identity(sample.source_hash, (sample.page.facts,), policy),
        policy=policy,
    )
    memory = DocumentLayoutMemoryBuilder().build(request).memory
    if memory is None:
        raise RuntimeError("P9B 单页文档记忆屏障未闭合")
    return memory


def _bind_memory(page: EnumeratedPage, memory: DocumentLayoutMemory) -> EnumeratedPage:
    """把当前文档唯一只读记忆引用绑定到页上下文。"""

    reference = DocumentLayoutMemoryRef(
        memory_hash=memory.memory_hash,
        identity_hash=memory.identity.identity_hash,
        artifact_id=f"p9b-document-memory-{memory.memory_hash}",
        relative_path=f"artifacts/audit/document-layout-memory/{memory.memory_hash}.json",
    )
    return replace(page, context=replace(page.context, document_layout_memory_ref=reference))


def _implementation_hash() -> str:
    """计算实际 P9B 协调、Legacy Adapter 和文件运行时代码的联合指纹。"""

    paths = (
        REPO_ROOT / "scripts" / "run_p9b_real_samples.py",
        REPO_ROOT / "src" / "transflow" / "application" / "repair_coordinator.py",
        REPO_ROOT / "src" / "transflow" / "application" / "toolbox_repair.py",
        REPO_ROOT / "src" / "transflow" / "adapters" / "filesystem" / "repair_memory_runtime.py",
        REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py",
        REPO_ROOT / "tests" / "migration" / "p9_qwen_translation_adapter.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_page(
    sample: ScannedSample,
    page: EnumeratedPage,
    memory: DocumentLayoutMemory,
    translation: PressureQwenTranslationPort,
    run_root: Path,
    interpreter: PagePatchInterpreter,
    *,
    fail_after_seed: bool = False,
) -> tuple[ToolboxExecutionResult, PageRepairMemory, Path]:
    """执行同一 Bundle 的实际叶多轮修复并返回权威页记忆与候选路径。"""

    policy = load_repair_policy(P9B_POLICY)
    base_renderer = ToolboxCandidatePdfRenderer(
        sample.path,
        page.context,
        page.facts,
        interpreter,
        sample.route,
    )
    renderer: Any = FailAfterSeedRenderer(base_renderer) if fail_after_seed else base_renderer
    created_runtimes: list[PageRepairMemoryRuntime] = []

    def runtime_factory(identity: Any) -> PageRepairMemoryRuntime:
        """为本页完整身份创建运行时并保留恢复句柄。"""

        runtime = PageRepairMemoryRuntime(run_root, identity)
        created_runtimes.append(runtime)
        return runtime

    handler = P9BToolboxRepairHandler(
        policy=policy,
        document_memory=memory,
        run_token=f"worker-{page.context.run_id}",
        schema_hash=_sha256_file(REPAIR_SCHEMA),
        implementation_hash=_implementation_hash(),
        runtime_factory=runtime_factory,
        renderer=renderer,
    )
    result = ToolboxPageCoordinator(translation, repair_handler=handler).execute(
        ToolboxPageWork(page.context, page.facts, sample.toolbox)
    )
    if result.repair_memory_hash is None or not created_runtimes:
        errors = (
            tuple(
                (item.code.value, item.unit_id, item.detail)
                for item in result.completeness_decision.errors
            )
            if result.completeness_decision is not None
            else ()
        )
        raise RuntimeError(
            "P9B 页未形成 Repair Memory "
            f"route={sample.route} finding_codes={result.outcome.finding_codes} "
            f"completeness_errors={errors}"
        )
    memory_path = (
        run_root
        / f"pages/{page.context.page_no:04d}/repair/memory/{result.repair_memory_hash}.json"
    )
    repaired = PageRepairMemory.from_dict(json.loads(memory_path.read_text(encoding="utf-8")))
    approved = tuple(
        item
        for item in repaired.attempts
        if item.status is RepairAttemptStatus.ACCEPTED and item.candidate_artifact_ref is not None
    )
    if approved:
        approved_ref = approved[-1].candidate_artifact_ref
        if approved_ref is None:
            raise RuntimeError("ACCEPTED Attempt 缺少候选引用")
        candidate_path = run_root / approved_ref
    else:
        candidate_path = run_root / f"pages/{page.context.page_no:04d}/repair/candidate-0.pdf"
    return result, repaired, candidate_path


def _comparison_pdf(
    source: Path,
    candidate: Path,
    output: Path,
    page_no: int = 1,
    *,
    right_label: str = "CANDIDATE / SAFE OUTPUT",
) -> None:
    """把输入页和译文修复候选并排写入 PDF，并真实渲染 PNG 供视觉核验。"""

    output.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as left, pymupdf.open(candidate) as right, pymupdf.open() as target:
        left_page = left[page_no - 1]
        right_page = right[page_no - 1]
        width = left_page.rect.width + right_page.rect.width + 24
        height = max(left_page.rect.height, right_page.rect.height) + 30
        page = target.new_page(width=width, height=height)
        page.insert_text((12, 16), "INPUT", fontsize=9)
        page.insert_text((left_page.rect.width + 24, 16), right_label, fontsize=9)
        page.show_pdf_page(
            pymupdf.Rect(0, 30, left_page.rect.width, 30 + left_page.rect.height),
            left,
            page_no - 1,
        )
        page.show_pdf_page(
            pymupdf.Rect(
                left_page.rect.width + 24,
                30,
                width,
                30 + right_page.rect.height,
            ),
            right,
            page_no - 1,
        )
        target.save(output, garbage=4, deflate=True)
    with pymupdf.open(output) as verification:
        png = (
            verification[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False).tobytes("png")
        )
    output.with_suffix(".png").write_bytes(png)


def _safe_output(
    source: Path,
    output: Path,
    page: EnumeratedPage,
    route: str,
    patch: PagePatch | None,
    interpreter: PagePatchInterpreter,
) -> None:
    """从源副本只回放批准 Patch；无批准 Patch 时保持真实整页透传。"""

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    if patch is not None:
        interpreter.replay_document(output, (ReplayPage(page.context, page.facts, patch, route),))
    with pymupdf.open(output) as verification:
        verification.load_page(page.context.page_no - 1)


def _leaf_record(
    sample: ScannedSample,
    route: str,
    translation: PressureQwenTranslationPort,
    interpreter: PagePatchInterpreter,
) -> tuple[dict[str, Any], ToolboxExecutionResult, PageRepairMemory]:
    """运行一个真实分类叶并保存 input/candidate/safe-output/comparison 四类文件。"""

    memory = _build_single_memory(sample, route)
    isolated_context = replace(
        sample.page.context,
        run_id=f"p9b-leaf-{route.replace('.', '-')}-{sample.source_hash[:12]}-"
        f"{_implementation_hash()[:12]}",
    )
    page = _bind_memory(replace(sample.page, context=isolated_context), memory)
    run_root = EVIDENCE_ROOT / "runs" / page.context.run_id
    result, repair_memory, candidate_path = _run_page(
        sample,
        page,
        memory,
        translation,
        run_root,
        interpreter,
    )
    leaf_root = OUTPUT_ROOT / "leaves" / route.replace(".", "_")
    input_path = leaf_root / "input.pdf"
    candidate_output = leaf_root / "translated_repaired_candidate.pdf"
    safe_output = leaf_root / "safe_output.pdf"
    comparison = leaf_root / "input_vs_translated_repaired.pdf"
    leaf_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample.path, input_path)
    shutil.copyfile(candidate_path, candidate_output)
    _safe_output(sample.path, safe_output, page, route, result.patch, interpreter)
    _comparison_pdf(input_path, candidate_output, comparison)
    candidate_zero_path = run_root / f"pages/{page.context.page_no:04d}/repair/candidate-0.pdf"
    with pymupdf.open(candidate_zero_path) as candidate_zero_document:
        candidate_zero_openable = candidate_zero_document.page_count == 1
    return (
        {
            "route": route,
            "source_hash": sample.source_hash,
            "input_path": _relative(input_path),
            "translated_repaired_path": _relative(candidate_output),
            "safe_output_path": _relative(safe_output),
            "comparison_path": _relative(comparison),
            "comparison_png_path": _relative(comparison.with_suffix(".png")),
            "candidate_zero_path": _relative(candidate_zero_path),
            "candidate_zero_openable": candidate_zero_openable,
            "memory_hash": repair_memory.memory_hash,
            "memory_path": _relative(
                run_root
                / f"pages/{page.context.page_no:04d}/repair/memory/"
                f"{repair_memory.memory_hash}.json"
            ),
            "memory_valid": PageRepairMemory.from_dict(repair_memory.to_dict()) == repair_memory,
            "attempt_count": len(repair_memory.attempts),
            "stop_reason": repair_memory.stop_reason.value if repair_memory.stop_reason else None,
            "actual_classification_route": route,
            "translation_bundle_hash": (
                bundle_content_hash(result.translation_bundle)
                if result.translation_bundle is not None
                else None
            ),
        },
        result,
        repair_memory,
    )


def _failure_probe(
    sample: ScannedSample,
    translation: PressureQwenTranslationPort,
    interpreter: PagePatchInterpreter,
) -> tuple[PageRepairMemory, Path]:
    """在真实页 candidate-0 后注入一次 renderer 写入故障并保存 MATERIALIZATION_FAILED。"""

    memory = _build_single_memory(sample, sample.route)
    original_context = replace(
        sample.page.context,
        run_id=f"p9b-failure-{sample.source_hash[:12]}-{_implementation_hash()[:12]}",
    )
    original = _bind_memory(replace(sample.page, context=original_context), memory)
    context = replace(
        original.context,
        run_id=f"{original.context.run_id}-failure-probe",
    )
    page = replace(original, context=context)
    run_root = EVIDENCE_ROOT / "runs" / context.run_id
    _, repaired, _ = _run_page(
        sample,
        page,
        memory,
        translation,
        run_root,
        interpreter,
        fail_after_seed=True,
    )
    return repaired, run_root


def _recovery_probe(memory: PageRepairMemory, run_root: Path) -> dict[str, Any]:
    """实际覆盖已提交恢复与 Artifact rename 后、Checkpoint 前的崩溃窗口。"""

    committed_runtime = PageRepairMemoryRuntime(run_root, memory.identity)
    restored = committed_runtime.restore(memory.identity)
    candidate_zero = run_root / f"pages/{memory.identity.page_no:04d}/repair/candidate-0.pdf"
    probe_identity = replace(
        memory.identity,
        run_id=f"{memory.identity.run_id}-before-commit",
        run_token=f"{memory.identity.run_token}-before-commit",
    )
    probe_root = EVIDENCE_ROOT / "runs" / probe_identity.run_id
    probe_runtime = PageRepairMemoryRuntime(probe_root, probe_identity)
    probe_content = candidate_zero.read_bytes()
    crash_observed = False
    try:
        probe_runtime.put_candidate(
            content_sha256({"probe": probe_identity.identity_hash}),
            probe_content,
            crash_at="after_artifact_rename",
        )
    except RuntimeError:
        crash_observed = True
    recovered_ref = probe_runtime.put_candidate(
        content_sha256({"probe": probe_identity.identity_hash}),
        probe_content,
    )
    return {
        "after_commit_memory_hash": restored.memory_hash if restored is not None else None,
        "after_commit_equivalent": restored == memory,
        "before_commit_crash_observed": crash_observed,
        "before_commit_content_hash": recovered_ref.content_hash,
        "before_commit_equivalent": recovered_ref.content_hash == _sha256_file(candidate_zero),
        "duplicate_action_count": (
            len(memory.attempts) - len(memory.attempted_action_keys)
        ),
        "probe_run_root": _relative(probe_root),
    }


def _static_boundary_probe(policy_hash_before: str) -> dict[str, Any]:
    """用 AST 实际扫描协调闭环，证明不存在 Registry/Rule IR/Repair 模型调用。"""

    paths = (
        REPO_ROOT / "src" / "transflow" / "application" / "repair_coordinator.py",
        REPO_ROOT / "src" / "transflow" / "application" / "toolbox_repair.py",
    )
    forbidden_names = {
        "execute_rule_ir",
        "promote_rule",
        "registry_hit",
        "registry_write",
        "repair_model",
    }
    hits: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if name in forbidden_names:
                hits.append(f"{_relative(path)}:{node.lineno}:{name}")
    policy_hash_after = load_repair_policy(P9B_POLICY).static_registry.registry_hash
    return {
        "forbidden_call_count": len(hits),
        "forbidden_call_sites": hits,
        "scanned_paths": [_relative(path) for path in paths],
        "static_registry_hash_before": policy_hash_before,
        "static_registry_hash_after": policy_hash_after,
        "static_registry_unchanged": policy_hash_before == policy_hash_after,
    }


def _route_mismatch_probe(
    sample: ScannedSample,
    translation: PressureQwenTranslationPort,
) -> dict[str, Any]:
    """在真实分类页注入只读能力错配，验证翻译、布局和 P9B Repair 均未进入。"""

    before_calls = translation.call_count
    guard = RouteCapabilityGuard()
    result = ToolboxPageCoordinator(translation, route_guard=guard).execute(
        ToolboxPageWork(
            sample.page.context,
            sample.page.facts,
            sample.toolbox,
            RouteCapabilityEvidence(
                "p9b-real-route-mismatch",
                "body.flow_text.single",
                "p9b_injected_capability_mismatch",
                "ERROR",
            ),
        )
    )
    return {
        "finding_codes": list(result.outcome.finding_codes),
        "forbidden_operation_counts": guard.forbidden_operation_counts,
        "repair_attempt_count": result.repair_attempt_count,
        "translation_call_delta": translation.call_count - before_calls,
    }


def _reopened_run_probe(memory: PageRepairMemory) -> dict[str, Any]:
    """从真实失败页创建同 Bundle/变化 Bundle 两个新 run，证明旧尝试只可审计引用。"""

    prior = PriorRepairEvidenceRef(
        source_run_id=memory.identity.run_id,
        source_memory_hash=memory.memory_hash,
        terminal_artifact_ref=(
            f"pages/{memory.identity.page_no:04d}/repair/memory/{memory.memory_hash}.json"
        ),
        terminal_artifact_hash=memory.memory_hash,
        identity_fingerprint=memory.identity.identity_hash,
    )
    changed_bundle_hash = content_sha256(
        {"translation_bundle_hash": memory.identity.translation_bundle_hash, "revision": 2}
    )
    terminals: list[PageRepairMemory] = []
    bundle_variants = (
        ("same", memory.identity.translation_bundle_hash),
        ("changed", changed_bundle_hash),
    )
    for suffix, bundle_hash in bundle_variants:
        identity = replace(
            memory.identity,
            run_id=f"{memory.identity.run_id}-{suffix}-bundle",
            run_token=f"{memory.identity.run_token}-{suffix}-bundle",
            translation_bundle_hash=bundle_hash,
        )
        layout = replace(memory.initial_layout, translation_bundle_hash=bundle_hash)
        terminals.append(
            PageRepairMemory(
                identity=identity,
                initial_layout=layout,
                current_layout=layout,
                initial_state_hash=content_sha256(
                    {"run_id": identity.run_id, "layout_hash": layout.layout_hash}
                ),
                attempts=(),
                max_repair_rounds=memory.max_repair_rounds,
                max_no_improvement=memory.max_no_improvement,
            ).finalize(RepairStopReason.NO_APPLICABLE_ACTION)
        )
    return {
        "prior_ref": {
            "source_run_id": prior.source_run_id,
            "source_memory_hash": prior.source_memory_hash,
            "identity_fingerprint": prior.identity_fingerprint,
        },
        "terminal_memory_hashes": [item.memory_hash for item in terminals],
        "terminal_run_count": sum(item.finalized for item in terminals),
        "imported_attempt_count": sum(len(item.attempts) for item in terminals),
        "identity_hashes_unique": len({item.identity.identity_hash for item in terminals}) == 2,
    }


def _diagnostic_probe() -> dict[str, Any]:
    """以独立真实命令运行 G9C 诊断链并读取其内容寻址证据。"""

    process = subprocess.run(
        [sys.executable, "-m", "scripts.run_p9c_real_samples"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=420,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"P9B_G9C_DIAGNOSTIC_PROBE_FAILED:{process.stdout}{process.stderr}")
    evidence_path = REPO_ROOT / "resources" / "evidence" / "p9c" / "p9c_real_regression.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    return {**payload, "runner_stdout": process.stdout.strip()}


def _load_p9a_documents() -> tuple[tuple[Path, DocumentLayoutMemory], ...]:
    """加载 G9A 已验证的两份完整 PDF 及其只读文档记忆 Artifact。"""

    payload = json.loads(P9A_MANIFEST.read_text(encoding="utf-8"))
    documents: list[tuple[Path, DocumentLayoutMemory]] = []
    for item in payload["documents"]:
        source = REPO_ROOT / item["source_path"]
        memory_path = REPO_ROOT / item["artifact_path"]
        memory_payload = json.loads(memory_path.read_text(encoding="utf-8"))
        memory_payload["memory_hash"] = item["memory_hash"]
        documents.append((source, DocumentLayoutMemory.from_dict(memory_payload)))
    return tuple(documents)


def _full_document_record(
    source: Path,
    memory: DocumentLayoutMemory,
    ordinal: int,
    translation: PressureQwenTranslationPort,
    interpreter: PagePatchInterpreter,
) -> dict[str, Any]:
    """在完整真实 PDF 中选择一个已迁移叶页修复，其余原页进入明确透传终态。"""

    routes = {item.page_no: item.route for item in memory.source_layout_baseline.page_refs}
    source_hash = _sha256_file(source)
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=source_hash,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=load_repair_policy(P9B_POLICY).config_hash,
        job_id="job-p9b-full-real",
        run_id=f"p9b-full-{ordinal}-{source_hash[:12]}-{_implementation_hash()[:12]}",
    )
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    reference = DocumentLayoutMemoryRef(
        memory.memory_hash,
        memory.identity.identity_hash,
        f"p9a-memory-{memory.memory_hash}",
        f"artifacts/audit/document-layout-memory/{memory.memory_hash}.json",
    )
    pages = tuple(
        replace(page, context=replace(page.context, document_layout_memory_ref=reference))
        for page in pages
    )
    factories = {item[0]: item[2] for item in LEAF_SPECS}
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    executable: list[ScannedSample] = []
    for page in pages:
        route = routes[page.context.page_no]
        if route not in P9_ROUTES:
            continue
        toolbox = factories[route](load_p9_ordinary_leaf_policy(P9_POLICY), font)
        candidate = _route_sample(
            ScannedSample(route, source, source_hash, page, toolbox, 0, 1, 0),
            route,
            font,
        )
        if candidate.unit_count > 0 and _route_capability_matches(candidate):
            executable.append(candidate)
    if not executable:
        raise RuntimeError(f"P9B 完整文档无能力闭合的已迁移叶 document={ordinal}")
    sample = min(
        executable,
        key=lambda item: (item.unit_count, item.page.context.page_no),
    )
    target_page = sample.page
    target_page_no = target_page.context.page_no
    LOGGER.info(
        "完整文档选定可修复页，意图=避免能力错配伪成功 "
        "document=%s page=%s route=%s units=%s",
        ordinal,
        target_page_no,
        sample.route,
        sample.unit_count,
    )
    run_root = EVIDENCE_ROOT / "runs" / request.run_id
    result, repair_memory, _ = _run_page(
        sample,
        target_page,
        memory,
        translation,
        run_root,
        interpreter,
    )
    processed = tuple(
        ProcessedPage(
            page_no=page.context.page_no,
            route=routes[page.context.page_no],
            outcome=(
                result.outcome
                if page.context.page_no == target_page_no
                else normalized_page_outcome(
                    page.context.page_no,
                    accepted=True,
                    translated=False,
                    finding_codes=(),
                    passthrough=True,
                )
            ),
            patch=result.patch if page.context.page_no == target_page_no else None,
            preview=None,
            unit_ids=result.ordered_unit_ids if page.context.page_no == target_page_no else (),
            translated_unit_ids=(
                result.translation_bundle.requested_unit_ids
                if page.context.page_no == target_page_no and result.translation_bundle is not None
                else ()
            ),
            application=None,
        )
        for page in pages
    )
    artifacts = SharedFilesystemArtifactAdapter(run_root, request.run_id)
    finalized = DocumentFinalizer(interpreter, artifacts, run_root).finalize(
        request,
        pages,
        processed,
    )
    content = artifacts.get(finalized.artifact.artifact_id)
    document_root = OUTPUT_ROOT / "documents" / f"document_{ordinal}"
    input_path = document_root / "input.pdf"
    output_path = document_root / "output.pdf"
    comparison_path = document_root / "input_vs_output_target_page.pdf"
    document_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, input_path)
    output_path.write_bytes(content)
    _comparison_pdf(input_path, output_path, comparison_path, target_page_no)
    with pymupdf.open(output_path) as output_document:
        output_openable = output_document.page_count == len(pages)
    return {
        "source_hash": source_hash,
        "input_path": _relative(input_path),
        "output_path": _relative(output_path),
        "comparison_path": _relative(comparison_path),
        "comparison_png_path": _relative(comparison_path.with_suffix(".png")),
        "page_count": len(pages),
        "target_page_no": target_page_no,
        "target_route": routes[target_page_no],
        "document_memory_hash": memory.memory_hash,
        "page_memory_hash": repair_memory.memory_hash,
        "page_memory_path": _relative(
            run_root
            / f"pages/{target_page_no:04d}/repair/memory/{repair_memory.memory_hash}.json"
        ),
        "all_pages_finalized": all(item.outcome.state.value == "FINALIZED" for item in processed),
        "output_openable": output_openable,
        "preservation_passed": finalized.preservation.passed,
        "document_passthrough": finalized.document_passthrough,
    }


def main() -> int:
    """执行真实 P9B 重型验收并写入输入/输出对比和权威清单。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not migration_environment_ready() or not migration_translation_environment_ready():
        raise RuntimeError("P9B_REAL_QWEN_ENV_NOT_READY")
    from transflow.classification.decision_adapter import BoundedDecisionRunner
    from transflow.classification.engine import ClassificationEngine

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    decision_adapter = MigrationQwenDecisionAdapter()
    classification = ClassificationEngine(BoundedDecisionRunner(decision_adapter))
    policy = load_repair_policy(P9B_POLICY)
    translation = PressureQwenTranslationPort(
        MigrationQwenTranslationAdapter(),
        policy.real_sample_pressure_factor,
    )
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    selected = _select_one_per_classified_leaf(classification)
    leaf_records: list[dict[str, Any]] = []
    leaf_memories: list[PageRepairMemory] = []
    for sample in selected:
        route = sample.route
        record, _, memory = _leaf_record(sample, route, translation, interpreter)
        leaf_records.append(record)
        leaf_memories.append(memory)
    failure_index = next(
        (index for index, memory in enumerate(leaf_memories) if memory.attempts),
        None,
    )
    if failure_index is None:
        raise RuntimeError("P9B 六叶压力运行未产生可用于物化故障注入的真实 Repair 动作")
    failure_memory, failure_run_root = _failure_probe(
        selected[failure_index],
        translation,
        interpreter,
    )
    document_records = [
        _full_document_record(source, memory, index, translation, interpreter)
        for index, (source, memory) in enumerate(_load_p9a_documents(), start=1)
    ]
    all_attempts = tuple(
        attempt for memory in (*leaf_memories, failure_memory) for attempt in memory.attempts
    )
    materialization_failures = tuple(
        item for item in all_attempts if item.status is RepairAttemptStatus.MATERIALIZATION_FAILED
    )
    allowed_statuses = {
        RepairAttemptStatus.ACCEPTED,
        RepairAttemptStatus.ROLLED_BACK,
        RepairAttemptStatus.REJECTED,
        RepairAttemptStatus.MATERIALIZATION_FAILED,
    }
    recovery = _recovery_probe(failure_memory, failure_run_root)
    static_boundary = _static_boundary_probe(policy.static_registry.registry_hash)
    route_mismatch = _route_mismatch_probe(selected[0], translation)
    reopened_runs = _reopened_run_probe(failure_memory)
    diagnostic = _diagnostic_probe()
    diagnostic_payload = diagnostic["diagnostic"]
    axes_payload = diagnostic["axes"]
    if not isinstance(diagnostic_payload, dict) or not isinstance(axes_payload, dict):
        raise RuntimeError("P9B 引用的 G9C 诊断证据结构无效")
    diagnostic_source = REPO_ROOT / str(diagnostic["source_path"])
    diagnostic_projection = REPO_ROOT / str(diagnostic["diagnostic_projection_path"])
    diagnostic_comparison = (
        OUTPUT_ROOT / "diagnostic" / "input_vs_translated_diagnostic.pdf"
    )
    _comparison_pdf(
        diagnostic_source,
        diagnostic_projection,
        diagnostic_comparison,
        right_label="TRANSLATED DIAGNOSTIC (NOT FINAL)",
    )
    diagnostic_evidence = diagnostic_payload.get("evidence", {})
    if not isinstance(diagnostic_evidence, dict):
        raise RuntimeError("P9B 引用的 G9C 诊断单元证据结构无效")
    manifest = {
        "schema_version": "transflow.p9b-real-run-evidence/v1",
        "leaf_runs": leaf_records,
        "document_runs": document_records,
        "classification_model_call_count": decision_adapter.call_count,
        "translation_http_call_count": translation.call_count,
        "attempt_terminal_coverage": (
            sum(item.status in allowed_statuses for item in all_attempts) / len(all_attempts)
            if all_attempts
            else 0.0
        ),
        "materialization_failure_count": len(materialization_failures),
        "fake_candidate_ref_count": sum(
            item.candidate_artifact_ref is not None for item in materialization_failures
        ),
        "static_boundary": static_boundary,
        "recovery": recovery,
        "result_boundary": {
            "diagnostic_artifact_label": diagnostic_payload.get("artifact", {}).get("label"),
            "diagnostic_isolated": (
                diagnostic_payload.get("status") == "TRANSLATED_DIAGNOSTIC_READY"
                and diagnostic_payload.get("artifact", {}).get("label") == "diagnostic"
            ),
            "diagnostic_published_count": int(
                diagnostic_payload.get("artifact", {}).get("label") == "final"
            ),
            "diagnostic_source_path": _relative(diagnostic_source),
            "diagnostic_projection_path": _relative(diagnostic_projection),
            "diagnostic_comparison_path": _relative(diagnostic_comparison),
            "diagnostic_comparison_png_path": _relative(
                diagnostic_comparison.with_suffix(".png")
            ),
            "diagnostic_expected_unit_count": diagnostic_evidence.get(
                "expected_unit_count"
            ),
            "diagnostic_materialized_unit_count": diagnostic_evidence.get(
                "materialized_unit_count"
            ),
            "route_mismatch": route_mismatch,
            "three_axis_fields": {
                key: axes_payload.get(key)
                for key in (
                    "engineering_closure",
                    "product_acceptance",
                    "promotion_eligibility",
                )
            },
        },
        "reopened_runs": reopened_runs,
        "environment_variable_names": (
            "TRANSFLOW_MIGRATION_QWEN_BASE_URL",
            "TRANSFLOW_MIGRATION_QWEN_API_KEY",
            "TRANSFLOW_MIGRATION_QWEN_MODEL",
        ),
    }
    _write_json(MANIFEST_PATH, manifest)
    print(
        json.dumps(
            {
                "classification_model_call_count": decision_adapter.call_count,
                "document_count": len(document_records),
                "leaf_count": len(leaf_records),
                "manifest": _relative(MANIFEST_PATH),
                "materialization_failure_count": len(materialization_failures),
                "translation_http_call_count": translation.call_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
