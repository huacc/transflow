from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    TranslationResult,
    write_json,
)
from page_toolbox_puncture.translation import ProviderError, QwenConfig, QwenPageTranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.composite.flow_text_table.tools.engine import run_p7_page


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = (
    ROOT
    / "toolboxes"
    / "body"
    / "composite"
    / "flow_text_table"
    / "prompts"
    / "page_translation.zh-CN.md"
)


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "p7-recorded-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self._value = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.model_name = str(self._value.get("model") or self.model_name)

    def translate(self, request):
        recorded = {
            str(item["container_id"]): str(item["translated_text"])
            for item in self._value["translations"]
        }
        if any(unit.container_id not in recorded for unit in request.units):
            raise ProviderError("RECORDED_TRANSLATION_MISSING")
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(
                TranslationResult(unit.container_id, recorded[unit.container_id])
                for unit in request.units
            ),
            response_sha256=self._value.get("response_sha256"),
        )
        bundle.validate_against(request)
        return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run every classified P7 PDF as one numbered batch")
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--provider", choices=("qwen", "recorded"), default="qwen")
    parser.add_argument("--recorded-run", type=Path)
    args = parser.parse_args()

    source_pdfs = tuple(sorted(args.source_dir.glob("*.pdf")))
    if not source_pdfs:
        raise SystemExit("no_pdf_samples_found")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")
    args.run_dir.mkdir(parents=True, exist_ok=False)
    (args.run_dir / "reports").mkdir()
    live_provider = None
    if args.provider == "qwen":
        live_provider = QwenPageTranslationProvider(
            QwenConfig.from_environment(),
            args.prompt.read_text(encoding="utf-8"),
        )
    summary_model = live_provider.model_name if live_provider else "p7-recorded-bundle"

    records = []
    for index, source_pdf in enumerate(source_pdfs, start=1):
        page_id = f"P7-{source_pdf.stem}"
        facts = extract_page_facts(source_pdf, page_id=page_id)
        source_language, language_evidence = _source_language(facts)
        target_language = "en" if source_language == "zh-CN" else "zh-CN"
        print(
            f"[{index:02d}/{len(source_pdfs):02d}] {source_pdf.stem} "
            f"{source_language}->{target_language}",
            flush=True,
        )
        try:
            provider = live_provider or RecordedTranslationProvider(
                args.recorded_run / source_pdf.stem / "output" / "translation_bundle.json"
            )
            result = run_p7_page(
                source_pdf=source_pdf,
                page_id=page_id,
                run_dir=args.run_dir / source_pdf.stem,
                provider=provider,
                font_file=args.font_file,
                bold_font_file=args.bold_font_file,
                source_language=source_language,
                target_language=target_language,
            )
            record = {
                **asdict(result),
                "sample_id": source_pdf.stem,
                "source_pdf": str(source_pdf.resolve()),
                "source_language": source_language,
                "target_language": target_language,
                "language_evidence": language_evidence,
            }
        except Exception as exc:
            record = {
                "sample_id": source_pdf.stem,
                "source_pdf": str(source_pdf.resolve()),
                "source_language": source_language,
                "target_language": target_language,
                "language_evidence": language_evidence,
                "process_verdict": "FAIL",
                "product_verdict": "NOT_REACHED",
                "terminal_state": "BATCH_CASE_EXCEPTION",
                "failure_owner": "batch_runner",
                "error_code": type(exc).__name__,
                "error_message": str(exc),
            }
        records.append(record)
        print(
            f"  -> {record['process_verdict']}/{record['product_verdict']} "
            f"{record['terminal_state']}",
            flush=True,
        )

    passed = sum(record["terminal_state"] == "PAGE_PASSED" for record in records)
    summary = {
        "schema_version": "p7-body-composite-flow-text-table-batch/v1",
        "run_id": args.run_dir.name,
        "sample_count": len(records),
        "passed_count": passed,
        "failed_count": len(records) - passed,
        "all_passed": passed == len(records),
        "provider": args.provider,
        "model": summary_model,
        "cases": records,
    }
    write_json(args.run_dir / "reports" / "batch_summary.json", summary)
    print(json.dumps({key: summary[key] for key in ("run_id", "sample_count", "passed_count", "failed_count", "all_passed")}, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


def _source_language(facts) -> tuple[str, dict[str, object]]:
    text = "".join(item.text for item in facts.text_objects)
    han_count = sum("\u3400" <= character <= "\u9fff" for character in text)
    latin_count = sum(character.isascii() and character.isalpha() for character in text)
    source_language = "zh-CN" if han_count >= 20 and han_count >= latin_count * 0.15 else "en"
    return source_language, {
        "method": "native_text_han_latin_ratio/v1",
        "han_character_count": han_count,
        "latin_character_count": latin_count,
        "han_to_latin_ratio": round(han_count / max(latin_count, 1), 6),
    }


if __name__ == "__main__":
    raise SystemExit(main())
