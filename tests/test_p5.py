"""验证 P5 页面分类迁移的匿名、控制流、接线和失败合同。"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from scripts.build_p5_baseline import (
    BASELINE_PATH,
    REPO_ROOT,
    baseline_content_hash,
    load_json,
    locate_authorized_pdf,
    stable_baseline_payload,
    verify_all,
)
from scripts.export_p5_showcase import export_showcase
from tests.test_p4 import make_request, make_runtime
from transflow.adapters.ai.fixed import DeterministicTranslationAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.classification.baseline import FrozenThresholdRegistry, ThresholdFreezeError
from transflow.classification.decision_adapter import (
    BoundedDecisionRunner,
    find_identity_leaks,
)
from transflow.classification.engine import ClassificationEngine
from transflow.classification.evidence import build_evidence, compact_evidence
from transflow.classification.rules import (
    decide_composite_kind,
    decide_layout_owner,
    estimate_text_columns,
)
from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageFactsExtractor

TESTS_ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_PATH = REPO_ROOT / "resources" / "manifests" / "p5_classification_thresholds.json"
RECEIPT_PATH = REPO_ROOT / "resources" / "manifests" / "p5_threshold_freeze_receipt.json"
ANSWER_KEY_PATH = TESTS_ROOT / "tests" / "migration" / "classification_answer_key.json"


def sha256_file(path: Path) -> str:
    """流式计算测试 PDF 的真实内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_pdf(path: Path, page_texts: tuple[str, ...]) -> Path:
    """生成包含真实 PDF 文字对象的多页分类输入。"""

    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(50, 70, 545, 780), text, fontsize=10)
    document.save(path)
    document.close()
    return path


def extract_single(path: Path) -> ExtractedPageFacts:
    """通过生产 Kernel 提取一个真实单页 PDF 的分类事实。"""

    extracted = PageFactsExtractor().extract_all(
        path,
        sha256_file(path),
        include_classification=True,
    )
    assert len(extracted) == 1
    return extracted[0]


def enumerated_pages(path: Path) -> tuple[EnumeratedPage, ...]:
    """由完整 PDF 请求生成含分类事实的稳定页面清单。"""

    source_hash = sha256_file(path)
    request = DocumentRunRequest(
        source_pdf_path=str(path),
        source_hash=source_hash,
        config_snapshot_hash="a" * 64,
        job_id="job-p5",
        run_id="run-p5",
        source_language="zh-CN",
        target_language="en",
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        request,
        include_classification=True,
    )


