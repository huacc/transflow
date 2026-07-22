"""RV3 Route、Catalog 与 capability 当前链路回归。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from transflow.application.route_capability import RouteCapabilityEvidence
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.classification import ClassificationRoute
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import Fallback, Quality, TranslationCoverage
from transflow.domain.toolbox import ToolboxDescriptor
from transflow.domain.translation import TranslationBatch, TranslationBundle
from transflow.pdf_kernel.facts import KernelTableFact, PageFactsExtractor
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.contracts import TOOLBOX_CONTRACT_VERSION, PageToolbox
from transflow.toolboxes.leaves import build_p9_toolbox_factories

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json"
CATALOG = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
GOLD_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
SPIKE_TOOLBOX_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "toolboxes"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
RV2_FRESH_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV2"
    / "05-fresh-blind-20260722-094154"
)
RV2_BOUNDARY_CASE_IDS = {
    "fresh-005",
    "fresh-016",
    "fresh-018",
    "fresh-027",
    "fresh-028",
    "fresh-029",
}


def _gold_routes() -> set[str]:
    """从真实单页金标目录推导具体类别，不读取文件名标签。"""

    routes: set[str] = set()
    for path in GOLD_ROOT.rglob("*.pdf"):
        routes.add(path.parent.relative_to(GOLD_ROOT).as_posix().replace("/", "."))
    return routes


def _spike_toolbox_routes(gold_routes: set[str]) -> set[str]:
    """核对每个非 visual_only 金标类别都有对应 Spike Toolbox 目录。"""

    routes = {"visual_only"}
    for route in gold_routes - {"visual_only"}:
        path = SPIKE_TOOLBOX_ROOT.joinpath(*route.split("."))
        if path.is_dir():
            routes.add(route)
    return routes


@pytest.mark.contract
def test_rv3_t01_taxonomy_catalog_and_concrete_toolboxes_are_one_to_one() -> None:
    """RV3-T01：具体类别一对一，freeform 只有唯一 disabled fallback。"""

    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    catalog_payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    taxonomy_routes = [str(item["route"]) for item in taxonomy["routes"]]
    catalog_entries = catalog_payload["entries"]
    catalog_routes = [str(item["route"]) for item in catalog_entries]
    gold_routes = _gold_routes()
    hash_routes: dict[str, set[str]] = {}
    for path in GOLD_ROOT.rglob("*.pdf"):
        route = path.parent.relative_to(GOLD_ROOT).as_posix().replace("/", ".")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        hash_routes.setdefault(content_hash, set()).add(route)
    non_conflicting_routes = {
        next(iter(routes)) for routes in hash_routes.values() if len(routes) == 1
    }

    assert len(taxonomy_routes) == len(set(taxonomy_routes)) == 17
    assert catalog_routes == taxonomy_routes
    assert gold_routes == set(taxonomy_routes) - {"body.freeform"}
    assert non_conflicting_routes == gold_routes
    assert _spike_toolbox_routes(gold_routes) == gold_routes
    assert all(
        item["toolbox_key"] == item["route"] and item["fallback"] == "PAGE_PASSTHROUGH"
        for item in catalog_entries
    )
    freeform = next(item for item in catalog_entries if item["route"] == "body.freeform")
    assert freeform["enabled"] is False and freeform["disabled_reason"]

    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG, factories)
    startup = catalog.validate_startup()
    assert startup.ready and startup.violations == ()
    assert {item.route for item in catalog.entries if item.enabled} == set(factories)


class _NoTranslation:
    """能力预检若正确，翻译端口永远不会被调用。"""

    def __init__(self) -> None:
        self.call_count = 0

    def translate(self, _batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        raise AssertionError("Route capability mismatch 不得进入翻译")


class _UnreachableSingleToolbox:
    """能力预检若正确，Toolbox 私有阶段永远不会被调用。"""

    descriptor = ToolboxDescriptor(
        "body.flow_text.single",
        "body.flow_text.single",
        TOOLBOX_CONTRACT_VERSION,
        "body.flow_text.single",
    )

    def __getattr__(self, _name: str) -> object:
        raise AssertionError("Route capability mismatch 不得调用 Toolbox 私有阶段")


@pytest.mark.fault_injection
def test_rv3_t03_composite_facts_reach_single_guard_before_translation() -> None:
    """RV3-T03：错投 single 时保留分类证据并在翻译前产品失败。"""

    source = next((GOLD_ROOT / "body" / "flow_text" / "single").glob("*.pdf"))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    facts = PageFactsExtractor().extract_page(
        source,
        source_hash,
        1,
        include_classification=True,
    )
    table = KernelTableFact(
        "rv3-test-table",
        (50.0, 100.0, 250.0, 180.0),
        ((50.0, 100.0, 150.0, 140.0), (150.0, 100.0, 250.0, 140.0)),
        (),
    )
    mismatched_facts = replace(facts, table_objects=(table,))
    context = PageExecutionContext(
        "rv3-test-job",
        "rv3-test-run",
        source_hash,
        1,
        facts.page.geometry_hash,
        "a" * 64,
    )
    classification_route = ClassificationRoute(
        "body.flow_text.single",
        0.75,
        ("classification-evidence-rv3",),
    )
    capability_evidence = RouteCapabilityEvidence(
        "structure-evidence-rv3",
        "body.composite.flow_text_table",
        "flow_text_and_table_require_composite_owner",
        "TEST_ONLY_FAULT_INJECTION",
    )
    translation = _NoTranslation()
    result = ToolboxPageCoordinator(translation).execute(
        ToolboxPageWork(
            context,
            mismatched_facts,
            cast(PageToolbox, _UnreachableSingleToolbox()),
            capability_evidence,
            classification_route,
        )
    )

    assert translation.call_count == 0 and result.patch is None
    assert result.trace.stages == ("route_capability", "outcome")
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.outcome.translation_coverage is TranslationCoverage.NONE
    assert result.outcome.quality is Quality.FAIL
    mismatch = result.route_capability_mismatch
    assert mismatch is not None
    assert mismatch["selected_route"] == "body.flow_text.single"
    assert mismatch["required_owner"] == "body.composite.flow_text_table"
    assert mismatch["failure_stage"] == "ROUTE_CAPABILITY_PREFLIGHT"
    assert mismatch["route_evidence_ids"] == ["classification-evidence-rv3"]


@pytest.mark.contract
def test_rv3_t04_production_has_no_route_injection_or_dynamic_leaf_discovery() -> None:
    """RV3-T04：生产路由目录无样本特例、强制 Route、动态发现或跨叶私调。"""

    roots = (
        REPO_ROOT / "src" / "transflow" / "classification",
        REPO_ROOT / "src" / "transflow" / "toolboxes",
        REPO_ROOT / "src" / "transflow" / "application",
    )
    forbidden = re.compile(
        r"\b(?:forced_route|target_route|route_override|override_route|entry_points|"
        r"iter_modules|import_module|__import__)\b|\.rglob\(|\.glob\("
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for match in forbidden.finditer(text):
                violations.append(f"{relative}:{match.group(0)}")
            for page_no in re.findall(r"page_no\s*==\s*(\d+)", text):
                if int(page_no) != 1:
                    violations.append(f"{relative}:page_no=={page_no}")
    assert violations == []


@pytest.mark.contract
def test_rv3_t06_rv2_boundary_mismatches_are_compatible_or_disabled() -> None:
    """RV3-T06：六个精确标签差异只能能力兼容或在 Catalog 层安全拒绝。"""

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
        if item["case_id"] in RV2_BOUNDARY_CASE_IDS
    }
    predictions = {
        str(item["case_id"]): item
        for item in score["results"]
        if item["case_id"] in RV2_BOUNDARY_CASE_IDS
    }
    assert set(answers) == set(predictions) == RV2_BOUNDARY_CASE_IDS

    factories = build_p9_toolbox_factories(P8_POLICY, P9_POLICY, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG, factories)
    compatible_count = 0
    disabled_fallback_count = 0
    for case_id in sorted(RV2_BOUNDARY_CASE_IDS):
        answer = answers[case_id]
        prediction = predictions[case_id]
        source = REPO_ROOT / str(answer["source_path"])
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        assert source_hash == answer["source_content_sha256"]
        assert prediction["expected_route"] == answer["expected_route"]
        selected_route = str(prediction["predicted_route"])
        resolution = catalog.resolve_enabled(selected_route, 1)

        if selected_route == "visual_only":
            assert case_id == "fresh-018" and resolution.toolbox is not None
            facts = PageFactsExtractor().extract_page(
                source,
                source_hash,
                1,
                include_classification=True,
            )
            inventory = freeze_page_text_inventory(facts)
            assert facts.text_spans == () and inventory.items == ()
            context = PageExecutionContext(
                "rv3-boundary-job",
                "rv3-boundary-test",
                source_hash,
                1,
                facts.page.geometry_hash,
                "6" * 64,
            )
            translation = _NoTranslation()
            result = ToolboxPageCoordinator(translation).execute(
                ToolboxPageWork(context, facts, resolution.toolbox)
            )
            assert translation.call_count == 0 and result.patch is None
            assert result.outcome.translation_coverage is TranslationCoverage.NONE
            assert result.outcome.quality is Quality.PASS
            assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
            compatible_count += 1
            continue

        assert resolution.toolbox is None and resolution.finding is not None
        assert resolution.finding.code == "TOOLBOX_DISABLED"
        assert resolution.outcome is not None
        assert resolution.outcome.translation_coverage is TranslationCoverage.NONE
        assert resolution.outcome.quality is Quality.FAIL
        assert resolution.outcome.fallback is Fallback.PAGE_PASSTHROUGH
        disabled_fallback_count += 1

    assert compatible_count == 1
    assert disabled_fallback_count == 5
