"""Transflow P0.1 至 P0.5 的真实仓库数据验收测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import transflow
from scripts import build_p0_assets as assets
from scripts import run_gate, verify_p0

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_json(relative_path: str) -> dict[str, Any]:
    """从仓库根读取测试所需的真实 JSON 文件。"""

    return json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def built_wheel() -> Path:
    """使用当前解释器和真实构建后端生成 P0 初始 wheel。"""

    dist_dir = REPO_ROOT / "tmp" / "p0-tests" / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    for existing_wheel in dist_dir.glob("*.whl"):
        existing_wheel.unlink()
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_p0_1_t01_migration_units_are_complete_and_unique() -> None:
    """P0.1-T01：真实迁移台账覆盖分类、Kernel、合同和全部路由行为叶。"""

    ledger = load_json("docs/迁移/migration_ledger.json")
    units = list(ledger["units"])
    unit_ids = [str(unit["unit_id"]) for unit in units]
    assert len(unit_ids) == len(set(unit_ids))
    categories: dict[str, list[dict[str, object]]] = {}
    for unit in units:
        categories.setdefault(str(unit["category"]), []).append(unit)
    classification_sources = list(
        (assets.CLASSIFICATION_ROOT / "src" / "page_classifier").glob("*.py")
    )
    kernel_sources = list((assets.TOOLBOX_ROOT / "src" / "shared_pdf_kernel").glob("*.py"))
    assert len(categories["classification_source"]) == len(classification_sources)
    assert len(categories["pdf_kernel_source"]) == len(kernel_sources)
    leaf_keys = {
        str(unit["unit_id"]).removeprefix("toolbox.leaf.")
        for unit in categories["toolbox_leaf"]
    }
    assert leaf_keys == set(assets.EXPECTED_ROUTE_BEHAVIORS)
    for unit in units:
        assert set(ledger["required_fields"]).issubset(unit)


def test_p0_1_t02_hashes_are_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0.1-T02：未修改来源时连续两次重算得到完全相同结果。"""

    first_baseline = assets.collect_baseline_manifest()
    second_baseline = assets.collect_baseline_manifest()
    first_ledger = assets.collect_migration_ledger()
    second_ledger = assets.collect_migration_ledger()
    assert first_baseline == second_baseline
    assert first_ledger == second_ledger
    assets.write_assets()
    assert not assets.check_assets()
    monkeypatch.setattr(sys, "argv", ["build_p0_assets.py", "--check"])
    assert assets.main() == 0


def test_p0_1_t03_maturity_is_not_promoted() -> None:
    """P0.1-T03：已知成熟度和无根级 Gate 资产不被乐观提升。"""

    ledger = load_json("docs/迁移/migration_ledger.json")
    status_by_key = {
        str(unit["unit_id"]).removeprefix("toolbox.leaf."): unit["evidence_status"]
        for unit in ledger["units"]
        if unit["category"] == "toolbox_leaf"
    }
    assert status_by_key["body.flow_text.single"] == "PASS"
    assert status_by_key["body.chart"] == "PASS_NON_BLIND"
    assert status_by_key["body.diagram"] == "PASS_NON_BLIND"
    assert status_by_key["body.flow_text.multi"] == "NOT_EVALUATED"
    assert status_by_key["body.table"] == "NOT_EVALUATED"
    assert status_by_key["body.flow_text.visual_anchored"] == "FAIL"
    assert status_by_key["body.composite.anchored_blocks_chart"] == "FAIL"
    assert status_by_key["body.composite.flow_text_table"] == "NO_ROOT_GATE"
    assert status_by_key["body.freeform"] == "NO_ROOT_GATE"


def test_p0_2_t01_import_resolves_only_from_src() -> None:
    """P0.2-T01：安装后 transflow 只能从 src/transflow 解析。"""

    package_path = Path(transflow.__file__).resolve()
    expected_root = (REPO_ROOT / "src" / "transflow").resolve()
    assert expected_root in package_path.parents