class ScriptedDecisionPort:
    """按匿名结构文字返回 fake 判定，仅用于 wiring 和控制流测试。"""

    def __init__(
        self,
        scripted: dict[tuple[str, str], str] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        """保存节点脚本和按文字标记注入的乱序延迟。"""

        self.scripted = scripted or {}
        self.delays = delays or {}
        self.calls: list[ModelDecisionRequest] = []
        self._lock = threading.Lock()

    @staticmethod
    def _page_text(request: ModelDecisionRequest) -> str:
        """从匿名 typed evidence 合并文字块，不读取路径或测试答案。"""

        return "\n".join(str(item.get("text", "")) for item in request.typed_evidence["blocks"])

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """返回 allow-list 内判定，并记录真实调用次数和载荷。"""

        text = self._page_text(request)
        for marker, delay in self.delays.items():
            if marker in text:
                time.sleep(delay)
        with self._lock:
            self.calls.append(request)
        node_key = str(request.node_spec["node_key"])
        stage = str(request.node_spec["stage"])
        selected = self.scripted.get((node_key, stage))
        if selected is None:
            if node_key == "page.role":
                selected = "visual_only" if "VISUAL" in text else "body"
            elif node_key == "body.layout_owner":
                selected = "flow_text"
            elif node_key == "body.flow.topology":
                selected = "multi" if "MULTI" in text else "single"
            else:
                selected = "flow_text_table"
        return ModelDecision(
            decision_id=request.decision_id,
            decision_kind=request.decision_kind,
            result_code=selected,
            evidence_ids=("TEXT1",),
            confidence=0.92,
            reason_summary="fake 仅验证接线与有界控制流",
        )


class FailingDecisionPort:
    """注入 Port 超时或越界响应，验证每类失败都有确定出口。"""

    def __init__(self, mode: str) -> None:
        """保存故障类型并初始化调用计数。"""

        self.mode = mode
        self.call_count = 0

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """按配置抛出真实异常或返回 Schema/allow-list 外结果。"""

        self.call_count += 1
        if self.mode == "timeout":
            raise PortCallError(ErrorCode.AI_TIMEOUT, True, "故障注入超时")
        if self.mode == "invalid_action":
            return ModelDecision(
                request.decision_id,
                request.decision_kind,
                "invented_route",
                request.evidence_ids,
            )
        if self.mode == "invalid_evidence":
            return ModelDecision(
                request.decision_id,
                request.decision_kind,
                "body",
                ("UNKNOWN",),
            )
        raise ValueError("故障注入非法 JSON")


@pytest.mark.migration
def test_p5_1_t01_model_payload_and_fixture_have_zero_identity_leaks(tmp_path: Path) -> None:
    """P5.1-T01：匿名 fixture 与真实模型载荷不含路径、样本身份和答案。"""

    baseline = load_json(BASELINE_PATH)
    source = create_pdf(tmp_path / "renamed-input.pdf", ("正文 " * 500,))
    payload = compact_evidence(build_evidence(extract_single(source), 1))
    assert find_identity_leaks(baseline) == ()
    assert find_identity_leaks(payload) == ()
    assert "renamed-input" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.contract
def test_p5_1_t01a_web_url_is_not_mistaken_for_windows_host_path() -> None:
    """页面正文中的网页 URL 不是宿主路径，但真实盘符路径仍必须被阻断。"""

    assert find_identity_leaks({"text": "website: https://example.com/fund"}) == ()
    assert find_identity_leaks({"text": "host file C:/workspace/sample.pdf"}) == (
        "$.text",
    )


@pytest.mark.migration
def test_p5_1_t02_anonymous_hashes_and_strata_regenerate_stably() -> None:
    """P5.1-T02：重复重建得到相同 case 哈希、分层和真实文件命中。"""

    baseline = load_json(BASELINE_PATH)
    first = stable_baseline_payload(baseline)
    second = stable_baseline_payload(copy.deepcopy(baseline))
    first_summary = verify_all()
    second_summary = verify_all()
    assert first == second
    assert baseline_content_hash(first) == baseline_content_hash(second)
    assert first_summary == second_summary
    assert first_summary["located_real_pdf_count"] == 22
    assert first_summary["stratum_count"] == 11


@pytest.mark.migration
def test_p5_1_t03_threshold_change_after_freeze_is_blocked() -> None:
    """P5.1-T03：冻结后按结果降低阈值会被 Gate 阻断。"""

    registry = FrozenThresholdRegistry.load(THRESHOLD_PATH, RECEIPT_PATH)
    candidate = copy.deepcopy(registry.payload)
    candidate["leaf_thresholds"]["cover"]["recall_min"] = 0.0
    with pytest.raises(ThresholdFreezeError, match="决策"):
        registry.require_unchanged(candidate)


@pytest.mark.migration
def test_p5_2_t01_rule_and_evidence_match_legacy_except_declared_identity_fields(
    tmp_path: Path,
) -> None:
    """P5.2-T01：匿名真实 PDF 的规则事实与旧实现仅有声明的身份差异。"""

    baseline = load_json(BASELINE_PATH)
    first_case = baseline["cases"][0]
    source = locate_authorized_pdf(str(first_case["content_sha256"]))
    production = build_evidence(extract_single(source), 1)
    source_root = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "src"
    sys.path.insert(0, str(source_root))
    try:
        legacy_evidence = importlib.import_module("page_classifier.evidence")
        legacy_rules = importlib.import_module("page_classifier.rules")
        legacy = legacy_evidence.build_evidence(
            source,
            {
                "sample_id": "opaque-case",
                "source_page_count": None,
                "source_page_number": None,
            },
            tmp_path / "legacy.png",
        )
    finally:
        sys.path.remove(str(source_root))
    compared_keys = ("blocks", "borderless_table", "drawings", "images", "tables", "text")
    assert {key: production[key] for key in compared_keys} == {
        key: legacy[key] for key in compared_keys
    }
    assert (
        decide_layout_owner(production).as_dict()
        == legacy_rules.decide_layout_owner(production).as_dict()
    )


@pytest.mark.migration
def test_p5_2_t02_threshold_boundary_changes_with_evidence_not_identity() -> None:
    """P5.2-T02：表格面积跨冻结阈值时分支变化，附加身份字段不参与。"""

    base: dict[str, Any] = {
        "blocks": [],
        "borderless_table": {"confidence": 0.0},
        "images": {"area_ratio": 0.0},
        "tables": {
            "area_ratio": 0.49,
            "count": 1,
            "details": [
                {
                    "cell_count": 24,
                    "column_count": 4,
                    "grid_coverage": 1.0,
                    "row_count": 6,
                    "text_object_count": 20,
                }
            ],
        },
        "text": {
            "block_count": 8,
            "native_char_count": 500,
            "outside_table_chars": 100,
            "text_area_ratio": 0.5,
        },
    }
    below = decide_layout_owner(base)
    above_payload = copy.deepcopy(base)
    above_payload["tables"]["area_ratio"] = 0.51
    above = decide_layout_owner(above_payload)
    assert below.status == "INCONCLUSIVE"
    assert above.selected_child == "table"
    assert find_identity_leaks(base) == ()


@pytest.mark.migration
def test_p5_2_t02a_small_semantic_table_with_substantial_prose_is_composite() -> None:
    """A small detected table remains a table owner when prose is also substantial."""

    evidence: dict[str, Any] = {
        "blocks": [],
        "borderless_table": {"confidence": 0.0},
        "images": {"area_ratio": 0.0},
        "tables": {
            "area_ratio": 0.026,
            "count": 1,
            "details": [
                {
                    "cell_count": 7,
                    "column_count": 2,
                    "grid_coverage": 0.875,
                    "row_count": 4,
                    "text_object_count": 9,
                }
            ],
        },
        "text": {
            "block_count": 18,
            "native_char_count": 3800,
            "outside_table_chars": 3580,
            "text_area_ratio": 0.46,
        },
    }

    owner = decide_layout_owner(evidence)
    kind = decide_composite_kind(evidence)

    assert owner.status == "DECIDED"
    assert owner.selected_child == "composite"
    assert owner.confidence >= 0.9
    assert kind.status == "DECIDED"
    assert kind.selected_child == "flow_text_table"
    assert kind.confidence >= 0.9


@pytest.mark.migration
def test_p5_2_t03_image_text_is_classification_only_and_never_translation_input(
    tmp_path: Path,
) -> None:
    """P5.2-T03：页面图片进入分类证据，但不会形成翻译单元。"""

    source = tmp_path / "image-text.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pixmap.clear_with(180)
    page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=pixmap.tobytes("png"))
    document.save(source)
    document.close()
    evidence = build_evidence(extract_single(source), 1)
    assert evidence["images"]["classification_only"] is True
    assert evidence["page_image"]["bytes"]
    assert all("translation" not in key.lower() for key in evidence)


