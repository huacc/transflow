from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import PageFacts, PageTemplate, PageTranslationRequest, TranslationUnit
from .runtime import run_translation_slice, sha256_text
from .sample_snapshot import snapshot_sample
from .translation import FixedTranslationProvider, QwenConfig, QwenPageTranslationProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the P1 translation-ready vertical slice")
    parser.add_argument("--provider", choices=("fixed", "qwen"), required=True)
    parser.add_argument("--run-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    repo_root = project_root.parents[1]
    fixture = json.loads((project_root / "fixtures" / "p1" / "S2P0043_contract_fixture.json").read_text(encoding="utf-8"))
    units = tuple(TranslationUnit(**row) for row in fixture["units"])

    sample = snapshot_sample(
        repo_root=repo_root,
        project_root=project_root,
        source_pdf=repo_root / fixture["sample"]["upstream_pdf"],
        sample_id=fixture["sample"]["sample_id"],
        classification_path=fixture["sample"]["classification_path"],
        leaf_key=fixture["sample"]["leaf_key"],
        original_document_id=fixture["sample"]["original_document_id"],
        original_page_number=fixture["sample"]["original_page_number"],
        source_document_sha256=fixture["sample"]["source_document_sha256"],
        expected_source_sha256=fixture["sample"]["upstream_sha256"],
    )
    page_facts = PageFacts(**fixture["page_facts"])
    page_template = PageTemplate(page_id=sample.sample_id, toolbox_key=sample.leaf_key, containers=units)
    request = PageTranslationRequest(
        request_id=fixture["request"]["request_id"],
        page_id=sample.sample_id,
        source_language=fixture["request"]["source_language"],
        target_language=fixture["request"]["target_language"],
        units=units,
    )
    prompt_text = (project_root / "prompts" / "page_translation.zh-CN.md").read_text(encoding="utf-8")
    if args.provider == "fixed":
        provider = FixedTranslationProvider({row["container_id"]: row["translated_text"] for row in fixture["fixed_translations"]})
    else:
        provider = QwenPageTranslationProvider(QwenConfig.from_environment(), prompt_text)

    result = run_translation_slice(
        project_root=project_root,
        sample=sample,
        page_facts=page_facts,
        page_template=page_template,
        request=request,
        provider=provider,
        prompt_sha256=sha256_text(prompt_text),
        run_id=args.run_id,
    )
    print(json.dumps({
        "run_id": result.run_id,
        "run_dir": result.run_dir.relative_to(project_root).as_posix(),
        "terminal_state": result.terminal_state,
        "process_verdict": result.process_verdict,
        "product_verdict": result.product_verdict,
        "error_code": result.error_code,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.terminal_state == "TRANSLATION_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

