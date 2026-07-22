"""执行 G9A 最终命令并生成唯一、逐项含原始输出的阶段报告。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger("transflow.p9a.stage")
REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_ROOT = REPO_ROOT / "docs" / "reports"


@dataclass(frozen=True, slots=True)
class StageCommand:
    """描述一个可追溯 Gate 命令及其覆盖的验收项。"""

    command_id: str
    intent: str
    gate_items: tuple[str, ...]
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageCommandResult:
    """保存真实命令、退出码和未改写的标准输出/错误。"""

    command: StageCommand
    return_code: int
    stdout: str
    stderr: str


COMMANDS = (
    StageCommand(
        "P9A-BASELINE",
        "核验 current overlay、字母阶段追溯与排期",
        ("G9A-0-1", "G9A-0-2"),
        ("-m", "scripts.build_p0_assets", "--check"),
    ),
    StageCommand(
        "P9A-P0-MECHANICS",
        "运行 P0 机械治理真实回归",
        ("G9A-0-2",),
        ("-m", "pytest", "-q", "tests/test_p0.py"),
    ),
    StageCommand(
        "P9A-29-TESTS-COVERAGE",
        "运行 P9A.0-P9A.4 全部 29 项及关键模块分支覆盖率",
        ("G9A-0-3", "G9A-1", "G9A-2", "G9A-3", "G9A-4", "G9A-5"),
        (
            "-m",
            "pytest",
            "-vv",
            "tests/test_p9a.py",
            "--cov=transflow.domain.layout_memory",
            "--cov=transflow.domain.text_inventory",
            "--cov=transflow.pdf_kernel.text_inventory",
            "--cov=transflow.application.document_layout_memory",
            "--cov=transflow.adapters.filesystem.layout_memory_runtime",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=json:tmp/p9a-coverage.json",
            "--cov-fail-under=85",
        ),
    ),
    StageCommand(
        "P9A-UPSTREAM-REGRESSION",
        "运行 G8/G9/G9C 受影响 Route/Toolbox/SemanticUnitMap 接口回归",
        ("G9A-6",),
        (
            "-m",
            "pytest",
            "-q",
            "tests/test_p7.py",
            "tests/test_p8.py",
            "tests/test_p9.py",
            "tests/test_p9c.py",
            "-k",
            "p7_1_t01 or p8_1_t01 or p9_1_t01 or p9c_2_t01",
        ),
    ),
    StageCommand(
        "P9A-REAL-PDFS",
        "实际提取两份完整年报并发布 memory Artifact/Checkpoint",
        ("G9A-6",),
        ("-m", "scripts.run_p9a_real_samples", "--write"),
    ),
    StageCommand(
        "P9A-AUDIT",
        "复核治理、两份真实年报、252 页引用和内容寻址 hash",
        ("G9A-1", "G9A-2", "G9A-3", "G9A-4", "G9A-5", "G9A-6"),
        ("-m", "scripts.verify_p9a"),
    ),
    StageCommand(
        "P9A-STATIC",
        "检查生产代码、脚本和测试格式/导入/静态错误",
        ("G9A-1", "G9A-2", "G9A-4"),
        ("-m", "ruff", "check", "src", "scripts", "tests"),
    ),
    StageCommand(
        "P9A-TYPES",
        "检查全部生产包和工程脚本类型边界",
        ("G9A-1", "G9A-2", "G9A-4", "G9A-5"),
        ("-m", "mypy", "src", "scripts"),
    ),
    StageCommand(
        "P9A-ARCHITECTURE",
        "扫描真实生产包依赖方向与禁用实现",
        ("G9A-2", "G9A-4"),
        ("-m", "scripts.verify_architecture"),
    ),
)


def execute(command: StageCommand) -> StageCommandResult:
    """在仓库根执行单个命令并完整捕获 UTF-8 证据。"""

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
    """逐 Gate 汇总通过/失败，并为每个结论嵌入真实命令输出。"""

    all_items = (
        "G9A-0-1",
        "G9A-0-2",
        "G9A-0-3",
        "G9A-1",
        "G9A-2",
        "G9A-3",
        "G9A-4",
        "G9A-5",
        "G9A-6",
    )
    item_rows: list[str] = []
    for item in all_items:
        evidence = tuple(result for result in results if item in result.command.gate_items)
        passed = bool(evidence) and all(result.return_code == 0 for result in evidence)
        command_ids = ", ".join(result.command.command_id for result in evidence)
        item_rows.append(f"| {item} | {'通过' if passed else '失败'} | `{command_ids}` |")
    conclusion = "PASS" if all(result.return_code == 0 for result in results) else "FAIL"
    sections = [
        "# P9A 阶段 · 文档级布局记忆执行报告",
        "",
        "- 所属阶段：P9A（含 G9A-0）",
        f"- 具体时间：{generated_at.astimezone().isoformat(timespec='seconds')}",
        f"- Gate 结论：{conclusion}",
        (
            "- 工作目录：Transflow 仓库根（所有代码路径均由 "
            "`Path(__file__).resolve().parent.parent` 派生）"
        ),
        "- 秘密边界：未读取、写入或输出任何 VLM/API Key；P9A 构建不调用模型。",
        "",
        "## 逐项验收结论",
        "",
        "| 验收项 | 结论 | 实际证据命令 |",
        "|---|---|---|",
        *item_rows,
        "",
        "## 真实样本与产物摘要",
        "",
        (
            "两份完整真实年报共 252 页，均实际经过 PageFacts 提取、Builder、"
            "内容寻址 Artifact 和 Checkpoint；持久清单位于 "
            "`resources/evidence/p9a/real_document_manifest.json`。"
        ),
        "",
        "## 设计判断与范围",
        "",
        (
            "P9A 只冻结源布局基线和翻译前目标语言政策；未实现 P9B 页级修复循环，"
            "也未把译文、候选、SemanticUnitMap 或页内 table/cell/owner/anchor "
            "明细写入文档记忆。"
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
                f"{output}\n[stderr]\n{result.stderr}" if output else f"[stderr]\n{result.stderr}"
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


def main() -> int:
    """执行全部命令、写一份带秒级时间戳的报告并返回 Gate 状态。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = tuple(execute(command) for command in COMMANDS)
    generated_at = datetime.now().astimezone()
    report_name = f"P9A阶段_文档级布局记忆_{generated_at:%Y%m%d_%H%M%S}.md"
    report_path = REPORT_ROOT / report_name
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, generated_at), encoding="utf-8")
    print(
        json.dumps(
            {
                "report_path": report_path.relative_to(REPO_ROOT).as_posix(),
                "conclusion": "PASS" if all(item.return_code == 0 for item in results) else "FAIL",
                "commands": {item.command.command_id: item.return_code for item in results},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if any(item.return_code != 0 for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
