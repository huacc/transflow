"""执行 RV4 文字分母、语义映射与翻译完整性重新验收。"""

# ruff: noqa: E402

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymupdf

from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.translated_diagnostic import (
    DiagnosticPageInput,
    TranslatedDiagnosticMaterializer,
)
from transflow.application.translation_completeness import (
    TranslationCompletenessGate,
    build_semantic_unit_map,
)
from transflow.domain.common import json_ready
from transflow.domain.completeness import CompletenessStatus, SemanticUnitMap
from transflow.domain.delivery import DiagnosticStatus
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.text_inventory import InventoryDisposition
from transflow.domain.translation import TranslationBatch, TranslationUnit
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.pdf_kernel.patch import PagePatchInterpreter
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.contracts import PageTemplate, TranslationDispatch
from transflow.toolboxes.leaves import SingleFlowTextToolbox
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV4"
SAMPLE_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
P0101 = (
    REPO_ROOT
    / "runs/toolbox_leaf_migration/TM2/05-body-flow-text-single-20260721-133143"
    / "cases/04-short-p0101/input/source.pdf"
)
P0151 = (
    REPO_ROOT
    / "runs/critical_chain_revalidation/RV3/02-routing-catalog-20260722-012551"
    / "pages/p0151/input/source.pdf"
)
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
P8_POLICY = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"
SCHEMA_PATH = REPO_ROOT / "resources/schemas/semantic_unit_map_v2.schema.json"
STRATIFIED_CATEGORIES = (
    "cover",
    "contents",
    "end",
    "visual_only",
    "body.flow_text.single",
    "body.flow_text.multi",
    "body.table",
    "body.chart",
    "body.diagram",
    "body.anchored_blocks",
    "body.composite.flow_text_table",
    "body.composite.flow_text_chart",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _next_run_dir() -> Path:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    sequences = tuple(
        int(path.name.split("-", 1)[0])
        for path in RUN_ROOT.iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    )
    sequence = max(sequences, default=0) + 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUN_ROOT / f"{sequence:02d}-translation-completeness-{stamp}"
    path.mkdir()
    return path


def _enumerate(path: Path, run_id: str) -> EnumeratedPage:
    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="4" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]


def _copy_source(source: Path, target: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _coverage(inventory: Any, semantic_map: SemanticUnitMap) -> dict[str, Any]:
    inventory_ids = {item.object_id for item in inventory.items}
    mapped_ids = tuple(
        object_id
        for entry in semantic_map.entries
        for object_id in entry.source_object_ids
    )
    return {
        "duplicate_source_object_count": len(mapped_ids) - len(set(mapped_ids)),
        "inventory_object_count": len(inventory_ids),
        "map_source_object_count": len(mapped_ids),
        "missing_source_object_ids": sorted(inventory_ids - set(mapped_ids)),
        "extra_source_object_ids": sorted(set(mapped_ids) - inventory_ids),
        "ratio": (
            1.0
            if not inventory_ids
            else len(set(mapped_ids) & inventory_ids) / len(inventory_ids)
        ),
        "unresolved_unit_count": len(semantic_map.unresolved_unit_ids),
        "unsupported_unit_count": len(semantic_map.unsupported_unit_ids),
    }


def _generic_map(
    page: EnumeratedPage,
    owner: str,
) -> tuple[Any, TranslationBatch | None, SemanticUnitMap, dict[str, Any]]:
    inventory = freeze_page_text_inventory(page.facts)
    spans = {item.object_id: item for item in page.facts.text_spans if item.text.strip()}
    translate_ids = tuple(
        item.object_id
        for item in inventory.items
        if item.disposition is InventoryDisposition.TRANSLATE
    )
    template = PageTemplate(
        f"rv4-{owner.replace('.', '-')}-{page.facts.page_identity[:20]}",
        page.context,
        page.facts.kernel_facts_hash,
        owner,
        translate_ids,
    )
    batch = (
        TranslationBatch(
            f"batch-{page.context.run_id}-{owner}",
            "en",
            "zh-CN",
            tuple(
                TranslationUnit(
                    hashlib.sha256(
                        f"{page.facts.page_identity}\0{object_id}\0rv4".encode("ascii")
                    ).hexdigest(),
                    page.context.page_no,
                    ordinal,
                    spans[object_id].text,
                    f"{owner}-r{ordinal:04d}",
                )
                for ordinal, object_id in enumerate(translate_ids)
            ),
        )
        if translate_ids
        else None
    )
    semantic_map = build_semantic_unit_map(template, batch, page.facts, inventory)
    if SemanticUnitMap.from_dict(semantic_map.to_dict()) != semantic_map:
        raise RuntimeError("SemanticUnitMap v2 往返不一致")
    return inventory, batch, semantic_map, _coverage(inventory, semantic_map)


def _single_case(
    source: Path,
    run_id: str,
) -> tuple[
    EnumeratedPage,
    SingleFlowTextToolbox,
    PageTemplate,
    TranslationBatch,
    Any,
    SemanticUnitMap,
]:
    page = _enumerate(source, run_id)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY),
        fonts.resolve(FONT_ID).path,
    )
    inventory = freeze_page_text_inventory(page.facts)
    template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(template)
    if batch is None:
        raise RuntimeError("p0101 未形成翻译请求")
    semantic_map = build_semantic_unit_map(template, batch, page.facts, inventory)
    return page, toolbox, template, batch, inventory, semantic_map


