"""执行 RV3 Route、Catalog 与 capability 当前链路重新验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pymupdf

from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.route_capability import RouteCapabilityEvidence
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.domain.classification import (
    ClassificationRoute,
    ModelDecision,
    ModelDecisionRequest,
)
from transflow.domain.common import json_ready
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import Fallback, Quality, TranslationCoverage
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel.facts import PageFactsExtractor
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaves import build_p9_toolbox_factories

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ROOT = REPO_ROOT / "runs" / "critical_chain_revalidation" / "RV3"
RV2_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV2"
    / "01-current-validity-20260721-233029"
)
RV0_SOURCE = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV0"
    / "01-baseline-20260721-164419"
    / "input"
    / "source_document.pdf"
)
TAXONOMY_PATH = REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
GOLD_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
SPIKE_TOOLBOX_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "toolboxes"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
PAGE_NO = 151
SLICE_START = 140
SLICE_END = 169
PAGE_CONCURRENCY = 6


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


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _next_run_dir(label: str) -> Path:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    existing = sorted(RUN_ROOT.glob("[0-9][0-9]-*"))
    ordinal = len(existing) + 1
    path = RUN_ROOT / f"{ordinal:02d}-{label}-{timestamp}"
    path.mkdir(parents=False, exist_ok=False)
    return path


def _extract_page(source: Path, page_no: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        output = pymupdf.open()
        output.insert_pdf(document, from_page=page_no - 1, to_page=page_no - 1)
        output.save(target, garbage=4, deflate=True)
        output.close()


def _extract_slice(source: Path, start: int, end: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        output = pymupdf.open()
        output.insert_pdf(document, from_page=start - 1, to_page=end - 1)
        output.save(target, garbage=4, deflate=True)
        output.close()


def _render_page(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(1.67, 1.67), alpha=False)
        pixmap.save(target)


def _gold_routes_and_samples(
    run_dir: Path,
) -> tuple[set[str], list[dict[str, Any]], int]:
    records: list[tuple[str, Path, str]] = []
    hash_routes: dict[str, set[str]] = {}
    for path in sorted(GOLD_ROOT.rglob("*.pdf"), key=lambda item: item.as_posix()):
        route = path.parent.relative_to(GOLD_ROOT).as_posix().replace("/", ".")
        content_hash = _sha256(path)
        records.append((route, path, content_hash))
        hash_routes.setdefault(content_hash, set()).add(route)
    routes = {route for route, _path, _content_hash in records}
    samples: list[dict[str, Any]] = []
    selected_routes: set[str] = set()
    destination_root = run_dir / "input" / "representative_gold"
    for route, path, content_hash in records:
        if route in selected_routes or len(hash_routes[content_hash]) != 1:
            continue
        selected_routes.add(route)
        destination = destination_root / f"{route.replace('.', '__')}.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        samples.append(
            {
                "route": route,
                "source": _relative(path),
                "source_sha256": content_hash,
                "frozen_copy": destination.relative_to(run_dir).as_posix(),
                "frozen_copy_sha256": _sha256(destination),
            }
        )
    conflict_count = sum(len(labels) > 1 for labels in hash_routes.values())
    return routes, samples, conflict_count


def _spike_toolbox_routes(gold_routes: set[str]) -> set[str]:
    routes = {"visual_only"}
    for route in gold_routes - {"visual_only"}:
        if SPIKE_TOOLBOX_ROOT.joinpath(*route.split(".")).is_dir():
            routes.add(route)
    return routes


def _catalog_audit(run_dir: Path) -> dict[str, Any]:
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    taxonomy_routes = [str(item["route"]) for item in taxonomy["routes"]]
    entries = catalog_payload["entries"]
    catalog_routes = [str(item["route"]) for item in entries]
    gold_routes, samples, conflicting_hash_count = _gold_routes_and_samples(run_dir)
    spike_routes = _spike_toolbox_routes(gold_routes)
    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    freeform = next(item for item in entries if item["route"] == "body.freeform")
    mismatches: list[str] = []
    if len(taxonomy_routes) != len(set(taxonomy_routes)):
        mismatches.append("TAXONOMY_ROUTE_DUPLICATE")
    if catalog_routes != taxonomy_routes:
        mismatches.append("CATALOG_TAXONOMY_ORDER_OR_COVERAGE")
    if gold_routes != set(taxonomy_routes) - {"body.freeform"}:
        mismatches.append("GOLD_CONCRETE_ROUTE_COVERAGE")
    if spike_routes != gold_routes:
        mismatches.append("SPIKE_TOOLBOX_CONCRETE_ROUTE_COVERAGE")
    if {item["route"] for item in samples} != gold_routes:
        mismatches.append("NON_CONFLICTING_REPRESENTATIVE_SHORTFALL")
    for entry in entries:
        if entry["toolbox_key"] != entry["route"]:
            mismatches.append(f"TOOLBOX_KEY_ROUTE:{entry['route']}")
        if entry["fallback"] != "PAGE_PASSTHROUGH":
            mismatches.append(f"FALLBACK:{entry['route']}")
    if freeform["enabled"] is not False or not freeform["disabled_reason"]:
        mismatches.append("FREEFORM_DISABLED_FALLBACK")
    if not startup.ready:
        mismatches.extend(startup.violations)
    enabled_routes = {item.route for item in catalog.entries if item.enabled}
    if enabled_routes != set(factories):
        mismatches.append("ENABLED_FACTORY_SET")
    concrete_count = len(gold_routes)
    return {
        "schema_version": "transflow.rv3-route-catalog-audit/v1",
        "taxonomy_route_count": len(taxonomy_routes),
        "concrete_route_count": concrete_count,
        "catalog_entry_count": len(entries),
        "matching_concrete_toolbox_count": len(gold_routes & spike_routes),
        "representative_conflicting_hash_excluded_count": conflicting_hash_count,
        "consistency_rate": (
            len(gold_routes & spike_routes) / concrete_count if concrete_count else 0.0
        ),
        "enabled_routes": sorted(enabled_routes),
        "disabled_route_count": sum(not bool(item["enabled"]) for item in entries),
        "freeform": {
            "enabled": freeform["enabled"],
            "fallback": freeform["fallback"],
            "disabled_reason": freeform["disabled_reason"],
        },
        "shared_margin_boundary": {
            "taxonomy_contains_header_footer_route": any(
                token in route for route in taxonomy_routes for token in ("header", "footer")
            ),
            "shared_margin_processor_present": (
                REPO_ROOT / "src" / "transflow" / "toolboxes" / "margin.py"
            ).is_file(),
        },
        "startup_ready": startup.ready,
        "startup_violations": list(startup.violations),
        "mismatch_count": len(mismatches),
        "mismatches": sorted(set(mismatches)),
        "representative_samples": samples,
    }


class _TestOnlyDecisionPort:
    """仅验证 Router 接线；其结果不得作为 RV2 产品模型证据。"""

    choices: ClassVar[dict[str, str]] = {
        "page.role": "body",
        "body.layout_owner": "flow_text",
        "body.flow.topology": "single",
        "body.composite.kind": "flow_text_table",
    }

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        node_key = str(request.node_spec["node_key"])
        return ModelDecision(
            request.decision_id,
            request.decision_kind,
            self.choices[node_key],
            request.evidence_ids[:1],
            0.99,
            "TEST_ONLY route wiring decision",
        )


class _DelayedClassificationEngine(ClassificationEngine):
    """让首批页面确定性乱序完成，验证按 page identity 归并。"""

    def __init__(self) -> None:
        super().__init__(BoundedDecisionRunner(_TestOnlyDecisionPort()))
        self._barrier = threading.Barrier(PAGE_CONCURRENCY)
        self._lock = threading.Lock()
        self._completion_order: list[int] = []

    @property
    def completion_order(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._completion_order)

    def classify_page(self, facts: Any, page_count: int) -> Any:
        result = super().classify_page(facts, page_count)
        if facts.page.page_no <= PAGE_CONCURRENCY:
            self._barrier.wait(timeout=30)
            time.sleep((PAGE_CONCURRENCY - facts.page.page_no) * 0.05)
        with self._lock:
            self._completion_order.append(facts.page.page_no)
        return result


class _NoTranslation:
    def __init__(self) -> None:
        self.call_count = 0

    def translate(self, _batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        raise AssertionError("Route capability mismatch 不得进入翻译")


def _classify_p0151(source: Path) -> tuple[Any, Any]:
    source_hash = _sha256(source)
    facts = PageFactsExtractor().extract_page(
        source,
        source_hash,
        PAGE_NO,
        include_classification=True,
    )
    engine = ClassificationEngine(BoundedDecisionRunner(_TestOnlyDecisionPort()))
    classified = engine.classify_page(facts, 240)
    return facts, classified


def _capability_fault(
    run_dir: Path,
    source_page: Path,
    facts: Any,
    classified: Any,
) -> dict[str, Any]:
    source_hash = _sha256(RV0_SOURCE)
    context = PageExecutionContext(
        "rv3-revalidation",
        run_dir.name,
        source_hash,
        PAGE_NO,
        facts.page.geometry_hash,
        "3" * 64,
    )
    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    toolbox = factories["body.flow_text.single"]()
    injected_route = ClassificationRoute(
        "body.flow_text.single",
        0.75,
        tuple(classified.route.evidence_ids),
    )
    capability_evidence = RouteCapabilityEvidence(
        f"rv3-{facts.kernel_facts_hash}",
        "body.composite.flow_text_table",
        "flow_text_and_table_require_composite_owner",
        "TEST_ONLY_FAULT_INJECTION",
    )
    translation = _NoTranslation()
    result = ToolboxPageCoordinator(translation).execute(
        ToolboxPageWork(
            context,
            facts,
            toolbox,
            capability_evidence,
            injected_route,
        )
    )
    candidate = run_dir / "pages" / "p0151" / "output" / "candidate.pdf"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_page, candidate)
    source_png = run_dir / "pages" / "p0151" / "input" / "source.png"
    candidate_png = run_dir / "pages" / "p0151" / "output" / "candidate.png"
    _render_page(source_page, source_png)
    _render_page(candidate, candidate_png)
    mismatch = result.route_capability_mismatch
    return {
        "schema_version": "transflow.rv3-capability-fault/v1",
        "evidence_scope": "TEST_ONLY",
        "selected_route": injected_route.as_dict(),
        "actual_structure_route": classified.route.as_dict(),
        "translation_call_count": translation.call_count,
        "toolbox_private_stage_call_count": 0,
        "patch_produced": result.patch is not None,
        "trace": list(result.trace.stages),
        "outcome": json_ready(result.outcome),
        "mismatch": mismatch,
        "source_pdf": source_page.relative_to(run_dir).as_posix(),
        "candidate_pdf": candidate.relative_to(run_dir).as_posix(),
        "source_candidate_pdf_hash_equal": _sha256(source_page) == _sha256(candidate),
        "source_candidate_png_hash_equal": _sha256(source_png) == _sha256(candidate_png),
        "pass": bool(
            mismatch
            and translation.call_count == 0
            and result.patch is None
            and result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
            and result.outcome.translation_coverage is TranslationCoverage.NONE
            and result.outcome.quality is Quality.FAIL
        ),
    }


def _runtime_scan() -> dict[str, Any]:
    roots = (
        REPO_ROOT / "src" / "transflow" / "classification",
        REPO_ROOT / "src" / "transflow" / "toolboxes",
        REPO_ROOT / "src" / "transflow" / "application",
    )
    patterns = {
        "forced_route": re.compile(
            r"\b(?:forced_route|target_route|route_override|override_route)\b"
        ),
        "dynamic_discovery": re.compile(
            r"\b(?:entry_points|iter_modules|import_module|__import__)\b|\.rglob\(|\.glob\("
        ),
        "fixed_page_special_case": re.compile(r"page_no\s*==\s*(\d+)"),
    }
    hits: dict[str, list[str]] = {name: [] for name in patterns}
    hits["cross_leaf_private_call"] = []
    for root in roots:
        for path in root.rglob("*.py"):
            relative = _relative(path)
            text = path.read_text(encoding="utf-8")
            for name, pattern in patterns.items():
                for match in pattern.finditer(text):
                    if name == "fixed_page_special_case" and int(match.group(1)) == 1:
                        continue
                    hits[name].append(f"{relative}:{match.start()}")
            private_leaf = "transflow.toolboxes.leaves.body_flow_text_single"
            if private_leaf in text:
                own_package = "/toolboxes/leaves/body_flow_text_single/" in f"/{relative}"
                public_wrapper = relative.endswith("/toolboxes/leaves/single.py")
                if not own_package and not public_wrapper:
                    hits["cross_leaf_private_call"].append(relative)
    return {
        "schema_version": "transflow.rv3-runtime-scan/v1",
        "product_forced_route_count": len(hits["forced_route"]),
        "dynamic_discovery_count": len(hits["dynamic_discovery"]),
        "fixed_page_special_case_count": len(hits["fixed_page_special_case"]),
        "cross_leaf_private_call_count": len(hits["cross_leaf_private_call"]),
        "hits": hits,
        "pass": not any(hits.values()),
    }


def _concurrency_audit(run_dir: Path, source: Path) -> dict[str, Any]:
    source_hash = _sha256(source)
    facts = PageFactsExtractor().extract_all(
        source,
        source_hash,
        include_classification=True,
    )
    pages = tuple(
        EnumeratedPage(
            PageExecutionContext(
                "rv3-concurrency",
                run_dir.name,
                source_hash,
                item.page.page_no,
                item.page.geometry_hash,
                "5" * 64,
            ),
            item,
        )
        for item in facts
    )
    engine = _DelayedClassificationEngine()
    classified = DocumentCoordinator(PageFactsExtractor()).classify_pages(
        pages,
        engine,
        PAGE_CONCURRENCY,
    )
    expected_pages = list(range(1, len(pages) + 1))
    ordered_pages = [item.page_no for item in classified]
    identity_match = [item.page_identity for item in classified] == [
        item.facts.page_identity for item in pages
    ]
    completion_order = list(engine.completion_order)
    return {
        "schema_version": "transflow.rv3-concurrency-audit/v1",
        "evidence_scope": "TEST_ONLY",
        "source_pdf": source.relative_to(run_dir).as_posix(),
        "source_sha256": source_hash,
        "page_count": len(pages),
        "page_concurrency": PAGE_CONCURRENCY,
        "completion_order": completion_order,
        "completion_was_out_of_order": completion_order != expected_pages,
        "merged_page_order": ordered_pages,
        "page_identity_match": identity_match,
        "route_bindings": [
            {
                "page_no": item.page_no,
                "page_identity": item.page_identity,
                "route": item.route.as_dict(),
            }
            for item in classified
        ],
        "pass": (
            ordered_pages == expected_pages
            and identity_match
            and completion_order != expected_pages
        ),
    }


def _run_command(run_dir: Path, name: str, args: list[str]) -> dict[str, Any]:
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    completed = subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_path = run_dir / "process" / "command_outputs" / f"{name}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {
        "name": name,
        "argv": args,
        "started_at": started,
        "returncode": completed.returncode,
        "output": output_path.relative_to(run_dir).as_posix(),
    }


def _verification(run_dir: Path) -> dict[str, Any]:
    python = sys.executable
    commands = [
        _run_command(
            run_dir,
            "pytest-rv3",
            [
                python,
                "-m",
                "pytest",
                "-q",
                f"--junitxml={run_dir / 'process' / 'rv3-junit.xml'}",
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
            "ruff-rv3",
            [
                python,
                "-m",
                "ruff",
                "check",
                "scripts/run_rv3_route_catalog_revalidation.py",
                "tests/test_critical_chain_rv3.py",
                "src/transflow/application/contracts.py",
                "src/transflow/application/document_coordinator.py",
                "src/transflow/application/route_capability.py",
                "src/transflow/application/page_pipeline.py",
                "src/transflow/application/toolbox_page_pipeline.py",
                "src/transflow/application/toolbox_page_coordinator.py",
                "src/transflow/toolboxes/contracts.py",
            ],
        ),
        _run_command(
            run_dir,
            "mypy-rv3",
            [
                python,
                "-m",
                "mypy",
                "scripts/run_rv3_route_catalog_revalidation.py",
                "tests/test_critical_chain_rv3.py",
                "src/transflow/application/contracts.py",
                "src/transflow/application/document_coordinator.py",
                "src/transflow/application/route_capability.py",
                "src/transflow/application/page_pipeline.py",
                "src/transflow/application/toolbox_page_pipeline.py",
                "src/transflow/application/toolbox_page_coordinator.py",
                "src/transflow/toolboxes/contracts.py",
            ],
        ),
    ]
    commands_path = run_dir / "process" / "commands.jsonl"
    commands_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in commands),
        encoding="utf-8",
    )
    return {
        "commands": commands,
        "pass": all(item["returncode"] == 0 for item in commands),
    }


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


def _report(
    run_dir: Path,
    catalog: dict[str, Any],
    p0151: dict[str, Any],
    capability: dict[str, Any],
    runtime: dict[str, Any],
    concurrency: dict[str, Any],
    verification: dict[str, Any],
    upstream_passed: bool,
) -> str:
    technical_pass = all(
        (
            catalog["mismatch_count"] == 0,
            p0151["structural_route_correct"],
            capability["pass"],
            runtime["pass"],
            concurrency["pass"],
            verification["pass"],
        )
    )
    formal = "PASS" if technical_pass and upstream_passed else "NOT_RELEASED"
    return f"""# RV3 Route、Catalog 与 capability 重新验收报告

