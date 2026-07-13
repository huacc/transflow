from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_table_layout
from .models import TableDecision, TableFinding
from .renderer import render_table_candidate
from .template_builder import TableCapabilityError, build_table_template


@dataclass(frozen=True)
class P6RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None


_PROCESS_FAILURE_CODES = {
    "TABLE_LOCKED_OBJECT_CHANGED",
    "PROTECTED_CELL_CHANGED",
    "OUTSIDE_ALLOWED_REGION_CHANGED",
}


def run_p6_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P6RunResult:
    if not Path(font_file).is_file():
        raise TableCapabilityError(f"font_file_missing:{font_file}")
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_pdf)
    if sha256_file(source_snapshot) != source_hash:
        raise RuntimeError("source_snapshot_hash_mismatch")
    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p6-body-table-page-run/v1",
            "toolbox_key": "body.table",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "table_rule": "column and merged-cell ownership are immutable; translated text stays inside its cell and clear of retained table rules",
            "protected_cell_rule": "pure numeric, currency, date and placeholder cells are neither translated nor redrawn",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P6 body.table 运行包\n\n"
        "原页快照位于 `input/source.pdf`；表格结构和翻译请求位于 `input/`；"
        "译文、布局与候选位于 `output/`；机械证据、Finding 和双裁决位于 `reports/`。\n",
        encoding="utf-8",
    )
    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p6_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_table_template(source_snapshot, facts)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "table_template_builder"})

        units = tuple(
            TranslationUnit(cell.container_id, cell.source_text, index)
            for index, cell in enumerate(template.translatable_cells)
        )
        request = PageTranslationRequest(
            f"p6-{page_id}-{source_language}-{target_language}",
            page_id,
            source_language,
            target_language,
            units,
        )
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, plan_findings = plan_table_layout(
            template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
        trace.append({"state": "PATCH_READY", "owner": "table_layout_planner"})
        if plan_findings:
            decision = TableDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", plan_findings)
        else:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            render_findings, render_evidence = render_table_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            evidence = dict(render_evidence)
            evidence.update(
                {
                    "source_png": "previews/source.png",
                    "candidate_png": "previews/candidate.png",
                    "comparison_png": "previews/comparison.png",
                }
            )
            write_json(run_dir / "reports" / "render_evidence.json", evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "table_pdf_renderer"})
            process_findings = tuple(item for item in render_findings if item.code in _PROCESS_FAILURE_CODES)
            capability_findings = tuple(item for item in render_findings if item.code == "FONT_NOT_EMBEDDED")
            product_findings = tuple(item for item in render_findings if item not in process_findings and item not in capability_findings)
            if process_findings:
                decision = TableDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", render_findings)
            elif capability_findings:
                decision = TableDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", render_findings)
            elif product_findings:
                decision = TableDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", render_findings)
            else:
                decision = TableDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "table_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "table_quality_judge"})
    except (TableCapabilityError, ProviderError) as exc:
        failure_owner = _owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = TableFinding(code, "HARD", failure_owner, None, str(exc), {})
        decision = TableDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _owner(trace)
        finding = TableFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {})
        decision = TableDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P6RunResult(
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
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "table_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "table_layout_planner",
        "PATCH_READY": "table_pdf_renderer",
        "CANDIDATE_READY": "table_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p6_orchestrator")