def _save_translation_case(
    process_root: Path,
    name: str,
    inventory: Any,
    batch: TranslationBatch,
    semantic_map: SemanticUnitMap,
    adapter: MigrationQwenTranslationAdapter,
) -> dict[str, Any]:
    gate = TranslationCompletenessGate(maximum_targeted_retries=1).execute(
        semantic_map,
        batch,
        adapter,
    )
    case_root = process_root / name
    _write_json(case_root / "inventory.json", inventory.to_dict())
    _write_json(case_root / "semantic_unit_map.json", semantic_map.to_dict())
    _write_json(case_root / "translation_batch.json", batch)
    _write_json(case_root / "translation_bundle.json", gate.bundle)
    _write_json(case_root / "completeness_decision.json", gate.decision.to_dict())
    coverage = _coverage(inventory, semantic_map)
    _write_json(case_root / "coverage.json", coverage)
    return {
        "bundle_present": gate.bundle is not None,
        "coverage": coverage,
        "decision_hash": gate.decision.decision_hash,
        "map_hash": semantic_map.map_hash,
        "provider_bundle_count": len(gate.provider_bundles),
        "request_count": len(gate.request_batches),
        "status": gate.decision.status.value,
        "translated_unit_count": len(semantic_map.translated_unit_ids),
    }


def _stratified_samples(run_dir: Path) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    for ordinal, route in enumerate(STRATIFIED_CATEGORIES, start=1):
        root = SAMPLE_ROOT.joinpath(*route.split("."))
        candidates = sorted(root.glob("*.pdf"), key=lambda path: (path.stat().st_size, path.name))
        if not candidates:
            raise FileNotFoundError(f"分类原文单页缺失:{route}")
        source = candidates[len(candidates) // 2]
        target = _copy_source(
            source,
            run_dir / "input/stratified" / f"{ordinal:02d}-{route.replace('.', '-')}.pdf",
        )
        page = _enumerate(target, f"{run_dir.name}-stratified-{ordinal:02d}")
        inventory, batch, semantic_map, coverage = _generic_map(page, route)
        _write_json(
            run_dir / "process/stratified" / f"{ordinal:02d}.json",
            {
                "batch_unit_count": len(batch.units) if batch is not None else 0,
                "coverage": coverage,
                "inventory": inventory.to_dict(),
                "route": route,
                "semantic_unit_map": semantic_map.to_dict(),
                "source_name": source.name,
                "source_sha256": _sha256(source),
            },
        )
        output.append(
            {
                "coverage": coverage,
                "route": route,
                "source_name": source.name,
                "source_sha256": _sha256(source),
            }
        )
    return tuple(output)


def _diagnostic_case(run_dir: Path) -> dict[str, Any]:
    source = run_dir / "input/t05-layout-failure-source.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(60, 120, 360, 220),
            "Revenue increased 10%",
            fontname="helv",
            fontsize=12,
        )
        document.save(source)
    page, toolbox, template, batch, inventory, semantic_map = _single_case(
        source,
        f"{run_dir.name}-t05",
    )
    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        semantic_map,
        batch,
        FixedTranslationAdapter({semantic_map.translated_unit_ids[0]: "收入增长 10%"}),
    )
    if gate.bundle is None:
        raise RuntimeError("RV4-T05 完整译文未形成")
    plan = toolbox.consume_translation_bundle(
        template,
        TranslationDispatch(batch=batch, bundle=gate.bundle),
    )
    artifacts = SharedFilesystemArtifactAdapter(run_dir, run_dir.name)
    candidate = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        artifacts,
        run_dir,
    ).materialize_page(
        source,
        DiagnosticPageInput(
            page.context,
            page.facts,
            plan.patch,
            semantic_map,
            gate.bundle,
            gate.decision,
        ),
    )
    _write_json(run_dir / "process/t05_diagnostic.json", candidate.to_dict())
    if candidate.artifact is not None and candidate.artifact.relative_path is not None:
        pdf_path = run_dir / candidate.artifact.relative_path
        with pymupdf.open(pdf_path) as document:
            pixmap = document[0].get_pixmap(dpi=144, alpha=False)
            preview = run_dir / "output/t05-diagnostic.png"
            preview.parent.mkdir(parents=True, exist_ok=True)
            pixmap.save(preview)
    return {
        "bundle_present": gate.bundle is not None,
        "coverage": _coverage(inventory, semantic_map),
        "diagnostic_artifact": (
            candidate.artifact.relative_path if candidate.artifact is not None else None
        ),
        "diagnostic_status": candidate.status.value,
        "final_artifact_count": len(tuple(run_dir.rglob("final*.pdf"))),
    }