def test_p0_2_t02_production_import_graph_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0.2-T02：真实生产源码 AST 不含 spike 或 MerqFin 导入边。"""

    assert verify_p0.check_package_boundary() == []
    assert verify_p0.is_within_repo(REPO_ROOT / "tmp")
    assert not verify_p0.is_within_repo(REPO_ROOT.parent)

    bad_source = REPO_ROOT / "tmp" / "p0-tests" / "invalid-production-source.py"
    bad_source.parent.mkdir(parents=True, exist_ok=True)
    bad_source.write_text(
        '"""仅用于验证生产包边界拒绝规则的输入。"""\n\n'
        "import spikes\n\n\n"
        "class EmptyClass:\n"
        '    """只有说明、没有实现的非法空壳类。"""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_p0, "production_python_files", lambda: [bad_source])
    violations = verify_p0.check_package_boundary()
    assert any(item.endswith(":spikes") for item in violations)
    assert any(item.endswith(":EmptyClass") for item in violations)


def test_p0_2_t03_real_wheel_excludes_non_production_material(
    built_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0.2-T03：真实构建 wheel 不包含样本、证据、临时或旧工作流。"""

    assert verify_p0.audit_wheel(built_wheel) == []
    with zipfile.ZipFile(built_wheel) as wheel:
        members = set(wheel.namelist())
    assert "transflow/__init__.py" in members
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_p0.py",
            "wheel",
            "--wheel",
            built_wheel.relative_to(REPO_ROOT).as_posix(),
        ],
    )
    assert verify_p0.main() == 0


def test_p0_3_t01_smoke_coverage_and_architecture_checks_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0.3-T01：快速检查均真实执行，覆盖率由当前 pytest Gate 采集。"""

    results = verify_p0.all_checks()
    assert results == {key: [] for key in results}
    report_path = REPO_ROOT / "tmp" / "p0-tests" / "passed-gate-report.json"
    return_code, report = run_gate.execute_gate(
        "TEST_PASS",
        REPO_ROOT / "tests" / "fixtures" / "passing_gate_catalog.json",
        report_path,
    )
    assert return_code == 0
    assert report["conclusion"] == "PASS"
    assert report["steps"][0]["stdout"].strip() == "INJECTED_SUCCESS_EXIT_0"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gate.py",
            "TEST_PASS",
            "--catalog",
            "tests/fixtures/passing_gate_catalog.json",
            "--report",
            "tmp/p0-tests/passed-gate-cli-report.json",
        ],
    )
    assert run_gate.main() == 0
    monkeypatch.setattr(sys, "argv", ["verify_p0.py", "all"])
    assert verify_p0.main() == 0


def test_p0_3_t02_failed_command_blocks_gate() -> None:
    """P0.3-T02：真实退出码 9 的命令必须使 Gate 非零退出。"""

    report_path = REPO_ROOT / "tmp" / "p0-tests" / "failed-gate-report.json"
    return_code, direct_report = run_gate.execute_gate(
        "TEST_FAIL",
        REPO_ROOT / "tests" / "fixtures" / "failing_gate_catalog.json",
        report_path,
    )
    assert return_code == 9
    assert direct_report["conclusion"] == "FAIL"
    assert direct_report["steps"][0]["stdout"].strip() == "INJECTED_FAILURE_EXIT_9"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_gate.py",
            "TEST_FAIL",
            "--catalog",
            "tests/fixtures/failing_gate_catalog.json",
            "--report",
            report_path.relative_to(REPO_ROOT).as_posix(),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 9
    assert "INJECTED_FAILURE_EXIT_9" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["conclusion"] == "FAIL"
    assert report["steps"][0]["return_code"] == 9

    with pytest.raises(ValueError, match="必须相对仓库根"):
        run_gate.resolve_repository_path(REPO_ROOT)
    with pytest.raises(ValueError, match="越出仓库根"):
        run_gate.resolve_repository_path("../outside.json")

    invalid_catalog = REPO_ROOT / "tmp" / "p0-tests" / "invalid-gate-catalog.json"
    invalid_catalog.write_text(
        json.dumps({"schema_version": "unsupported", "gates": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        run_gate.load_catalog(invalid_catalog)
    invalid_catalog.write_text(
        json.dumps({"schema_version": "transflow.gate-catalog/v1"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少 gates"):
        run_gate.load_catalog(invalid_catalog)
    with pytest.raises(ValueError, match="未登记"):
        run_gate.execute_gate(
            "UNKNOWN_GATE",
            REPO_ROOT / "tests" / "fixtures" / "passing_gate_catalog.json",
            report_path,
        )


def test_p0_3_t03_ci_runs_fast_gate_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0.3-T03：CI 同时覆盖 push/PR，且没有重型 PDF E2E 触发。"""

    assert verify_p0.check_ci() == []
    invalid_ci = REPO_ROOT / "tmp" / "p0-tests" / "invalid-ci.yml"
    invalid_ci.write_text("schedule:\n  - cron: daily\n", encoding="utf-8")
    monkeypatch.setattr(verify_p0, "CI_PATH", invalid_ci)
    violations = verify_p0.check_ci()
    assert len([item for item in violations if item.startswith("CI_REQUIRED_TOKEN_MISSING")]) == 3
    assert violations[-1] == "CI_HEAVY_TRIGGER_FOUND:schedule:"