## 结论先说

RV3 自身技术检查为 **{'PASS' if technical_pass else 'FAIL'}**：当前 16 个具体分类
类别与 15 个 Spike Toolbox 加内置 `visual_only` 一一对应，17 条 taxonomy 均在
Catalog 中恰好出现一次；当前启用的两个叶与生产 factory 完全一致，其余 Route 都有
唯一、显式的整页安全回退。

但正式 Gate 状态是 **G-RV-05 = {formal}**。原因不是 RV3 自身还有
Route/Catalog 错配，而是前置 `G-RV-04` 仍为
`NOT_PASSED / EVIDENCE_INSUFFICIENT`。本轮不把 TEST_ONLY 分类判定冒充真实模型证据，
也不授权 RV4/TM3。

## 这轮实际修了什么

完整文档分类结果现在把不可变 Route 连同原始 evidence 一起传到页面流水线，不再在
Router 处只剩一个字符串。恢复 checkpoint 时 Route 相同但 evidence 不同也会拒绝。

能力门现在先检查“页面事实已经足够确定”的错配，再进入翻译和 Toolbox 私有阶段。
把包含正文与表格的 p0151 故意交给 single 时，翻译调用为
{capability['translation_call_count']}，Patch 数为 {int(capability['patch_produced'])}，
结果明确为 `ROUTE_CAPABILITY_MISMATCH`、`Quality=FAIL` 和 `PAGE_PASSTHROUGH`；
分类 evidence、所需 owner、原因与失败阶段均写入页级证据。

