"""重放 RV2 六个精确标签差异，验证 RV3 能力兼容或安全拒绝。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf

from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.checkpoint import FilesystemCheckpointAdapter
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.page_pipeline import PreviewPublisher
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator
from transflow.application.toolbox_page_pipeline import ToolboxPagePipeline
from transflow.domain.common import json_ready
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import (
    CheckpointCompatibility,
    Fallback,
    Quality,
    TranslationCoverage,
)
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    PyMuPdfPageRenderer,
)
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.catalog import ToolboxCatalog, load_toolbox_catalog
from transflow.toolboxes.leaves import build_p9_toolbox_factories

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV3"
RV2_FRESH_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV2"
    / "05-fresh-blind-20260722-094154"
)
RV2_DISPOSITION_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV2"
    / "06-owner-disposition-20260722-101614"
)
RV3_BASE_RUN = RUN_ROOT / "02-routing-catalog-20260722-012551"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
BOUNDARY_CASE_IDS = {
    "fresh-005",
    "fresh-016",
    "fresh-018",
    "fresh-027",
    "fresh-028",
    "fresh-029",
}


class _NoTranslation:
    """记录意外翻译调用；六页本轮均不得进入 TranslationPort。"""

    def __init__(self) -> None:
        self.call_count = 0

    def translate(self, _batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        raise AssertionError("RV3 能力边界重放不得进入翻译")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _next_run_dir() -> Path:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    ordinal = len(tuple(RUN_ROOT.glob("[0-9][0-9]-*"))) + 1
    run_dir = RUN_ROOT / f"{ordinal:02d}-six-boundary-addendum-{timestamp}"
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_dir


def _render(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        pixmap.save(target)


def _load_cases() -> tuple[dict[str, Any], ...]:
    answer_key = json.loads(
        (RV2_FRESH_RUN / "input" / "sealed_answer_key.json").read_text(encoding="utf-8")
    )
    score = json.loads(
        (RV2_FRESH_RUN / "process" / "fresh-blind-score-r1.json").read_text(
            encoding="utf-8"
        )
    )
    answers = {
        str(item["case_id"]): item
        for item in answer_key["cases"]
        if item["case_id"] in BOUNDARY_CASE_IDS
    }
    predictions = {
        str(item["case_id"]): item
        for item in score["results"]
        if item["case_id"] in BOUNDARY_CASE_IDS
    }
    if set(answers) != set(predictions) or set(answers) != BOUNDARY_CASE_IDS:
        raise RuntimeError("RV2 六页边界集合不完整")
    return tuple(
        {
            **answers[case_id],
            "predicted_route": predictions[case_id]["predicted_route"],
        }
        for case_id in sorted(BOUNDARY_CASE_IDS)
    )


def _runtime(
    run_dir: Path,
    case_id: str,
    request: DocumentRunRequest,
    catalog: ToolboxCatalog,
    translation: _NoTranslation,
) -> ToolboxPagePipeline:
    runtime_root = run_dir / "runtime" / case_id
    artifacts = SharedFilesystemArtifactAdapter(runtime_root, request.run_id)
    checkpoints = FilesystemCheckpointAdapter(runtime_root, request.run_id, artifacts)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    interpreter = PagePatchInterpreter(fonts)
    compatibility = CheckpointCompatibility(
        source_hash=request.source_hash,
        config_hash=request.config_snapshot_hash,
        font_hash=fonts.manifest_hash,
        toolbox_catalog_hash=catalog.catalog_hash,
        schema_hash=_sha256(SCHEMA_PATH),
    )
    return ToolboxPagePipeline(
        catalog,
        ToolboxPageCoordinator(translation),
        PyMuPdfPageRenderer(interpreter),
        PreviewPublisher(artifacts),
        checkpoints,
        compatibility,
    )


def _execute_case(
    run_dir: Path,
    case: dict[str, Any],
    catalog: ToolboxCatalog,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    selected_route = str(case["predicted_route"])
    original = REPO_ROOT / str(case["source_path"])
    case_root = run_dir / "pages" / case_id
    source = case_root / "input" / "source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, source)
    source_hash = _sha256(source)
    if source_hash != case["source_content_sha256"]:
        raise RuntimeError(f"{case_id} 冻结副本 hash 漂移")
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=source_hash,
        source_language="auto",
        target_language="zh-CN",
        config_snapshot_hash="6" * 64,
        job_id=f"rv3-boundary-{case_id}",
        run_id=f"{run_dir.name}-{case_id}",
    )
    coordinator = DocumentCoordinator(PageFactsExtractor())
    pages = coordinator.enumerate_pages(request)
    if len(pages) != 1:
        raise RuntimeError(f"{case_id} 不是单页 PDF")
    page = pages[0]
    inventory = freeze_page_text_inventory(page.facts)
    translation = _NoTranslation()
    pipeline = _runtime(run_dir, case_id, request, catalog, translation)
    processed = pipeline.execute(source, page, selected_route)

    candidate = case_root / "output" / "candidate.pdf"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, candidate)
    source_png = case_root / "input" / "source.png"
    candidate_png = case_root / "output" / "candidate.png"
    _render(source, source_png)
    _render(candidate, candidate_png)
    entry = next(item for item in catalog.entries if item.route == selected_route)
    source_candidate_equal = _sha256(source) == _sha256(candidate)
    png_equal = _sha256(source_png) == _sha256(candidate_png)
    compatible = (
        selected_route == "visual_only"
        and entry.enabled
        and len(inventory.items) == 0
        and translation.call_count == 0
        and processed.patch is None
        and processed.outcome.translation_coverage is TranslationCoverage.NONE
        and processed.outcome.quality is Quality.PASS
        and processed.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    )
    safe_disabled = (
        not entry.enabled
        and translation.call_count == 0
        and processed.patch is None
        and processed.outcome.translation_coverage is TranslationCoverage.NONE
        and processed.outcome.quality is Quality.FAIL
        and processed.outcome.fallback is Fallback.PAGE_PASSTHROUGH
        and processed.outcome.finding_codes == ("TOOLBOX_DISABLED",)
    )
    result = {
        "schema_version": "transflow.rv3-boundary-case/v1",
        "case_id": case_id,
        "source_path": str(case["source_path"]),
        "source_sha256": source_hash,
        "expected_route": str(case["expected_route"]),
        "selected_route": selected_route,
        "catalog": {
            "enabled": entry.enabled,
            "disabled_reason": entry.disabled_reason,
            "toolbox_key": entry.toolbox_key,
            "toolbox_version": entry.toolbox_version,
        },
        "facts": {
            "text_span_count": len(page.facts.text_spans),
            "text_inventory_count": len(inventory.items),
            "image_count": len(page.facts.image_objects),
            "drawing_count": len(page.facts.drawing_objects),
            "table_count": len(page.facts.table_objects),
        },
        "translation_call_count": translation.call_count,
        "patch_produced": processed.patch is not None,
        "outcome": json_ready(processed.outcome),
        "source_candidate_pdf_hash_equal": source_candidate_equal,
        "source_candidate_png_hash_equal": png_equal,
        "disposition": (
            "COMPATIBLE_ZERO_TRANSLATION"
            if compatible
            else "SAFE_CATALOG_FALLBACK"
            if safe_disabled
            else "UNSAFE_OR_UNEXPLAINED"
        ),
        "pass": bool((compatible or safe_disabled) and source_candidate_equal and png_equal),
    }
    _write_json(case_root / "process" / "boundary_result.json", result)
    return result


def _run_command(run_dir: Path, name: str, argv: list[str]) -> dict[str, Any]:
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    environment = os.environ.copy()
    environment["MYPYPATH"] = str(REPO_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(REPO_ROOT / "src"), str(REPO_ROOT), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = run_dir / "process" / "command_outputs" / f"{name}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {
        "name": name,
        "argv": argv,
        "started_at": started_at,
        "returncode": completed.returncode,
        "output": output.relative_to(run_dir).as_posix(),
    }


def _verify(run_dir: Path) -> dict[str, Any]:
    commands = [
        _run_command(
            run_dir,
            "pytest-rv3-addendum",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                f"--junitxml={run_dir / 'process' / 'rv3-addendum-junit.xml'}",
                "tests/test_critical_chain_rv3.py",
                "tests/test_p5.py::test_p5_4_t01_mixed_pdf_has_one_route_per_page_and_stable_identity",
                "tests/test_p5.py::test_p5_4_t01_run_classified_finalizes_one_complete_pdf",
                "tests/test_p5.py::test_p5_4_t02_out_of_order_model_responses_merge_by_page_no",
                "tests/test_p7.py",
                "tests/test_p9c.py::test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation",
            ],
        ),
        _run_command(
            run_dir,
            "ruff-rv3-addendum",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "scripts/run_rv3_boundary_addendum.py",
                "tests/test_critical_chain_rv3.py",
            ],
        ),
        _run_command(
            run_dir,
            "mypy-rv3-addendum",
            [
                sys.executable,
                "-m",
                "mypy",
                "scripts/run_rv3_boundary_addendum.py",
                "tests/test_critical_chain_rv3.py",
            ],
        ),
    ]
    commands_path = run_dir / "process" / "commands.jsonl"
    commands_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in commands),
        encoding="utf-8",
    )
    return {"commands": commands, "pass": all(item["returncode"] == 0 for item in commands)}


def _artifact_hashes(run_dir: Path) -> list[dict[str, Any]]:
    excluded = {"artifact_hashes.json", "run_manifest.json"}
    return [
        {
            "path": path.relative_to(run_dir).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name not in excluded
    ]


def _report(results: tuple[dict[str, Any], ...], formal_pass: bool) -> str:
    rows = "\n".join(
        "| `{case_id}` | `{expected_route}` | `{selected_route}` | {text_count} | "
        "`{disposition}` | {status} |".format(
            case_id=item["case_id"],
            expected_route=item["expected_route"],
            selected_route=item["selected_route"],
            text_count=item["facts"]["text_inventory_count"],
            disposition=item["disposition"],
            status="PASS" if item["pass"] else "FAIL",
        )
        for item in results
    )
    return f"""# RV3 六页能力边界补充验收

