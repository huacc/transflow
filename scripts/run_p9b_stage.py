"""执行 G9B 最终命令并生成唯一、逐项含原始输出的阶段报告。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger("transflow.p9b.stage")
REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_ROOT = REPO_ROOT / "docs" / "reports"
GATE_PATH = REPO_ROOT / "resources" / "manifests" / "p9b_gate.json"


@dataclass(frozen=True, slots=True)
class StageCommand:
    """描述一个可追溯 P9B 命令及其覆盖的 Gate 项。"""

    command_id: str
    intent: str
    gate_items: tuple[str, ...]
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageCommandResult:
    """保存真实命令、退出码及未经改写的标准输出和错误。"""

    command: StageCommand
    return_code: int
    stdout: str
    stderr: str


COMMANDS = (
    StageCommand(
        "P9B-REAL-SAMPLES",
        "运行生产分类、真实千问、六叶修复、故障恢复和两份完整 PDF",
        ("G9B-3", "G9B-4", "G9B-5", "G9B-6", "G9B-7", "G9B-11"),
        ("-m", "scripts.run_p9b_real_samples"),
    ),
    StageCommand(
        "P9B-34-TESTS",
        "运行 P9B.1-P9B.4 全部 34 个编号验收用例",
        tuple(f"G9B-{index}" for index in range(1, 12)),
        ("-m", "pytest", "-vv", "tests/test_p9b.py"),
    ),
    StageCommand(
        "P9B-ARTIFACT-AUDIT",
        "重开候选与最终 PDF 并复算页记忆、恢复和静态边界",
        ("G9B-1", "G9B-3", "G9B-6", "G9B-7", "G9B-8", "G9B-10", "G9B-11"),
        ("-m", "scripts.verify_p9b"),
    ),
    StageCommand(
        "P9B-UPSTREAM-REGRESSION",
        "重跑 G8/G9 与 G9C 语义、完整性和 Route 错配回归",
        ("G9B-5", "G9B-8", "G9B-9", "G9B-11"),
        (
            "-m",
            "pytest",
            "-q",
            "tests/test_p8.py",
            "tests/test_p9.py",
            "tests/test_p9c.py::test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text",
            "tests/test_p9c.py::test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full",
            "tests/test_p9c.py::test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation",
        ),
    ),
    StageCommand(
        "P9B-STATIC",
        "检查生产代码、脚本和测试的格式、导入与静态错误",
        ("G9B-1", "G9B-2", "G9B-3", "G9B-8"),
        ("-m", "ruff", "check", "src", "scripts", "tests"),
    ),
    StageCommand(
        "P9B-TYPES",
        "检查生产包、阶段脚本和 P9B 测试类型边界",
        ("G9B-1", "G9B-2", "G9B-3", "G9B-6"),
        ("-m", "mypy", "src", "scripts", "tests/test_p9b.py"),
    ),
    StageCommand(
        "P9B-ARCHITECTURE",
        "扫描生产依赖方向和禁止实现",
        ("G9B-2", "G9B-5", "G9B-8", "G9B-9"),
        ("-m", "scripts.verify_architecture"),
    ),
)


def execute(command: StageCommand) -> StageCommandResult:
    """在 Transflow 仓库根执行一个命令并完整捕获 UTF-8 证据。"""

    argv = (sys.executable, *command.arguments)
    LOGGER.info("调用阶段命令，意图=%s command_id=%s", command.intent, command.command_id)
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
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
    return StageCommandResult(command, completed.returncode, completed.stdout, completed.stderr)


def render_report(results: tuple[StageCommandResult, ...], generated_at: datetime) -> str:
    """逐项输出通过/失败，并为每项嵌入真实命令输出证据。"""

    item_rows: list[str] = []
    for index in range(1, 12):
        item = f"G9B-{index}"
        evidence = tuple(result for result in results if item in result.command.gate_items)
        passed = bool(evidence) and all(result.return_code == 0 for result in evidence)
        command_ids = ", ".join(result.command.command_id for result in evidence)
        item_rows.append(f"| {item} | {'通过' if passed else '失败'} | `{command_ids}` |")
    conclusion = "PASS" if all(result.return_code == 0 for result in results) else "FAIL"
    sections = [
        "# P9B 阶段 · 页级修复记忆、确定性修复目录与多轮重排执行报告",
        "",
        "- 所属阶段：P9B",
        f"- 具体时间：{generated_at.astimezone().isoformat(timespec='seconds')}",
        f"- Gate 结论：{conclusion}",
        "- 工作目录：Transflow 仓库根；所有持久路径均由仓库相对路径派生。",
        "- 秘密边界：报告和 Artifact 不记录 API Key、Authorization 或 Provider 原始响应。",
        "",
        "## 逐项验收结论",
        "",
        "| 验收项 | 结论 | 实际证据命令 |",
        "|---|---|---|",
        *item_rows,
        "",
        "## 实现范围与设计判断",
        "",
        (
            "P9B 只实现当前 run 页级 Repair Memory、叶静态 RepairAtomCatalog、"
            "版本化 comparator、最多三轮确定性修复、每轮 Artifact/Checkpoint 和安全回滚。"
        ),
        (
            "既有 Toolbox repair 仅由手工登记 legacy adapter 调用；未实现 Repair 模型、"
            "Rule IR、运行时 Registry 学习或 FINALIZED 后全书二次布局。"
        ),
        (
            "真实产物清单位于 `resources/evidence/p9b/real_run_manifest.json`；"
            "六叶及两份完整文档的 input/output 对比位于 `output/pdf/P9B_real_repairs/`。"
        ),
        (
            "安全 final 发生源文透传时不冒充译文；真实中文位于 P9B 的"
            " `diagnostic/input_vs_translated_diagnostic.pdf` 双轨对比中，且明确标记不进入 final。"
        ),
        "",
        "## 原始命令证据",
    ]
    for result in results:
        argv = subprocess.list2cmdline((sys.executable, *result.command.arguments))
        status = "通过" if result.return_code == 0 else "失败"
        output = result.stdout
        if result.stderr:
            output = (
                f"{output}\n[stderr]\n{result.stderr}"
                if output
                else f"[stderr]\n{result.stderr}"
            )
        sections.extend(
            (
                "",
                f"### {result.command.command_id} · {status}",
                "",
                f"命令：`{argv}`",
                f"退出码：`{result.return_code}`",
                "",
                "```text",
                output.rstrip(),
                "```",
            )
        )
    return "\n".join(sections) + "\n"


def _write_gate(report_path: Path, results: tuple[StageCommandResult, ...]) -> None:
    """仅在全部真实命令通过后，把 G9B manifest 更新为 PASS。"""

    payload = {
        "schema_version": "transflow.stage-gate/v1",
        "gate": "G9B",
        "stage": "P9B",
        "status": "PASS",
        "items": [f"G9B-{index}" for index in range(1, 12)],
        "commands": [
            {
                "command_id": result.command.command_id,
                "return_code": result.return_code,
            }
            for result in results
        ],
        "report_path": report_path.relative_to(REPO_ROOT).as_posix(),
    }
    GATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """执行全部命令、写唯一秒级报告，并只在全通过时发布 Gate。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = tuple(execute(command) for command in COMMANDS)
    generated_at = datetime.now().astimezone()
    report_name = f"P9B阶段_页级修复记忆与多轮重排_{generated_at:%Y%m%d_%H%M%S}.md"
    report_path = REPORT_ROOT / report_name
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, generated_at), encoding="utf-8")
    passed = all(item.return_code == 0 for item in results)
    if passed:
        _write_gate(report_path, results)
    print(
        json.dumps(
            {
                "report_path": report_path.relative_to(REPO_ROOT).as_posix(),
                "conclusion": "PASS" if passed else "FAIL",
                "commands": {item.command.command_id: item.return_code for item in results},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