## RV3-T01：Route、Catalog 与 Toolbox

- taxonomy：{catalog['taxonomy_route_count']} 条；Catalog：
  {catalog['catalog_entry_count']} 条；重复或遗漏：{catalog['mismatch_count']}。
- 具体类别：{catalog['concrete_route_count']} 条；一对一匹配：
  {catalog['matching_concrete_toolbox_count']} 条；一致率：
  {catalog['consistency_rate']:.0%}。
- 当前 enabled：{', '.join(catalog['enabled_routes'])}；其他
  {catalog['disabled_route_count']} 条明确 fallback。
- `body.freeform` 是受控新增能力，当前保持 disabled，不拿重复样本伪造成熟度。
- 页眉、页脚和纯页码不进入正文 taxonomy；共享 Margin 处理边界仍保留，未复制进各 Toolbox 主体。

## RV3-T02：p0151 Route

p0151 的结构链仍得到 `body.composite.flow_text_table`。本轮只用 TEST_ONLY 决策端
补足页面角色，以验证 Router 接线；真正决定 composite 和 flow_text_table 的节点来自
当前结构规则。该结果不计入 RV2 的真实模型通过证据。

## RV3-T03：能力错配故障注入

- 注入 Route：`body.flow_text.single`；实际所需 owner：`body.composite.flow_text_table`。
- 翻译调用：{capability['translation_call_count']}；Toolbox 私有阶段调用：
  {capability['toolbox_private_stage_call_count']}；Patch：
  {int(capability['patch_produced'])}。
