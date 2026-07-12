from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .judge import judge_candidate
from .layout_planner import plan_layout
from .models import ToolboxDecision, ToolboxFinding
from .renderer import render_candidate
from .template_builder import build_page_template


@dataclass(frozen=True)
class P3RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None


def run_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> P3RunResult:
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    if sha256_file(source_snapshot) != sha256_file(source_pdf):
        raise RuntimeError("source_snapshot_hash_mismatch")
    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "page-run-package/v1",
            "toolbox_key": "body.flow_text.single",
            "page_id": page_id,
            "source_snapshot": "input/source.pdf",
            "source_sha256": sha256_file(source_snapshot),
            "candidate_output": "output/candidate.pdf",
            "required_directories": ["contracts", "docs", "input", "output", "previews", "reports"],
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        "# 页级运行包\n\n"
        f"页面：`{page_id}`；工具箱：`body.flow_text.single`。\n\n"
        "- `input/source.pdf`：本次实际处理的只读原文快照；\n"
        "- `input/`：PageFacts、页面模板和翻译请求；\n"
        "- `output/`：千问译文、布局计划和候选 PDF；\n"
        "- `previews/`：原文、候选和并排渲染图；\n"
        "- `reports/`：Finding、生成证据、质量裁决、状态轨迹和最终结果；\n"
        "- `contracts/`：本页运行包合同。\n",
        encoding="utf-8",
    )
    trace: list[dict[str, str]] = []
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "PAGE_FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_page_template(facts)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "PAGE_TEMPLATE_READY", "owner": "page_template_builder"})

        units = tuple(TranslationUnit(item.container_id, item.source_text, item.reading_order) for item in template.containers)
        request = PageTranslationRequest(f"p3-{page_id}-en-zh", page_id, source_language, target_language, units)
        write_json(run_dir / "input" / "translation_request.json", request)
        translation = provider.translate(request)
        translation.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.json", translation)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, plan_findings = plan_layout(template, translation, font_file=font_file)
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
        trace.append({"state": "LAYOUT_PLAN_READY", "owner": "layout_planner"})
        if plan_findings:
            decision = ToolboxDecision(page_id, "PASS", "FAIL", "P3_PRODUCT_FAIL", plan_findings)
        else:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            render_findings, render_evidence = render_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            render_evidence = dict(render_evidence)
            render_evidence.update(
                {
                    "source_png": "previews/source.png",
                    "candidate_png": "previews/candidate.png",
                    "comparison_png": "previews/comparison.png",
                }
            )
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "pdf_renderer"})
            decision = judge_candidate(candidate_pdf=candidate_pdf, template=template, plan=plan, upstream_findings=render_findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": decision.terminal_state, "owner": "quality_judge"})
    except Exception as exc:
        failure_owner = _owner(trace)
        decision = ToolboxDecision(
            page_id,
            "FAIL",
            "NOT_REACHED",
            "P3_PROCESS_FAIL",
            (ToolboxFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc)),),
        )
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "P3_PROCESS_FAIL", "owner": failure_owner})
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P3RunResult(
        page_id,
        str(run_dir),
        str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        decision.process_verdict,
        decision.product_verdict,
        decision.terminal_state,
        failure_owner,
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _owner(trace: list[dict[str, str]]) -> str:
    if not trace:
        return "shared_pdf_kernel"
    last = trace[-1]["state"]
    return {
        "PAGE_FACTS_READY": "page_template_builder",
        "PAGE_TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "layout_planner",
        "LAYOUT_PLAN_READY": "pdf_renderer",
        "CANDIDATE_READY": "quality_judge",
    }.get(last, "p3_orchestrator")