@pytest.mark.contract
def test_p5_3_t01_direct_table_rule_skips_layout_model_call(tmp_path: Path) -> None:
    """P5.3-T01：直接表格证据达到 0.90 时按原合同跳过布局模型。"""

    source = tmp_path / "table.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    for row in range(24):
        page.insert_text((55, 80 + row * 25), f"Item {row:02d}")
        page.insert_text((330, 80 + row * 25), f"{row * 100}")
    document.save(source)
    document.close()
    port = ScriptedDecisionPort()
    result = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(
        extract_single(source), 1
    )
    layout_calls = [
        call for call in port.calls if call.node_spec["node_key"] == "body.layout_owner"
    ]
    assert result.route.route in {"body.table", "body.flow_text.single"}
    if result.route.route == "body.table":
        assert layout_calls == []


@pytest.mark.contract
def test_p5_3_t02_conflict_has_one_review_then_resolver(tmp_path: Path) -> None:
    """P5.3-T02：规则与主判冲突时仅执行一次复核并采用 Resolver 结果。"""

    source = create_pdf(tmp_path / "conflict.pdf", ("CONFLICT " + "正文 " * 500,))
    port = ScriptedDecisionPort(
        {
            ("page.role", "PRIMARY"): "body",
            ("body.layout_owner", "PRIMARY"): "chart",
            ("body.layout_owner", "REVIEW"): "diagram",
        }
    )
    result = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(
        extract_single(source), 1
    )
    layout_calls = [
        call for call in port.calls if call.node_spec["node_key"] == "body.layout_owner"
    ]
    assert result.route.route == "body.diagram"
    assert [call.node_spec["stage"] for call in layout_calls] == ["PRIMARY", "REVIEW"]
    assert result.resolutions[-1].resolution == "REVIEW_DECIDED"