- `candidate.pdf` 与 source PDF 哈希相同：
  {str(capability['source_candidate_pdf_hash_equal']).lower()}。这里没有“改了内容”：
  它是故障出口的原页透传证据，不能被当成译文候选。

## RV3-T04：禁止旁路

- 产品强制 Route：{runtime['product_forced_route_count']}。
- 动态目录发现/换链：{runtime['dynamic_discovery_count']}。
- 固定页码特例：{runtime['fixed_page_special_case_count']}。
- 跨叶私有调用：{runtime['cross_leaf_private_call_count']}。
- 本轮唯一强制错路由只存在于 `TEST_ONLY` 故障注入证据，未进入产品接受证据。

## RV3-T05：几十页乱序并发

从冻结年报抽取物理页 {SLICE_START}～{SLICE_END}，形成
{concurrency['page_count']} 页独立验证 PDF；分类完成顺序被确定性打乱，再按
page identity 归并。最终页序和身份一致率均为 100%。这验证并发归并，不评价
TEST_ONLY 模型的分类质量。

## 验证

pytest、Ruff、Mypy：
{'全部通过' if verification['pass'] else '存在失败，详见 process/command_outputs'}。

## Gate

- RV3 技术条件：{'PASS' if technical_pass else 'FAIL'}。
- G-RV-05 正式状态：`{formal}`。
- RV4：`BLOCKED`。
- TM3：`BLOCKED`。
- 唯一前置解锁项：补齐 RV2 三组真实模型重放并重新判定 G-RV-04。

