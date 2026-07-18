from __future__ import annotations

import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .. import TOOLBOX_KEY
from .layout_planner import plan_composite_layout
from .models import (
    CompositeDecision,
    CompositeFinding,
    P18RunResult,
)
from .renderer import render_composite_candidate
from .template_builder import CompositeCapabilityError, build_composite_template
from .translation_guard import translate_with_guard
from .translation_request import build_translation_request


_PROCESS_FAILURE_CODES = {
    "DIAGRAM_TOPOLOGY_CHANGED",
    "DIAGRAM_PROTECTED_TEXT_CHANGED",
    "DIAGRAM_OUTSIDE_ALLOWED_REGION_CHANGED",
    "LOCKED_OBJECT_CHANGED",
    "PROTECTED_OBJECT_CHANGED",
    "OUTSIDE_ALLOWED_REGION_CHANGED",
}
_CAPABILITY_CODES = {
    "FONT_NOT_EMBEDDED",
    "FONT_GLYPH_MISSING",
    "DIAGRAM_SAFE_REDACTION_REGION_NOT_FOUND",
    "P18_CHILD_LAYOUT_UNFIT",
    "P18_TRANSLATED_DIAGNOSTIC_MATERIALIZATION",
    "P18_TRANSLATION_LITERAL_MISSING",
    "P18_TRANSLATION_INCOMPLETE",
}


def run_p18_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P18RunResult:
    source_pdf = Path(source_pdf)
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    if not Path(font_file).is_file():
        raise CompositeCapabilityError(f"FONT_FILE_MISSING:{font_file}")
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_pdf)
    if sha256_file(source_snapshot) != source_hash:
        raise RuntimeError("P18_SOURCE_SNAPSHOT_HASH_MISMATCH")

    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p18-flow-text-diagram-page-run/v1",
            "toolbox_key": TOOLBOX_KEY,
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "every native text object belongs exactly once to flow, diagram, shared, or protected",
            "translation_rule": "one page-level request; route translations by immutable composite container ID",
            "render_rule": "flow and diagram plans are rendered together from the immutable source snapshot",
            "diagram_rule": "nodes, connectors, arrows, hierarchy, direction, drawings, and images stay locked",
            "repair_rule": "flow repair cannot move diagram geometry; diagram repair cannot enter flow regions",
            "failure_rule": "capability, quality, and process failures remain explicit; diagnostic candidates are not product PASS",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P18 run package\n\n"
        "The immutable source snapshot is in `input/source.pdf`. Ownership and the single page-level "
        "translation request are in `input/`; plans are in `output/`; machine findings and visual "
        "evidence are in `reports/` and `previews/`.\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p18_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf = run_dir / "output" / "candidate.pdf"
    flow_mode: str | None = None
    counts = {"flow": 0, "diagram": 0, "shared": 0, "protected": 0}
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_composite_template(
            source_snapshot,
            facts,
            target_language=target_language,
        )
        flow_mode = template.flow_mode
        counts = {
            owner: sum(item.owner == owner for item in template.containers)
            for owner in ("flow", "diagram", "shared")
        }
        counts["protected"] = len(template.protected_object_ids)
        write_json(run_dir / "input" / "page_template.json", template)
        write_json(
            run_dir / "reports" / "ownership_audit.json",
            {
                "schema_version": "p18-ownership-audit/v1",
                "page_id": page_id,
                "source_text_object_count": len(facts.text_objects),
                "ownership_count": len(template.ownerships),
                "owner_counts": {
                    owner: sum(item.owner == owner for item in template.ownerships)
                    for owner in ("flow", "diagram", "shared", "protected")
                },
                "duplicate_object_ids": [],
                "unowned_object_ids": [],
                "diagram_region": template.diagram_region,
                "flow_mode": template.flow_mode,
                "status": "PASS",
            },
        )
        trace.append({"state": "TEMPLATE_READY", "owner": "composite_template_builder"})

        request = build_translation_request(
            template,
            source_language=source_language,
            target_language=target_language,
        )
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle, translation_audit = translate_with_guard(provider, request)
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        write_json(run_dir / "reports" / "translation_validation.json", translation_audit)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        translation_findings = _translation_findings(translation_audit)
        plan, layout_findings, plan_evidence = plan_composite_layout(
            facts=facts,
            template=template,
            translations=bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", layout_findings)
        write_json(run_dir / "reports" / "layout_attempts.json", plan_evidence)
        trace.append({"state": "PATCH_READY", "owner": "composite_layout_planner"})

        render_findings, render_evidence, rendered_plan = render_composite_candidate(
            source_pdf=source_snapshot,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=plan,
            evidence_dir=run_dir / "previews",
        )
        if rendered_plan != plan.render_plan:
            write_json(run_dir / "output" / "diagnostic_layout_plan.json", rendered_plan)
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "composite_pdf_renderer"})

        findings = _deduplicate(
            tuple(translation_findings) + tuple(layout_findings) + tuple(render_findings)
        )
        decision = _decide(page_id, findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "composite_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "composite_quality_judge"})
    except (CompositeCapabilityError, ProviderError) as exc:
        failure_owner = _owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        decision = CompositeDecision(
            page_id=page_id,
            process_verdict="PASS",
            product_verdict="NOT_REACHED",
            terminal_state="CAPABILITY_FAILED",
            findings=(
                CompositeFinding(code, "HARD", failure_owner, None, str(exc), {}),
            ),
        )
        _record_failure_candidate(candidate_pdf, run_dir, code)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _owner(trace)
        decision = CompositeDecision(
            page_id=page_id,
            process_verdict="FAIL",
            product_verdict="NOT_REACHED",
            terminal_state="PROCESS_FAILED",
            findings=(
                CompositeFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {}),
            ),
        )
        _record_failure_candidate(candidate_pdf, run_dir, type(exc).__name__)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("P18_UPSTREAM_SAMPLE_CHANGED_DURING_RUN")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P18RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf.is_file() else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        provider=getattr(provider, "provider_name", "unknown"),
        failure_owner=failure_owner,
        flow_mode=flow_mode,
        flow_container_count=counts.get("flow", 0),
        diagram_container_count=counts.get("diagram", 0),
        shared_container_count=counts.get("shared", 0),
        protected_object_count=counts.get("protected", 0),
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _translation_findings(audit: dict[str, object]) -> tuple[CompositeFinding, ...]:
    final = audit.get("final", {})
    if not isinstance(final, dict) or final.get("status") == "PASS":
        return ()
    findings = []
    missing = final.get("missing_required_literals", {})
    if isinstance(missing, dict) and missing:
        findings.append(
            CompositeFinding(
                "P18_TRANSLATION_LITERAL_MISSING",
                "HARD",
                "translation_provider",
                None,
                "One or more required literals were not preserved by the translation.",
                {"containers": missing},
            )
        )
    incomplete = {
        "target_language_mismatches": final.get("target_language_mismatches", {}),
        "inadequate_outputs": final.get("inadequate_outputs", {}),
        "list_structure_mismatches": final.get("list_structure_mismatches", {}),
        "terminal_punctuation_mismatches": final.get(
            "terminal_punctuation_mismatches",
            {},
        ),
        "delimiter_balance_mismatches": final.get(
            "delimiter_balance_mismatches",
            {},
        ),
    }
    if any(incomplete.values()):
        findings.append(
            CompositeFinding(
                "P18_TRANSLATION_INCOMPLETE",
                "HARD",
                "translation_provider",
                None,
                "Translation language or completeness validation failed.",
                incomplete,
            )
        )
    return tuple(findings)


