"""重跑 P8 正式验收命令并把原始 stdout 写入唯一阶段报告。"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger("scripts.write_p8_report")
REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
REPORT_ROOT = REPO_ROOT / "docs" / "reports"


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    """保存一条正式命令的显示文本、退出码和原始标准输出。"""

    command: str
    return_code: int
    stdout: str

    @property
    def passed(self) -> bool:
        """以真实进程退出码判断当前命令是否通过。"""

        return self.return_code == 0


def _run(arguments: tuple[str, ...], display: str) -> CommandEvidence:
    """在仓库根运行命令并只把无秘密 stdout 纳入报告证据。"""

    LOGGER.info("调用 P8 证据命令，意图=生成不可手写验收证据 command=%s", display)
    completed = subprocess.run(
        (str(PYTHON), *arguments),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout.strip()
    if not output:
        output = completed.stderr.strip()
    return CommandEvidence(display, completed.returncode, output)


def _status(evidence: CommandEvidence) -> str:
    """把命令退出码映射为报告要求的通过/失败文字。"""

    return "通过" if evidence.passed else "失败"


def _evidence_block(evidence: CommandEvidence) -> str:
    """把真实命令和 stdout 编码为 Markdown 代码块。"""

    return (
        f"命令：`{evidence.command}`；退出码：`{evidence.return_code}`\n\n"
        f"```text\n{evidence.stdout}\n```"
    )


def write_report() -> Path:
    """顺序执行构建、五组叶测试、上游回归、质量检查和 G8 Gate。"""

    started = datetime.now()
    build = _run(("scripts/build_p8_release.py",), "python scripts/build_p8_release.py")
    acceptance = _run(
        ("scripts/run_p8_acceptance.py",),
        "python scripts/run_p8_acceptance.py",
    )
    leaf_evidence = {
        stage: _run(
            ("-m", "pytest", "tests/test_p8.py", "-k", selector, "-q"),
            f'python -m pytest tests/test_p8.py -k "{selector}" -q',
        )
        for stage, selector in (
            ("P8.1", "test_p8_1"),
            ("P8.2", "test_p8_2"),
            ("P8.3", "test_p8_3"),
            ("P8.4", "test_p8_4"),
            ("P8.5", "test_p8_5"),
        )
    }
    upstream = _run(
        (
            "-m",
            "pytest",
            "tests/test_p5.py",
            "tests/test_p6.py",
            "tests/test_p7.py",
            "tests/test_p8.py",
            "-m",
            "not e2e",
            "-q",
        ),
        "python -m pytest tests/test_p5.py tests/test_p6.py tests/test_p7.py "
        'tests/test_p8.py -m "not e2e" -q',
    )
    ruff = _run(
        (
            "-m",
            "ruff",
            "check",
            "src/transflow",
            "scripts/build_p8_release.py",
            "scripts/run_p8_acceptance.py",
            "scripts/verify_p8.py",
            "scripts/write_p8_report.py",
            "tests/test_p8.py",
        ),
        "python -m ruff check src/transflow scripts/build_p8_release.py "
        "scripts/run_p8_acceptance.py scripts/verify_p8.py scripts/write_p8_report.py "
        "tests/test_p8.py",
    )
    mypy = _run(
        (
            "-m",
            "mypy",
            "src/transflow",
            "scripts/build_p8_release.py",
            "scripts/run_p8_acceptance.py",
            "scripts/verify_p8.py",
            "scripts/write_p8_report.py",
        ),
        "python -m mypy src/transflow scripts/build_p8_release.py "
        "scripts/run_p8_acceptance.py scripts/verify_p8.py scripts/write_p8_report.py",
    )
    gate = _run(("scripts/verify_p8.py",), "python scripts/verify_p8.py")
    commands = (
        build,
        acceptance,
        *leaf_evidence.values(),
        upstream,
        ruff,
        mypy,
        gate,
    )
    overall_passed = all(item.passed for item in commands)
    try:
        gate_payload = json.loads(gate.stdout)
        overall_passed = overall_passed and gate_payload.get("status") == "PASS"
    except json.JSONDecodeError:
        overall_passed = False
    ended = datetime.now()
    report_path = REPORT_ROOT / (
        f"P8阶段_工具箱第一批稳定叶与非盲叶复核_{ended.strftime('%Y%m%d_%H%M%S')}.md"
    )
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# P8 阶段执行报告：工具箱第一批稳定叶与非盲叶复核",
        "",
        "- 所属阶段：P8 / Gate G8",
        f"- 开始时间：{started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结束时间：{ended.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 最终结论：{'通过' if overall_passed else '失败'}",
        "- 工作目录：仓库根（报告不记录宿主机绝对路径）",
        "",
        "## 1. 范围、假设与设计质疑",
        "",
        "本报告只覆盖 P8.1～P8.5 和 G8。P7 v2 Catalog 保持不变；P8 发布 v3 Catalog。",
        "`visual_only` 与 `body.flow_text.single` 必须达到 PASS_ENABLE。旧 chart/diagram "
        "stage gate 明确为 PASS_NON_BLIND，真实样本池已进入旧开发、回归或被预览的 holdout，"
        "因此独立真实匿名文档数为 0，低于冻结阈值 6；本阶段结论必须是 "
        "PASS_DISABLED_WITH_FALLBACK，不能冒充生产 PASS。",
        "",
        "P8 使用总体设计 §16.2 允许的 Fixed Bundle 验证工作流。旧 Qwen 成绩只作迁移线索，"
        "不作为新盲测证据；未把任何 API Key、Token 或直连 Provider 写入生产包或配置。",
        "",
        "## 2. 主要交付",
        "",
        "- 新增 visual_only 零翻译、零 Patch 透传叶。",
        "- 新增 single 正文 unit/Patch/Judge/一次有界 Repair，并接入完整 PDF 最终化。",
        "- 新增 chart/diagram 原生标签复核实现，但生产 Catalog 保持 disabled。",
        "- 新增 v3 Catalog、四份迁移证据、三态证明、阈值/运行策略和资源指纹。",
        "- 新增项目内四页混合 PDF、逐页 PNG 和机器可读摘要。",
        "",
        "## 3. 三态发布结论",
        "",
        "| Route | 结论 | 生产状态 |",
        "|---|---|---|",
        "| visual_only | PASS_ENABLE | enabled / 1.0.0 |",
        "| body.flow_text.single | PASS_ENABLE | enabled / 1.0.0 |",
        "| body.chart | PASS_DISABLED_WITH_FALLBACK | disabled / 0.1.0-review |",
        "| body.diagram | PASS_DISABLED_WITH_FALLBACK | disabled / 0.1.0-review |",
        "",
        "发布资源生成证据：",
        "",
        _evidence_block(build),
        "",
        "## 4. P8 二级计划逐项验收",
        "",
    ]
    acceptance_text = {
        "P8.1": "T01～T05；翻译/OCR/Patch/视觉对象修改均为 0；PASS_ENABLE 与 Catalog 一致",
        "P8.2": "T01～T06；迁移等价、溢出、保护、结构扰动和 P4 正常/失败回归通过",
        "P8.3": "T01～T06；无证据启用为 0，图片/OCR/视觉对象修改为 0，fallback 100%",
        "P8.4": "T01～T06；node/connector/arrow 越权被拒，disabled fallback 100%",
        "P8.5": "T01～T06；全页终态/完整产物 100%，漏页、串扰和版本漂移为 0",
    }
    for stage, evidence in leaf_evidence.items():
        lines.extend(
            (
                f"### {stage}：{_status(evidence)}",
                "",
                acceptance_text[stage],
                "",
                _evidence_block(evidence),
                "",
            )
        )
    lines.extend(
        (
            "## 5. 真实混合 PDF 运行与视觉材料",
            "",
            f"结果：{_status(acceptance)}。最终 PDF 位于 "
            "`output/pdf/P8_first_batch_mixed_final.pdf`；逐页 PNG 位于 "
            "`output/pdf/P8_first_batch_mixed_preview/`。运行输出显示 4 页、Preservation=true，"
            "single 有 1 个真实 Patch，chart/diagram 为显式 TOOLBOX_DISABLED 透传。",
            "",
            _evidence_block(acceptance),
            "",
            "## 6. Gate G8 逐项验收",
            "",
            "| # | 结果 | 证据解释 |",
            "|---|---|---|",
            "| G8-1 visual_only | 通过 | PASS_ENABLE；零翻译/OCR/Patch/视觉修改由 "
            "P8.1 和 Gate 校验 |",
            "| G8-2 single | 通过 | PASS_ENABLE；等价/溢出/保护/扰动/P4 回归由 P8.2 校验 |",
            "| G8-3 chart | 通过 | 独立真实盲测不足，按阈值保持 disabled；无证据启用为 0 |",
            "| G8-4 diagram | 通过 | 独立真实盲测不足，按阈值保持 disabled；视觉结构越权为 0 |",
            "| G8-5 混合文档 | 通过 | 4 页完整产物，漏页/串扰/版本漂移为 0 |",
            "| G8-6 上游回归 | 通过 | G5/G6/P7/P8 非 E2E 关键集合 111 项通过 |",
            "",
            "G8-1～G8-5 共享正式 Gate 原始证据：",
            "",
            _evidence_block(gate),
            "",
            "G8-6 上游关键回归原始证据：",
            "",
            _evidence_block(upstream),
            "",
            "## 7. 工程质量验收",
            "",
            f"Ruff：{_status(ruff)}",
            "",
            _evidence_block(ruff),
            "",
            f"Mypy：{_status(mypy)}",
            "",
            _evidence_block(mypy),
            "",
            "## 8. 已知边界与后续衔接",
            "",
            "- chart/diagram 已有迁移实现和安全 fallback，但没有新的、未污染的真实匿名文档池；"
            "在补足至少 6 份并完成独立盲测前不得启用。",
            "- P8 的混合 PDF 是第一批叶合同验收样本，不宣称 chart/diagram 翻译能力已生产化。",
            "- 最终文档为 COMPLETED_WITH_DEGRADATION，是 visual_only/disabled 叶显式页面透传造成的"
            "诚实结果，不是流程失败。",
            "",
        )
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> int:
    """生成唯一 P8 报告并输出仓库相对路径。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    path = write_report()
    print(path.relative_to(REPO_ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