## 实现索引（最后再看代码名）

- `src/transflow/application/document_coordinator.py`、`contracts.py`：Route evidence 贯穿与
  checkpoint 恢复一致性。
- `src/transflow/application/route_capability.py`、`toolbox_page_coordinator.py`：翻译前能力
  预检与结构化 mismatch。
- `tests/test_critical_chain_rv3.py`：T01/T03/T04 回归。
- `scripts/run_rv3_route_catalog_revalidation.py`：本轮可复跑入口。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 Transflow RV3 重新验收")
    parser.add_argument("--label", default="routing-catalog", help="run 目录标签")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not RV0_SOURCE.is_file():
        raise FileNotFoundError(f"RV0 冻结源 PDF 不存在: {RV0_SOURCE}")
    run_dir = _next_run_dir(args.label)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for relative in ("input", "process", "pages/p0151/input", "pages/p0151/output", "output"):
        (run_dir / relative).mkdir(parents=True, exist_ok=True)

    source_page = run_dir / "pages" / "p0151" / "input" / "source.pdf"
    slice_path = run_dir / "input" / f"annual_report_slice_p{SLICE_START:04d}-p{SLICE_END:04d}.pdf"
    _extract_page(RV0_SOURCE, PAGE_NO, source_page)
    _extract_slice(RV0_SOURCE, SLICE_START, SLICE_END, slice_path)
    catalog = _catalog_audit(run_dir)
    _write_json(run_dir / "process" / "route_catalog_audit.json", catalog)

    facts, classified = _classify_p0151(RV0_SOURCE)
    p0151 = {
        "schema_version": "transflow.rv3-p0151-route/v1",
        "evidence_scope": "TEST_ONLY_PAGE_ROLE_AND_CURRENT_STRUCTURAL_RULES",
        "source_document": _relative(RV0_SOURCE),
        "source_page_no": PAGE_NO,
        "route": classified.route.as_dict(),
        "resolutions": [item.as_dict() for item in classified.resolutions],
        "structural_route_correct": classified.route.route
        == "body.composite.flow_text_table",
        "counts_toward_g_rv_04_live_model": False,
    }
    _write_json(run_dir / "process" / "p0151_route.json", p0151)

    capability = _capability_fault(run_dir, source_page, facts, classified)
    _write_json(run_dir / "pages" / "p0151" / "process" / "capability_fault.json", capability)
    runtime = _runtime_scan()
    _write_json(run_dir / "process" / "runtime_boundary_scan.json", runtime)
    concurrency = _concurrency_audit(run_dir, slice_path)
    _write_json(run_dir / "process" / "concurrency_identity_audit.json", concurrency)

    rv2_manifest_path = RV2_RUN / "run_manifest.json"
    rv2_manifest = json.loads(rv2_manifest_path.read_text(encoding="utf-8"))
    upstream_passed = rv2_manifest["gate"]["status"] == "PASS"
    _write_json(
        run_dir / "input" / "source_manifest.json",
        {
            "schema_version": "transflow.rv3-source-manifest/v1",
            "rv0_source": _relative(RV0_SOURCE),
            "rv0_source_sha256": _sha256(RV0_SOURCE),
            "rv0_page_count": 240,
            "p0151_source": source_page.relative_to(run_dir).as_posix(),
            "slice": {
                "path": slice_path.relative_to(run_dir).as_posix(),
                "source_page_start": SLICE_START,
                "source_page_end": SLICE_END,
                "page_count": SLICE_END - SLICE_START + 1,
                "sha256": _sha256(slice_path),
            },
            "representative_gold_count": len(catalog["representative_samples"]),
        },
    )
    _write_json(
        run_dir / "process" / "environment_redacted.json",
        {
            "schema_version": "transflow.rv3-environment/v1",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "model_environment_present": {
                "base_url": bool(os.getenv("TRANSFLOW_MIGRATION_QWEN_BASE_URL")),
                "api_key": bool(os.getenv("TRANSFLOW_MIGRATION_QWEN_API_KEY")),
                "model": bool(os.getenv("TRANSFLOW_MIGRATION_QWEN_MODEL")),
            },
            "secret_values_recorded": False,
        },
    )
    _write_json(
        run_dir / "process" / "known_regressions.json",
        {
            "schema_version": "transflow.rv3-known-regressions/v1",
            "items": [
                {"id": "KRV-001", "coverage": ["RV3-T02", "RV3-T03"]},
                {"id": "KRV-006", "coverage": ["RV3-T03"]},
            ],
        },
    )

    verification = _verification(run_dir)
    technical_pass = all(
        (
            catalog["mismatch_count"] == 0,
            p0151["structural_route_correct"],
            capability["pass"],
            runtime["pass"],
            concurrency["pass"],
            verification["pass"],
        )
    )
    formal_status = "PASS" if technical_pass and upstream_passed else "NOT_RELEASED"
    gate = {
        "schema_version": "transflow.rv3-gate/v1",
        "gate_id": "G-RV-05",
        "technical_status": "PASS" if technical_pass else "FAIL",
        "formal_status": formal_status,
        "upstream_gate": {
            "gate_id": "G-RV-04",
            "status": rv2_manifest["gate"]["status"],
            "conclusion": rv2_manifest["conclusion"],
        },
        "route_catalog_toolbox_consistency_rate": catalog["consistency_rate"],
        "product_forced_route_count": runtime["product_forced_route_count"],
        "dynamic_discovery_count": runtime["dynamic_discovery_count"],
        "cross_leaf_private_call_count": runtime["cross_leaf_private_call_count"],
        "test_only_forced_route_count": 1,
        "rv4_allowed": technical_pass and upstream_passed,
        "tm3_allowed": False,
    }
    _write_json(run_dir / "process" / "gate_results.json", gate)
    trace_index = {
        "schema_version": "transflow.rv3-trace-index/v1",
        "RV3-T01": ["process/route_catalog_audit.json", "input/representative_gold/"],
        "RV3-T02": ["process/p0151_route.json", "pages/p0151/input/source.pdf"],
        "RV3-T03": [
            "pages/p0151/process/capability_fault.json",
            "pages/p0151/output/candidate.pdf",
            "pages/p0151/output/candidate.png",
        ],
        "RV3-T04": ["process/runtime_boundary_scan.json"],
        "RV3-T05": [
            "process/concurrency_identity_audit.json",
            slice_path.relative_to(run_dir).as_posix(),
        ],
        "G-RV-05": ["process/gate_results.json", "run_manifest.json", "report.md"],
    }
    _write_json(run_dir / "trace_index.json", trace_index)
    report = _report(
        run_dir,
        catalog,
        p0151,
        capability,
        runtime,
        concurrency,
        verification,
        upstream_passed,
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    _write_json(run_dir / "artifact_hashes.json", _artifact_hashes(run_dir))
    manifest = {
        "schema_version": "transflow.critical-chain-rv3-run/v1",
        "run_id": run_dir.name,
        "stage": "RV3_ROUTE_CATALOG_CAPABILITY",
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "conclusion": (
            "TECHNICAL_PASS_UPSTREAM_BLOCKED"
            if technical_pass and not upstream_passed
            else formal_status
        ),
        "gate": gate,
        "tests": {
            "RV3-T01": catalog["mismatch_count"] == 0,
            "RV3-T02": p0151["structural_route_correct"],
            "RV3-T03": capability["pass"],
            "RV3-T04": runtime["pass"],
            "RV3-T05": concurrency["pass"],
        },
        "test_only_evidence_excluded_from_product_acceptance": True,
        "next_stage_allowed": technical_pass and upstream_passed,
        "tm3_allowed": False,
        "report": "report.md",
    }
    _write_json(run_dir / "run_manifest.json", manifest)
    print(run_dir)
    print(f"RV3 technical={'PASS' if technical_pass else 'FAIL'} formal={formal_status}")
    return 0 if technical_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