def _run_verification(run_dir: Path) -> tuple[dict[str, Any], ...]:
    commands = (
        (sys.executable, "-m", "pytest", "-q", "tests/test_critical_chain_rv4.py"),
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_toolbox_leaf_migration_tm2.py",
            "tests/test_p9c.py::test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text",
            "tests/test_p9c.py::test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full",
            "tests/test_p9c.py::test_p9c_2_t04_keep_source_reasons_and_required_literals_are_auditable",
            "tests/test_p9c.py::test_p9c_2_t06_completeness_checkpoint_recovers_without_retranslation",
        ),
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "src/transflow/domain/completeness.py",
            "src/transflow/domain/text_inventory.py",
            "src/transflow/application/translation_completeness.py",
            "src/transflow/application/translated_diagnostic.py",
            "src/transflow/application/route_capability.py",
            "src/transflow/domain/result_axes.py",
            "src/transflow/toolboxes/leaves/body_flow_text_single",
            "tests/test_critical_chain_rv4.py",
        ),
    )
    results: list[dict[str, Any]] = []
    for command in commands:
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        results.append(
            {
                "command": list(command),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "returncode": completed.returncode,
                "stderr": completed.stderr,
                "stdout": completed.stdout,
            }
        )
    _write_json(run_dir / "process/verification.json", results)
    return tuple(results)


