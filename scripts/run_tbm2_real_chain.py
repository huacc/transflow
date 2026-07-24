"""Run one TBM2 page through the real TranslationPort and production patch path."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pymupdf

from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
)
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.jobs import DocumentRunRequest
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.toolboxes.composites import (
    FlowTextChartToolbox,
    FlowTextDiagramToolbox,
    FreeformToolbox,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
ROUTES = {
    "body.composite.flow_text_chart",
    "body.composite.flow_text_diagram",
    "body.freeform",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request(source: Path, run_id: str) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(source),
        source_hash=_sha256_file(source),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="b" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )


def _toolbox(route: str, source: Path):
    policy = load_p8_toolbox_policy(POLICY_PATH)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    font_path = fonts.resolve(policy.font_id).path
    if route == "body.composite.flow_text_chart":
        return FlowTextChartToolbox(policy, font_path)
    if route == "body.composite.flow_text_diagram":
        return FlowTextDiagramToolbox(policy, font_path, source)
    return FreeformToolbox(policy, font_path, source)


def _materialize(
    source: Path,
    output_dir: Path,
    work: ToolboxPageWork,
    result,
) -> tuple[str | None, bool]:
    patch = result.patch
    if patch is not None:
        candidate = output_dir / "candidate.pdf"
        fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
        with pymupdf.open(source) as document:
            application = PagePatchInterpreter(fonts).apply(
                document,
                work.context,
                work.facts,
                patch,
                patch.owner,
            )
            document.save(candidate)
        return str(candidate.relative_to(REPO_ROOT)), application.fits
    proposed = result.proposed_patch
    if proposed is None:
        return None, False
    diagnostic = output_dir / "failure-diagnostic.pdf"
    with pymupdf.open(source) as document:
        page = document[0]
        for operation in proposed.operations:
            if operation.rect is not None:
                page.draw_rect(
                    pymupdf.Rect(operation.rect),
                    color=(1, 0, 0),
                    width=1.2,
                    overlay=True,
                )
        metadata = document.metadata
        metadata["subject"] = "TBM2 FAILURE DIAGNOSTIC - NOT A CANDIDATE"
        document.set_metadata(metadata)
        document.save(diagnostic)
    return str(diagnostic.relative_to(REPO_ROOT)), False


def run(route: str, source: Path, output_dir: Path) -> dict[str, object]:
    source = source.resolve()
    output_dir = output_dir.resolve()
    if route not in ROUTES:
        raise ValueError(f"unsupported_route:{route}")
    if not source.is_file():
        raise FileNotFoundError(source)
    output_dir.mkdir(parents=True, exist_ok=False)
    run_id = output_dir.name
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(
        _request(source, run_id)
    )[0]
    toolbox = _toolbox(route, source)
    work = ToolboxPageWork(page.context, page.facts, toolbox)
    adapter = MigrationQwenTranslationAdapter(
        timeout_seconds=240.0,
        chunk_size=24,
    )
    result = ToolboxPageCoordinator(adapter).execute(work)
    artifact, fits = _materialize(source, output_dir, work, result)
    summary = {
        "schema_version": "transflow.tbm2-real-chain/v1",
        "route": route,
        "run_id": run_id,
        "source": str(source.relative_to(REPO_ROOT)),
        "source_sha256": _sha256_file(source),
        "translation_port_call_count": adapter.call_count,
        "ordered_unit_count": len(result.ordered_unit_ids),
        "root_owner": (
            result.patch.owner
            if result.patch is not None
            else result.proposed_patch.owner
            if result.proposed_patch is not None
            else route
        ),
        "verdict": str(result.verdict.disposition),
        "outcome": asdict(result.outcome),
        "finding_codes": [item.code for item in result.findings],
        "artifact": artifact,
        "materialized_patch_fits": fits,
        "product_acceptance": "NOT_EVALUATED",
    }
    (output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", required=True, choices=sorted(ROUTES))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = run(args.route, args.source, args.output_dir)
    print(
        "TBM2_REAL_CHAIN "
        f"route={summary['route']} "
        f"verdict={summary['verdict']} "
        f"calls={summary['translation_port_call_count']} "
        f"fits={summary['materialized_patch_fits']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