@pytest.mark.parametrize("mode", ["timeout", "invalid_action", "invalid_evidence", "invalid_json"])
@pytest.mark.fault_injection
def test_p5_3_t03_model_failures_have_deterministic_unclassified_route(
    tmp_path: Path,
    mode: str,
) -> None:
    """P5.3-T03：主判和复核各类失败都收敛到 failed page.role。"""

    source = create_pdf(tmp_path / f"failure-{mode}.pdf", ("正文 " * 500,))
    port = FailingDecisionPort(mode)
    result = ClassificationEngine(BoundedDecisionRunner(port)).classify_page(
        extract_single(source), 1
    )
    assert result.route.route == "unclassified"
    assert result.route.complete_to_leaf is False
    assert result.route.failed_node == "page.role"
    assert port.call_count == 2


@pytest.mark.contract
def test_p5_3_t04_production_wheel_has_no_direct_model_provider(tmp_path: Path) -> None:
    """P5.3-T04：真实构建 wheel 的分类包不含直连 Provider、端点或密钥入口。"""

    output = tmp_path / "wheel"
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheel = next(output.glob("*.whl"))
    forbidden = ("qwen", "/chat/completions", "api_key", "page_classifier_qwen")
    hits: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if "/classification/" not in name or not name.endswith(".py"):
                continue
            text = archive.read(name).decode("utf-8").lower()
            hits.extend(f"{name}:{token}" for token in forbidden if token in text)
    assert hits == []


@pytest.mark.integration
def test_p5_4_t01_mixed_pdf_has_one_route_per_page_and_stable_identity(tmp_path: Path) -> None:
    """P5.4-T01：混合完整 PDF 每页恰有一个 Route，分类前后身份不变。"""

    source = create_pdf(
        tmp_path / "mixed.pdf",
        ("FIRST " + "正文 " * 500, "SECOND MULTI " + "正文 " * 500, "VISUAL"),
    )
    pages = enumerated_pages(source)
    port = ScriptedDecisionPort()
    classified = DocumentCoordinator(PageFactsExtractor()).classify_pages(
        pages,
        ClassificationEngine(BoundedDecisionRunner(port)),
        page_concurrency=2,
    )
    assert len(classified) == len(pages) == 3
    assert [item.page_identity for item in classified] == [
        item.facts.page_identity for item in pages
    ]
    assert all(item.route.route for item in classified)


@pytest.mark.integration
def test_p5_4_t01_classified_scan_releases_page_images_after_each_page(
    tmp_path: Path,
) -> None:
    """流式扫描不得整本保留分类 PNG，后续阶段只接收轻量页面事实。"""

    class StreamingOnlyExtractor(PageFactsExtractor):
        def extract_all(self, *args: Any, **kwargs: Any) -> tuple[ExtractedPageFacts, ...]:
            raise AssertionError("流式分类不得调用 extract_all")

    source = create_pdf(
        tmp_path / "streaming-classification.pdf",
        tuple(f"PAGE{i} " + "正文 " * 300 for i in range(1, 13)),
    )
    request = make_request(source, "run-p5-streaming")
    port = ScriptedDecisionPort()
    pages, classified = DocumentCoordinator(StreamingOnlyExtractor()).scan_classified_pages(
        request,
        ClassificationEngine(BoundedDecisionRunner(port)),
    )

    assert [item.context.page_no for item in pages] == list(range(1, 13))
    assert [item.page_no for item in classified] == list(range(1, 13))
    assert all(item.facts.classification is None for item in pages)
    assert port.calls
    assert all(
        str(call.typed_evidence["page_image"]["data_url"]).startswith("data:image/png;base64,")
        for call in port.calls
    )