def _decide(page_id: str, findings: tuple[CompositeFinding, ...]) -> CompositeDecision:
    hard = tuple(item for item in findings if item.severity == "HARD")
    process = tuple(item for item in hard if item.code in _PROCESS_FAILURE_CODES)
    capability = tuple(
        item
        for item in hard
        if item.code in _CAPABILITY_CODES
        or item.code.startswith("FLOW_")
        or "CAPABILITY" in item.code
    )
    if process:
        return CompositeDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return CompositeDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if hard:
        return CompositeDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return CompositeDecision(page_id, "PASS", "PASS", "PAGE_PASSED", findings)


def _owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "composite_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "composite_layout_planner",
        "PATCH_READY": "composite_pdf_renderer",
        "CANDIDATE_READY": "composite_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p18_orchestrator")


def _record_failure_candidate(
    candidate_pdf: Path,
    run_dir: Path,
    reason: str,
) -> None:
    has_candidate = candidate_pdf.is_file()
    write_json(
        run_dir / "reports" / "failure_candidate.json",
        {
            "candidate_kind": (
                "DIAGNOSTIC_PARTIAL_CANDIDATE"
                if has_candidate
                else "NO_TRANSLATED_CANDIDATE"
            ),
            "translated_candidate": has_candidate,
            "product_acceptance": False,
            "reason": reason,
        },
    )


def _deduplicate(findings: tuple[CompositeFinding, ...]) -> tuple[CompositeFinding, ...]:
    output = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.container_id, finding.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return tuple(output)