def test_p0_3_t04_forbidden_wheel_member_is_rejected() -> None:
    """P0.3-T04：带真实禁止成员的合成 wheel 必须被内容审计拒绝。"""

    fixture_wheel = REPO_ROOT / "tmp" / "p0-tests" / "forbidden-material.whl"
    fixture_wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(fixture_wheel, mode="w") as wheel:
        wheel.writestr("transflow/__init__.py", "__version__ = 'test'\n")
        wheel.writestr("spikes/leaked-run.json", "{}\n")
    violations = verify_p0.audit_wheel(fixture_wheel)
    assert violations == ["FORBIDDEN_WHEEL_MEMBER:spikes/leaked-run.json"]
    assert verify_p0.audit_wheel(REPO_ROOT.parent / "outside.whl") == [
        "WHEEL_OUTSIDE_REPOSITORY"
    ]
    assert verify_p0.audit_wheel(REPO_ROOT / "tmp" / "missing.whl") == ["WHEEL_NOT_FOUND"]


def test_p0_4_t01_traceability_has_no_dangling_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0.4-T01：计划全部任务、测试和 Gate 引用解析后无悬空项。"""

    assert verify_p0.check_traceability() == []
    invalid_traceability = REPO_ROOT / "tmp" / "p0-tests" / "invalid-traceability.json"
    invalid_traceability.write_text(
        json.dumps(
            {
                "validation": {
                    "invalid_design_references": ["99.1"],
                    "dangling_gate_test_references": ["P0.0-T99"],
                    "unowned_test_definitions": ["P0.0-T98"],
                },
                "tasks": [
                    {
                        "task_id": "P0.0",
                        "design_sections": [],
                        "delivery_contract": "",
                        "test_ids": [],
                        "gate_items": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(assets, "TRACEABILITY_PATH", invalid_traceability)
    violations = verify_p0.check_traceability()
    assert len(violations) == 7
    assert "TASK_WITHOUT_GATE:P0.0" in violations


def test_p0_4_t02_open_decision_blocks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0.4-T02：模拟未关闭待决策时只能得到 BLOCKED_BY_DECISION。"""

    assert (
        verify_p0.evaluate_gate_conclusion(["D-P0-SIMULATED"], checks_passed=True)
        == "BLOCKED_BY_DECISION"
    )
    assert verify_p0.evaluate_gate_conclusion([], checks_passed=False) == "FAIL"
    assert verify_p0.evaluate_gate_conclusion([], checks_passed=True) == "PASS"
    invalid_governance = REPO_ROOT / "tmp" / "p0-tests" / "invalid-governance.json"
    invalid_governance.write_text(
        json.dumps(
            {
                "allowed_stage_statuses": ["PASS"],
                "current_stage": {"status": "UNKNOWN"},
                "record_templates": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_p0, "GOVERNANCE_PATH", invalid_governance)
    assert verify_p0.check_governance() == [
        "INVALID_CURRENT_STAGE_STATUS",
        "GOVERNANCE_TEMPLATE_SET_INCOMPLETE",
    ]


def test_p0_4_t03_design_and_test_reverse_indexes_resolve() -> None:
    """P0.4-T03：设计章节和测试 ID 均可反向定位任务。"""

    traceability = load_json("docs/迁移/traceability_matrix.json")
    indexes = traceability["indexes"]
    assert "P0.2" in indexes["by_design_section"]["19.1"]
    assert indexes["by_test_id"]["P0.1-T01"] == "P0.1"
    assert len(indexes["by_test_id"]) > 0


def test_p0_5_t01_schedule_is_topologically_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0.5-T01：P1-P14 负责人、窗口和前置 Gate 拓扑全部有效。"""

    assert verify_p0.check_schedule() == []
    invalid_schedule = load_json(
        "docs/计划/Transflow_P1-P14_依赖迭代排期_v0.1.json"
    )
    estimation_inputs = invalid_schedule["estimation_inputs"]
    assert estimation_inputs["fixture_inventory_at_p0"]["annual_report_pdf_files"] > 0
    assert estimation_inputs["fixture_inventory_at_p0"]["classification_result_pdf_files"] > 0
    assert estimation_inputs["heavy_e2e_runtime"]["first_measurement_stage"] == "P4"
    assert estimation_inputs["target_server"]["verification_stage"] == "P1"
    invalid_schedule["first_part"][0]["stage"] = "P2"
    invalid_schedule["first_part"][0]["order"] = 2
    invalid_schedule["first_part"][0]["dependency_gate"] = "G9"
    invalid_schedule["first_part"][0]["owner"] = ""
    invalid_schedule["resource_assumptions"]["implementation_lanes"] = 2
    invalid_schedule["g14_stop"]["required"] = False
    invalid_schedule["second_part"][0]["fixed_date"] = "2099-01-01"
    invalid_path = REPO_ROOT / "tmp" / "p0-tests" / "invalid-schedule.json"
    invalid_path.write_text(
        json.dumps(invalid_schedule, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_p0, "SCHEDULE_PATH", invalid_path)
    violations = verify_p0.check_schedule()
    assert "P1_P14_STAGE_SEQUENCE_INVALID" in violations
    assert "INVALID_ORDER:P2" in violations
    assert "REVERSE_OR_MISSING_DEPENDENCY:P2" in violations
    assert "SCHEDULE_FIELD_MISSING:P2:owner" in violations
    assert "P0_UNAPPROVED_PARALLEL_LANES" in violations
    assert "G14_STOP_NOT_REQUIRED" in violations
    assert "SECOND_PART_FIXED_DATE:P15" in violations


def test_p0_5_t02_gate_delay_shifts_all_downstream_windows() -> None:
    """P0.5-T02：模拟 P5 延迟时 P5-P14 等量顺延且不跳过阶段。"""

    schedule = load_json("docs/计划/Transflow_P1-P14_依赖迭代排期_v0.1.json")
    shifted = verify_p0.simulate_delay(schedule, delayed_stage="P5", slots=2)
    assert shifted["P4"] == 4
    assert shifted["P5"] == 7
    assert shifted["P14"] == 19
    assert list(shifted) == [
        "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9",
        "P9C", "P9A", "P9B", "P10", "P11", "P12", "P13", "P14",
    ]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