@pytest.mark.integration
def test_p5_4_t01_run_classified_finalizes_one_complete_pdf(tmp_path: Path) -> None:
    """P5.4-T01：真实分类接线驱动 P4 页面流水线并最终化一份完整 PDF。"""

    source = create_pdf(
        tmp_path / "classified-run.pdf",
        ("PAGE ONE " + "正文 " * 300, "VISUAL"),
    )
    request = make_request(source, "run-p5-classified")
    runtime = make_runtime(tmp_path, request, DeterministicTranslationAdapter())
    execution = runtime.coordinator.run_classified(
        request,
        ClassificationEngine(BoundedDecisionRunner(ScriptedDecisionPort())),
        2,
        runtime.pipeline,
        runtime.finalizer,
    )
    assert len(execution.pages) == 2
    assert all(page.classification_route is not None for page in execution.pages)
    assert all(
        page.classification_route is not None
        and page.classification_route.route == page.route
        and page.classification_route.evidence_ids
        for page in execution.pages
    )
    assert execution.final_artifact is not None
    assert runtime.artifacts.get(execution.final_artifact.artifact_id).startswith(b"%PDF")


@pytest.mark.integration
def test_p5_4_t02_out_of_order_model_responses_merge_by_page_no(tmp_path: Path) -> None:
    """P5.4-T02：模型响应乱序完成后仍按原 page_no 归并。"""

    source = create_pdf(
        tmp_path / "out-of-order.pdf",
        tuple(f"PAGE{i} " + "正文 " * 500 for i in range(1, 4)),
    )
    pages = enumerated_pages(source)
    port = ScriptedDecisionPort(delays={"PAGE1": 0.06, "PAGE2": 0.03, "PAGE3": 0.0})
    classified = DocumentCoordinator(PageFactsExtractor()).classify_pages(
        pages,
        ClassificationEngine(BoundedDecisionRunner(port)),
        page_concurrency=3,
    )
    assert [item.page_no for item in classified] == [1, 2, 3]
    assert [item.page_identity for item in classified] == [
        item.facts.page_identity for item in pages
    ]


@pytest.mark.integration
def test_p5_4_t03_failed_page_role_is_unclassified_not_freeform(tmp_path: Path) -> None:
    """P5.4-T03：未知或失败的 page.role 进入 Unclassified，不伪造 freeform。"""

    source = create_pdf(tmp_path / "unknown.pdf", ("UNKNOWN",))
    result = ClassificationEngine(
        BoundedDecisionRunner(FailingDecisionPort("timeout"))
    ).classify_page(
        extract_single(source),
        1,
    )
    assert result.route.route == "unclassified"
    assert result.route.failed_node == "page.role"
    assert result.route.taxonomy_fallback is False


@pytest.mark.regression
def test_p5_5_t01_filename_and_body_page_order_do_not_change_route(tmp_path: Path) -> None:
    """P5.5-T01：文件名和正文页顺序变化不改变同一结构的 Route。"""

    first = create_pdf(tmp_path / "alpha.pdf", ("SAME " + "正文 " * 500,))
    second = tmp_path / "renamed.pdf"
    second.write_bytes(first.read_bytes())
    port_a = ScriptedDecisionPort()
    port_b = ScriptedDecisionPort()
    route_a = (
        ClassificationEngine(BoundedDecisionRunner(port_a))
        .classify_page(
            extract_single(first),
            1,
        )
        .route.route
    )
    reordered = extract_single(second)
    reordered = replace(
        reordered,
        page=replace(reordered.page, page_no=5),
        page_identity="b" * 64,
    )
    route_b = (
        ClassificationEngine(BoundedDecisionRunner(port_b))
        .classify_page(
            reordered,
            9,
        )
        .route.route
    )
    assert route_a == route_b == "body.flow_text.single"