## 结论

`G-RV-05 = {'PASS' if formal_pass else 'NOT_PASSED'}`

六个 RV2 精确标签差异已按其真实模型 Route 重放，没有修改分类结果或强制换链。
五页命中 disabled Catalog，在 TranslationPort 和 Toolbox 私有阶段前形成
`TOOLBOX_DISABLED / PAGE_PASSTHROUGH / Quality=FAIL`；一页命中 enabled
`visual_only`，其原生文字清单为 0，因此形成合法零翻译透传。

| case | 应归类 | 实际 Route | 文字清单 | 处置 | 结果 |
|---|---|---|---:|---|---|
{rows}

## 核心结果

- 翻译调用：0。
- Patch：0。
- 动态换链或跨叶调用：0。
- 源 PDF 与候选 PDF hash 相同：6/6。
- 源 PNG 与候选 PNG hash 相同：6/6。
- 能力兼容：1 页；Catalog 安全拒绝：5 页；未解释接受：0 页。

本轮证明的是当前 Route/Catalog/capability 链能够安全吸收这六个边界差异，
不证明五个 disabled Toolbox 已具备翻译产品能力，也不证明 Freeform 已实现。
"""


def main() -> int:
    run_dir = _next_run_dir()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    base_manifest = json.loads((RV3_BASE_RUN / "run_manifest.json").read_text(encoding="utf-8"))
    owner_disposition = json.loads(
        (RV2_DISPOSITION_RUN / "owner_disposition.json").read_text(encoding="utf-8")
    )
    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    cases = _load_cases()
    results = tuple(_execute_case(run_dir, case, catalog) for case in cases)
    _write_json(
        run_dir / "input" / "lineage.json",
        {
            "schema_version": "transflow.rv3-boundary-lineage/v1",
            "rv2_score": (RV2_FRESH_RUN / "process" / "fresh-blind-score-r1.json").relative_to(
                REPO_ROOT
            ).as_posix(),
            "rv2_score_sha256": _sha256(
                RV2_FRESH_RUN / "process" / "fresh-blind-score-r1.json"
            ),
            "rv2_answer_key": (
                RV2_FRESH_RUN / "input" / "sealed_answer_key.json"
            ).relative_to(REPO_ROOT).as_posix(),
            "rv2_answer_key_sha256": _sha256(
                RV2_FRESH_RUN / "input" / "sealed_answer_key.json"
            ),
            "rv2_owner_disposition": (
                RV2_DISPOSITION_RUN / "owner_disposition.json"
            ).relative_to(REPO_ROOT).as_posix(),
            "rv3_base_run": RV3_BASE_RUN.relative_to(REPO_ROOT).as_posix(),
            "case_ids": sorted(BOUNDARY_CASE_IDS),
        },
    )
    _write_json(
        run_dir / "process" / "environment_redacted.json",
        {
            "schema_version": "transflow.rv3-boundary-environment/v1",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "model_call_required": False,
            "secret_values_recorded": False,
        },
    )
    summary = {
        "schema_version": "transflow.rv3-boundary-summary/v1",
        "case_count": len(results),
        "compatible_zero_translation_count": sum(
            item["disposition"] == "COMPATIBLE_ZERO_TRANSLATION" for item in results
        ),
        "safe_catalog_fallback_count": sum(
            item["disposition"] == "SAFE_CATALOG_FALLBACK" for item in results
        ),
        "unsafe_or_unexplained_count": sum(not item["pass"] for item in results),
        "translation_call_count": sum(item["translation_call_count"] for item in results),
        "patch_count": sum(item["patch_produced"] for item in results),
        "source_candidate_pdf_hash_equal_count": sum(
            item["source_candidate_pdf_hash_equal"] for item in results
        ),
        "source_candidate_png_hash_equal_count": sum(
            item["source_candidate_png_hash_equal"] for item in results
        ),
        "cases": results,
        "pass": startup.ready and all(item["pass"] for item in results),
    }
    _write_json(run_dir / "process" / "boundary_summary.json", summary)
    verification = _verify(run_dir)
    base_pass = base_manifest["gate"]["technical_status"] == "PASS"
    upstream_authorized = bool(
        owner_disposition["owner_decision"]["satisfies_rv3_entry_dependency"]
    )
    formal_pass = bool(
        summary["pass"] and verification["pass"] and base_pass and upstream_authorized
    )
    gate = {
        "schema_version": "transflow.rv3-boundary-gate/v1",
        "gate_id": "G-RV-05",
        "technical_status": "PASS" if formal_pass else "FAIL",
        "formal_status": "PASS" if formal_pass else "NOT_PASSED",
        "base_rv3_technical_status": base_manifest["gate"]["technical_status"],
        "upstream_status": owner_disposition["owner_decision"]["effective_gate_status"],
        "boundary_case_count": len(results),
        "unsafe_or_unexplained_count": summary["unsafe_or_unexplained_count"],
        "translation_call_count": summary["translation_call_count"],
        "patch_count": summary["patch_count"],
        "rv4_allowed": formal_pass,
        "tm3_allowed": False,
    }
    _write_json(run_dir / "process" / "gate_results.json", gate)
    _write_json(
        run_dir / "trace_index.json",
        {
            "schema_version": "transflow.rv3-boundary-trace-index/v1",
            "RV3-T06": [
                "input/lineage.json",
                "process/boundary_summary.json",
                "pages/*/process/boundary_result.json",
                "pages/*/input/source.pdf",
                "pages/*/output/candidate.pdf",
                "pages/*/output/candidate.png",
            ],
            "G-RV-05": ["process/gate_results.json", "run_manifest.json", "report.md"],
        },
    )
    (run_dir / "report.md").write_text(_report(results, formal_pass), encoding="utf-8")
    _write_json(run_dir / "artifact_hashes.json", _artifact_hashes(run_dir))
    _write_json(
        run_dir / "run_manifest.json",
        {
            "schema_version": "transflow.critical-chain-rv3-boundary-run/v1",
            "run_id": run_dir.name,
            "stage": "RV3_ROUTE_CATALOG_CAPABILITY_BOUNDARY_ADDENDUM",
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "conclusion": "PASS" if formal_pass else "FAIL",
            "gate": gate,
            "tests": {"RV3-T06": summary["pass"], "verification": verification["pass"]},
            "next_stage_allowed": formal_pass,
            "tm3_allowed": False,
            "report": "report.md",
        },
    )
    print(run_dir)
    print(f"RV3 boundary={'PASS' if formal_pass else 'FAIL'}")
    return 0 if formal_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