def _report(
    run_dir: Path,
    p0101: dict[str, Any],
    p0151: dict[str, Any],
    stratified: tuple[dict[str, Any], ...],
    diagnostic: dict[str, Any],
    verification: tuple[dict[str, Any], ...],
    qwen_calls: int,
    passed: bool,
) -> None:
    report = f"""# RV4 文字分母、语义映射与翻译完整性重新验收

- 运行：{run_dir.relative_to(REPO_ROOT).as_posix()}
- 结论：G-RV-06 = {"PASS" if passed else "FAIL"}
- 真实模型 HTTP 调用：{qwen_calls}
- 整本 PDF 执行：0；仅使用 p0101、p0151 两个冻结单页
- 新分层原文单页：{len(stratified)}

## 核心结果

- p0101：Inventory/Map 覆盖率 {p0101["coverage"]["ratio"]:.0%}，语义页脚进入翻译，
  纯页码单独 KEEP_SOURCE/PAGE_NUMBER，真实千问完整性 {p0101["status"]}。
- p0151：Inventory/Map 覆盖率 {p0151["coverage"]["ratio"]:.0%}，正文和小表格文字
  无 unresolved unit，真实千问完整性 {p0151["status"]}。
- 12 类分层单页：全部只做文字分母和 v2 map 结构重放，不运行整本，也不宣称对应
  disabled Toolbox 已实现。
- RV4-T05：完整译文的隔离诊断状态 {diagnostic["diagnostic_status"]}，
  final 产物数 {diagnostic["final_artifact_count"]}。
- 定向验证：{sum(item["returncode"] == 0 for item in verification)}/{len(verification)}
  条命令通过。

## 边界

页眉和语义页脚在 SemanticUnitMap 中使用 shared.margin.header/footer owner，
不再由正文分类决定是否翻译。当前 single 叶仍复用已有文字布局能力物化这些 region；
跨页重复识别和统一 margin 排版属于 RV5，不在 RV4 过度扩展。
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")


def main() -> int:
    if not migration_translation_environment_ready():
        raise RuntimeError("RV4 真实重放缺少千问迁移环境变量")
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(SCHEMA_PATH)
    run_dir = _next_run_dir()
    for relative in ("input", "process", "output"):
        (run_dir / relative).mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "process/environment.json",
        {
            "api_key_persisted": False,
            "base_url_configured": True,
            "model_configured": True,
            "python": sys.version,
            "schema_sha256": _sha256(SCHEMA_PATH),
        },
    )

    verification = _run_verification(run_dir)
    # p0151 由大量短 span 组成；小分片降低结构化响应一次遗漏 ID 的概率，
    # 不改变 PageTextInventory、SemanticUnitMap 或最终 Bundle 身份。
    adapter = MigrationQwenTranslationAdapter(timeout_seconds=240.0, chunk_size=8)

    p0101_source = _copy_source(P0101, run_dir / "input/p0101-source.pdf")
    page, _, _, batch, inventory, semantic_map = _single_case(
        p0101_source,
        f"{run_dir.name}-p0101",
    )
    p0101 = _save_translation_case(
        run_dir / "process", "p0101", inventory, batch, semantic_map, adapter
    )
    footer = next(
        item
        for item in semantic_map.entries
        if item.source_text == "Corporate Governance Report"
    )
    page_number = next(item for item in semantic_map.entries if item.source_text == "99")
    if page_number.keep_source_reason is None:
        raise RuntimeError("p0101 纯页码缺少 KEEP_SOURCE 原因")
    p0101["footer"] = {
        "disposition": footer.disposition.value,
        "owner": footer.owner,
        "unit_id": footer.unit_id,
    }
    p0101["page_number"] = {
        "disposition": page_number.disposition.value,
        "keep_source_reason": page_number.keep_source_reason.value,
        "owner": page_number.owner,
    }
    p0101["page_identity"] = page.facts.page_identity
    _write_json(run_dir / "process/p0101/summary.json", p0101)

    p0151_source = _copy_source(P0151, run_dir / "input/p0151-source.pdf")
    p0151_page = _enumerate(p0151_source, f"{run_dir.name}-p0151")
    p0151_inventory, p0151_batch, p0151_map, _ = _generic_map(
        p0151_page, "body.composite.flow_text_table"
    )
    if p0151_batch is None:
        raise RuntimeError("p0151 未形成翻译请求")
    p0151 = _save_translation_case(
        run_dir / "process",
        "p0151",
        p0151_inventory,
        p0151_batch,
        p0151_map,
        adapter,
    )
    p0151["table_count"] = len(p0151_page.facts.table_objects)
    p0151["table_text_object_count"] = sum(
        len(item.text_object_ids) for item in p0151_page.facts.table_objects
    )
    _write_json(run_dir / "process/p0151/summary.json", p0151)

    stratified = _stratified_samples(run_dir)
    diagnostic = _diagnostic_case(run_dir)
    all_coverage = (
        p0101["coverage"],
        p0151["coverage"],
        *(item["coverage"] for item in stratified),
        diagnostic["coverage"],
    )
    passed = (
        all(item["returncode"] == 0 for item in verification)
        and p0101["status"] == CompletenessStatus.PASS.value
        and p0151["status"] == CompletenessStatus.PASS.value
        and all(
            item["ratio"] == 1.0
            and item["duplicate_source_object_count"] == 0
            and not item["missing_source_object_ids"]
            and not item["extra_source_object_ids"]
            and item["unresolved_unit_count"] == 0
            and item["unsupported_unit_count"] == 0
            for item in all_coverage
        )
        and diagnostic["diagnostic_status"]
        == DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY.value
        and diagnostic["final_artifact_count"] == 0
    )
    gate = {
        "gate": "G-RV-06",
        "metrics": {
            "inventory_map_bundle_coverage": 1.0 if passed else None,
            "qwen_http_call_count": adapter.call_count,
            "stratified_single_page_count": len(stratified),
            "target_passthrough_accepted_count": 0 if passed else None,
            "unauthorized_source_residual_accepted_count": 0 if passed else None,
        },
        "p0101": p0101,
        "p0151": p0151,
        "schema_version": "transflow.rv4-gate/v1",
        "status": "PASS" if passed else "FAIL",
    }
    _write_json(run_dir / "gate.json", gate)
    _write_json(run_dir / "process/stratified_summary.json", stratified)
    _write_json(run_dir / "process/diagnostic_summary.json", diagnostic)
    _report(
        run_dir,
        p0101,
        p0151,
        stratified,
        diagnostic,
        verification,
        adapter.call_count,
        passed,
    )
    gate_hash = _sha256(run_dir / "gate.json")
    _write_json(
        REPO_ROOT / "resources/manifests/rv4_gate.json",
        {
            "gate": "G-RV-06",
            "gate_sha256": gate_hash,
            "run": run_dir.relative_to(REPO_ROOT).as_posix(),
            "schema_version": "transflow.current-gate-pointer/v1",
            "status": gate["status"],
        },
    )
    print(run_dir)
    print(gate["status"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