@pytest.mark.regression
def test_p5_5_t02_scale_and_text_replacement_follow_structure_not_fixed_coordinates(
    tmp_path: Path,
) -> None:
    """P5.5-T02：同比缩放和等长文本替换保持栏道判断，无固定坐标特例。"""

    source = create_pdf(tmp_path / "columns.pdf", ("LEFT " + "正文 " * 500,))
    evidence = build_evidence(extract_single(source), 1)
    scaled = copy.deepcopy(evidence)
    scaled["page"]["width"] *= 1.5
    scaled["page"]["height"] *= 1.5
    for block in scaled["blocks"]:
        block["bbox"] = [float(value) * 1.5 for value in block["bbox"]]
        block["text"] = "替" * len(str(block["text"]))
    assert estimate_text_columns(evidence) == estimate_text_columns(scaled)
    assert (
        decide_layout_owner(evidence).selected_child == decide_layout_owner(scaled).selected_child
    )


@pytest.mark.regression
def test_p5_5_t03_blind_cases_are_frozen_and_counted_without_gold_repair() -> None:
    """P5.5-T03：未知盲样本已在结果前冻结，且答案哈希不允许人工修补。"""

    answers = load_json(ANSWER_KEY_PATH)["answers"]
    receipt = load_json(RECEIPT_PATH)
    blind = [item for item in answers if item["evaluation_role"] == "blind"]
    assert len(blind) == 11
    assert receipt["migration_result_count_at_freeze"] == 0
    assert verify_all()["post_freeze_change_count"] == 0


@pytest.mark.regression
def test_p5_5_t03_composite_prompts_freeze_card_chart_and_table_boundaries() -> None:
    """P5.5-T03：复合节点 Prompt 必须冻结卡片、图表与候选表格的通用边界。"""

    prompt_root = REPO_ROOT / "resources" / "prompts" / "classification"
    prompt_text = "\n".join(
        (
            (prompt_root / "body_composite_kind" / "decide.zh-CN.md").read_text(
                encoding="utf-8"
            ),
            (prompt_root / "body_composite_kind" / "review.zh-CN.md").read_text(
                encoding="utf-8"
            ),
        )
    )
    required_boundaries = (
        "卡片内的说明文字归属于 anchored_blocks",
        "表格检测框只是候选几何证据",
        "`selected_child` 必须与 reason_summary 中识别出的两类主体一致",
    )
    assert all(boundary in prompt_text for boundary in required_boundaries)


@pytest.mark.fault_injection
def test_p5_5_t04_every_model_failure_has_a_defined_route(tmp_path: Path) -> None:
    """P5.5-T04：各模型失败模式均返回定义 Route，不产生无路由状态。"""

    source = create_pdf(tmp_path / "failures.pdf", ("正文 " * 500,))
    routes = [
        ClassificationEngine(BoundedDecisionRunner(FailingDecisionPort(mode)))
        .classify_page(extract_single(source), 1)
        .route.route
        for mode in ("timeout", "invalid_action", "invalid_evidence", "invalid_json")
    ]
    assert routes == ["unclassified"] * 4


@pytest.mark.e2e
def test_p5_showcase_t01_exports_readable_chinese_pdf_and_gate_results(tmp_path: Path) -> None:
    """P5 展示验收：持久目录结构、中文 PDF 和真实 Gate 快照必须同时可读。"""

    output_root = tmp_path / "p5-showcase"
    manifest = export_showcase(output_root)
    final_path = output_root / str(manifest["final_pdf"])
    with pymupdf.open(final_path) as document:
        extracted_text = "\n".join(page.get_text() for page in document)
    normalized_text = unicodedata.normalize("NFKC", extracted_text).replace("\u00a0", " ")
    assert manifest["classification_route"] == "body.flow_text.single"
    assert manifest["question_mark_replacement_count"] == 0
    assert "P5 页面分类接线演示" in normalized_text
    assert (output_root / "final" / "p5_classification_wiring_demo.png").is_file()
    assert (output_root / "test-results" / "G5_evidence.json").is_file()
    assert (output_root / "test-results" / "P5_classification_metrics.json").is_file()


def main() -> int:
    """输出 P5 测试模块包含的具名验收用例数量。"""

    count = sum(
        1 for name, value in globals().items() if name.startswith("test_p5_") and callable(value)
    )
    print(f"P5_TEST_CASES count={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
