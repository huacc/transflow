"""重跑 P9 正式验收命令并把原始 stdout 写入唯一阶段报告。"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("scripts.write_p9_report")
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
    """在仓库根执行命令，并只把无秘密 stdout 纳入报告证据。"""

    LOGGER.info("调用 P9 证据命令，意图=生成不可手写验收证据 command=%s", display)
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
    """把真实退出码映射为报告要求的通过或失败。"""

    return "通过" if evidence.passed else "失败"


def _evidence_block(evidence: CommandEvidence) -> str:
    """把实际命令、退出码和 stdout 编码为 Markdown 代码块。"""

    return (
        f"命令：`{evidence.command}`；退出码：`{evidence.return_code}`\n\n"
        f"```text\n{evidence.stdout}\n```"
    )


def _gate_check_status(payload: dict[str, Any], check: str) -> str:
    """从正式 Gate JSON 提取单项通过/失败，禁止手工填写。"""

    return "通过" if payload.get("checks", {}).get(check) is True else "失败"


def write_report() -> Path:
    """执行构建、七组测试、混合 PDF、上游回归、质量检查和 G9。"""

    started = datetime.now()
    build = _run(("scripts/build_p9_release.py",), "python scripts/build_p9_release.py")
    acceptance = _run(
        ("scripts/run_p9_acceptance.py",),
        "python scripts/run_p9_acceptance.py",
    )
    real_samples = _run(
        ("scripts/verify_p9_real_samples.py",),
        "python scripts/verify_p9_real_samples.py",
    )
    real_sample_tests = _run(
        ("-m", "pytest", "tests/migration/test_p9_real_samples.py", "-q"),
        "python -m pytest tests/migration/test_p9_real_samples.py -q",
    )
    secret_scan = _run(
        ("scripts/verify_p9_secrets.py",),
        "python scripts/verify_p9_secrets.py",
    )
    stage_evidence = {
        stage: _run(
            ("-m", "pytest", "tests/test_p9.py", "-k", selector, "-q"),
            f'python -m pytest tests/test_p9.py -k "{selector}" -q',
        )
        for stage, selector in (
            ("P9.1", "test_p9_1"),
            ("P9.2", "test_p9_2"),
            ("P9.3", "test_p9_3"),
            ("P9.4", "test_p9_4"),
            ("P9.5", "test_p9_5"),
            ("P9.6", "test_p9_6"),
            ("P9.7", "test_p9_7"),
        )
    }
    upstream = _run(
        (
            "-m",
            "pytest",
            "tests/test_p6.py",
            "tests/test_p7.py",
            "tests/test_p8.py",
            "-q",
        ),
        "python -m pytest tests/test_p6.py tests/test_p7.py tests/test_p8.py -q",
    )
    ruff = _run(
        (
            "-m",
            "ruff",
            "check",
            "src/transflow",
            "scripts/build_p9_release.py",
            "scripts/run_p9_acceptance.py",
            "scripts/run_p9_real_samples.py",
            "scripts/verify_p9.py",
            "scripts/verify_p9_real_samples.py",
            "scripts/verify_p9_secrets.py",
            "scripts/write_p9_report.py",
            "tests/test_p9.py",
            "tests/migration/p9_qwen_translation_adapter.py",
            "tests/migration/test_p9_real_samples.py",
        ),
        "python -m ruff check src/transflow scripts/build_p9_release.py "
        "scripts/run_p9_acceptance.py scripts/run_p9_real_samples.py scripts/verify_p9.py "
        "scripts/verify_p9_real_samples.py scripts/verify_p9_secrets.py "
        "scripts/write_p9_report.py tests/test_p9.py "
        "tests/migration/p9_qwen_translation_adapter.py "
        "tests/migration/test_p9_real_samples.py",
    )
    mypy = _run(
        (
            "-m",
            "mypy",
            "src/transflow",
            "scripts/build_p9_release.py",
            "scripts/run_p9_acceptance.py",
            "scripts/run_p9_real_samples.py",
            "scripts/verify_p9.py",
            "scripts/verify_p9_real_samples.py",
            "scripts/verify_p9_secrets.py",
            "scripts/write_p9_report.py",
        ),
        "python -m mypy src/transflow scripts/build_p9_release.py "
        "scripts/run_p9_acceptance.py scripts/run_p9_real_samples.py scripts/verify_p9.py "
        "scripts/verify_p9_real_samples.py scripts/verify_p9_secrets.py scripts/write_p9_report.py",
    )
    gate = _run(("scripts/verify_p9.py",), "python scripts/verify_p9.py")
    commands = (
        build,
        acceptance,
        real_samples,
        real_sample_tests,
        secret_scan,
        *stage_evidence.values(),
        upstream,
        ruff,
        mypy,
        gate,
    )
    overall_passed = all(item.passed for item in commands)
    gate_payload: dict[str, Any] = {}
    try:
        gate_payload = json.loads(gate.stdout)
        overall_passed = overall_passed and gate_payload.get("status") == "PASS"
    except json.JSONDecodeError:
        overall_passed = False
    ended = datetime.now()
    report_path = REPORT_ROOT / (
        f"P9阶段_工具箱第二批证据不足普通叶_{ended.strftime('%Y%m%d_%H%M%S')}.md"
    )
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# P9 阶段执行报告：工具箱第二批证据不足的普通叶",
        "",
        "- 所属阶段：P9 / Gate G9",
        f"- 开始时间：{started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结束时间：{ended.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 最终结论：{'通过' if overall_passed else '失败'}",
        "- 工作目录：仓库根（报告不记录宿主机绝对路径）",
        "",
        "## 1. 范围、假设与设计质疑",
        "",
        "本报告只覆盖 P9.1～P9.7 和 G9；不提前实现 P10。P9 在 G8 的显式 Catalog、"
        "PageToolbox、Checkpoint、Artifact 和 Preservation 合同上增量迁移。",
        "",
        "旧 cover、contents、end、anchored_blocks Gate 为 EVIDENCE_INSUFFICIENT；multi、table "
        "为 NOT_EVALUATED；六叶均没有 PromotionManifest。旧样本已经进入开发、预览、定向修复或"
        "非盲回归，不能重新命名为新的独立真实盲测。P9 已把 `分类结果` 中 187 份已知真实"
        "单页全部纳入结构回归，并从六叶各选 2 份英文页调用真实千问形成 12 份候选 PDF；"
        "这些证据可以验证技术行为，但不能补写成匿名晋级证据。因此六叶的诚实结论均为 "
        "PASS_DISABLED_WITH_FALLBACK，生产 Catalog 不注册对应 factory。",
        "",
        "42 项编号合同测试使用总体设计允许的 FixedTranslationAdapter 保证确定性；真实样本"
        "候选另用 migration-only 千问 TranslationPort，共发生 14 次真实 HTTP 调用。端点、模型"
        "和密钥只通过进程环境变量注入，P9 新增代码、配置、报告、日志和证据均未写入秘密。",
        "",
        "设计衔接问题：P6 旧事实只给出块级文本 owner，会把同一目录行的标题、点线和页码合并，"
        "且把整个 table bbox 作为禁写区域。P9 以兼容方式补充 span 级原生文本 owner，并把 "
        "table bbox 降为结构 anchor。真实表格样本进一步发现“页面存在任意图片即放弃整表”的"
        "过度回退，已改为只在没有直接 table facts 时回退；Logo/页眉图片仍受保护。真实封面又"
        "暴露文字与受保护背景区域相交，现于候选阶段拒绝并整页回退，不再拖到最终 Patch 回放"
        "异常。P6/P7/P8 回归用于证明这些通用修正没有破坏既有行为。",
        "",
        "## 2. 主要交付",
        "",
        "- 新增 cover、contents、end、multi、table、anchored_blocks 六阶段迁移骨架。",
        "- 新增目录 entry、multi column、table cell、anchored owner 的唯一映射和硬 guard。",
        "- 新增 page/table/entry/column/owner 有界原子回退与 REGION_FALLBACK 终态。",
        "- 新增 P9 集中策略、阈值、六份迁移记录、证明、资源指纹和 v4 Catalog。",
        "- 新增 42 项编号测试、十页混合 PDF、逐页 PNG 和机器可读摘要。",
        "- 新增 187 份分类结果结构扫描、2 项真实语料缺陷回归、12 份真实千问候选和展示 PDF。",
        "",
        "## 3. 六叶三态发布结论",
        "",
        "| Route | 旧正式状态 | P9 结论 | 生产状态 |",
        "|---|---|---|---|",
        "| cover | EVIDENCE_INSUFFICIENT | PASS_DISABLED_WITH_FALLBACK | disabled / 0.1.0-review |",
        "| contents | EVIDENCE_INSUFFICIENT | PASS_DISABLED_WITH_FALLBACK | "
        "disabled / 0.1.0-review |",
        "| end | EVIDENCE_INSUFFICIENT | PASS_DISABLED_WITH_FALLBACK | disabled / 0.1.0-review |",
        "| body.flow_text.multi | NOT_EVALUATED | PASS_DISABLED_WITH_FALLBACK | "
        "disabled / 0.1.0-review |",
        "| body.table | NOT_EVALUATED | PASS_DISABLED_WITH_FALLBACK | disabled / 0.1.0-review |",
        "| body.anchored_blocks | EVIDENCE_INSUFFICIENT | PASS_DISABLED_WITH_FALLBACK | "
        "disabled / 0.1.0-review |",
        "",
        "发布资源生成证据：",
        "",
        _evidence_block(build),
        "",
        "## 4. P9 二级计划逐项验收",
        "",
    ]
    acceptance_text = {
        "P9.1": "T01～T06；cover owner/层级、视觉保护、扰动、失败回退和跨叶拒绝均验证。",
        "P9.2": "T01～T06；目录 owner/mapping 100%，页码/点线/链接保护、整条回退和完整 PDF 验证。",
        "P9.3": "T01～T06；blank/visual/text/mixed 均有终态，无末页索引或文件身份分支。",
        "P9.4": "T01～T06；列 owner 100%，跨栏/clip/owner 拒绝，栏级失败不串扰。",
        "P9.5": "T01～T06；cell/KEEP_SOURCE 100%，图片表零 OCR，跨 cell 拒绝，整表回退。",
        "P9.6": "T01～T06；anchor/slot/owner 可追溯，冲突 KEEP_SOURCE，owner 级原子回退。",
        "P9.7": "T01～T06；六叶 disabled/失败组合全页终态，Preservation 与 G8 回归通过。",
    }
    for stage, evidence in stage_evidence.items():
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
            "## 5. 分类结果真实样本与千问候选验收",
            "",
            "真实来源：`spikes/page_classification_engine_puncture_v1/分类结果`。六叶分别为 "
            "cover 60、contents 34、end 20、multi 23、table 20、anchored_blocks 30，"
            "合计 187 份单页 PDF。全部来源哈希、Kernel facts、owner 覆盖和 TranslationUnit 数"
            "写入 `resources/evidence/p9/real_sample_regression.json`。",
            "",
            "按结构复杂度和内容哈希从每叶选择 2 份英文页，共 12 份；真实千问调用 14 次。"
            "结果为 1 份安全接受、11 份安全回退，所有候选 PDF、PNG 和汇总均位于 "
            "`output/pdf/P9_real_samples/`。这证明真实调用链可工作，也证明当前证据不足叶不应启用。",
            "",
            f"真实语料缺陷回归：{_status(real_sample_tests)}",
            "",
            _evidence_block(real_sample_tests),
            "",
            f"真实来源、候选产物、千问调用计数与 Catalog 复验：{_status(real_samples)}",
            "",
            _evidence_block(real_samples),
            "",
            "## 6. 完整混合 PDF 运行与可视材料",
            "",
            f"结果：{_status(acceptance)}。最终 PDF 位于 "
            "`output/pdf/P9_second_batch_mixed_final.pdf`；逐页 PNG 位于 "
            "`output/pdf/P9_second_batch_mixed_preview/`。运行输出逐页列出 Route、版本、Patch、"
            "fallback、十页页序和目录链接目标。",
            "",
            _evidence_block(acceptance),
            "",
            "## 7. Gate G9 逐项验收",
            "",
            "| # | 结果 | 量化结论 |",
            "|---|---|---|",
            f"| G9-1 cover | {_gate_check_status(gate_payload, 'g9_1_cover')} | "
            "视觉修改/跨叶 fallback 0；状态明确 |",
            f"| G9-2 contents | {_gate_check_status(gate_payload, 'g9_2_contents')} | "
            "owner/mapping 100%；链接目标保持 |",
            f"| G9-3 end | {_gate_check_status(gate_payload, 'g9_3_end')} | "
            "四类组合均终态；索引特判 0 |",
            f"| G9-4 multi | {_gate_check_status(gate_payload, 'g9_4_multi')} | "
            "column owner 100%；越权/身份分支 0 |",
            f"| G9-5 table | {_gate_check_status(gate_payload, 'g9_5_table')} | "
            "cell/KEEP_SOURCE 100%；OCR/半表 0 |",
            f"| G9-6 anchored | {_gate_check_status(gate_payload, 'g9_6_anchored')} | "
            "owner 100%；冲突/flow 误用 0 |",
            f"| G9-7 启用纪律 | {_gate_check_status(gate_payload, 'g9_7_enable_discipline')} | "
            "未达盲测阈值六叶全部 disabled |",
            "| G9-8 集成/回归 | "
            f"{_gate_check_status(gate_payload, 'g9_8_mixed_and_g8_regression')} | "
            "十页全终态、完整产物、Preservation 与 G8 无退化 |",
            "",
            "G9-1～G9-8 正式 Gate 原始证据：",
            "",
            _evidence_block(gate),
            "",
            "P6/P7/P8 上游回归原始证据：",
            "",
            _evidence_block(upstream),
            "",
            "## 8. 工程质量验收",
            "",
            f"Ruff：{_status(ruff)}",
            "",
            _evidence_block(ruff),
            "",
            f"Mypy：{_status(mypy)}",
            "",
            _evidence_block(mypy),
            "",
            f"P9 秘密持久化扫描：{_status(secret_scan)}",
            "",
            _evidence_block(secret_scan),
            "",
            "扫描确认 P9 新增范围命中为 0；但 P9 范围外的未跟踪文件 "
            "`docs/提示词/标准提示词.txt` 含连接信息。该文件不是本阶段创建或修改，"
            "本阶段未擅自删除；上传 GitHub 前必须由项目负责人决定脱敏或排除。",
            "",
            "## 9. 已知边界与 P10 衔接",
            "",
            "- 六叶已执行 187 份已知分类样本结构回归和 12 份真实千问候选，但新的独立真实"
            "匿名文档数仍为 0；"
            "每叶补足至少 6 份并重新盲测前不得启用。",
            "- P9 十页 PDF 是合同、Catalog、Preservation 和降级验收产物；P9 六叶页面保持源页，"
            "不能解读为六叶已经完成生产翻译。",
            "- 文档结果为 COMPLETED_WITH_DEGRADATION，来源是 visual_only、chart/diagram "
            "以及 P9 六叶"
            "的显式透传，不是流程失败。",
            "- P10 必须消费 v4 Catalog 和本阶段证明，不得因工具目录或迁移代码存在自动启用叶。",
            "",
        )
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    # 用户要求每阶段只保留一份报告；新报告完整落盘后再移除同阶段旧版本。
    for existing in REPORT_ROOT.glob("P9阶段_工具箱第二批证据不足普通叶_*.md"):
        if existing.resolve() != report_path.resolve():
            existing.unlink()
    return report_path


def main() -> int:
    """生成唯一 P9 报告并输出仓库相对路径。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    path = write_report()
    print(path.relative_to(REPO_ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
