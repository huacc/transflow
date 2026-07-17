from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationBundle, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import (
    FixedTranslationProvider,
    ProviderError,
    QwenConfig,
)
from toolboxes.body.chart.tools.run import ValidatedQwenRetryProvider

from .engine import run_p16_page


TOOLBOX = Path(__file__).resolve().parents[1]


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "recorded-p16-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self.bundle_path = bundle_path

    def translate(self, request):
        bundle_path = self.bundle_path
        if not bundle_path.exists():
            raw_bundle = bundle_path.with_name("translation_bundle.raw.json")
            if raw_bundle.exists():
                bundle_path = raw_bundle
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=str(payload.get("model") or self.model_name),
            translations=tuple(
                TranslationResult(str(item["container_id"]), str(item["translated_text"]))
                for item in payload["translations"]
            ),
            provider_request_id=payload.get("provider_request_id"),
            latency_ms=payload.get("latency_ms"),
            response_sha256=payload.get("response_sha256"),
        )
        bundle.validate_against(request)
        return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P16 body.composite.chart_table page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true")
    selection.add_argument("--initial-expansion", action="store_true")
    selection.add_argument("--non-holdout", action="store_true")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--provider", choices=("fixed", "qwen", "recorded"), required=True)
    parser.add_argument("--fixed-translations", type=Path)
    parser.add_argument("--recorded-run", type=Path)
    parser.add_argument("--allow-holdout", action="store_true")
    parser.add_argument("--final-validation", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    args = parser.parse_args()

    records = _read_manifest(TOOLBOX / "samples" / "manifest.jsonl")
    selected = _select(parser, records, args)
    holdout_accessed = any(record["split"] == "holdout" for record in selected)
    if holdout_accessed and not (args.allow_holdout and args.final_validation):
        parser.error("holdout selection requires both --allow-holdout and --final-validation")
    if args.provider == "fixed" and args.fixed_translations is None:
        parser.error("--provider fixed requires --fixed-translations")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")

    run_root = TOOLBOX / "runs" / args.run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("cases", "input", "reports"):
        (run_root / name).mkdir()
    shutil.copy2(TOOLBOX / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "body.composite.chart_table",
            "run_id": args.run_id,
            "provider": args.provider,
            "sample_ids": [record["sample_id"] for record in selected],
            "holdout_accessed": holdout_accessed,
            "holdout_purpose": "final_validation" if holdout_accessed else None,
        },
    )

    fixed = _read_fixed(args.fixed_translations) if args.provider == "fixed" else {}
    qwen_config = None
    if args.provider == "qwen":
        try:
            qwen_config = QwenConfig.from_environment()
        except ProviderError as exc:
            write_json(
                run_root / "reports" / "batch_result.json",
                {
                    "terminal_state": "CAPABILITY_FAILED",
                    "process_verdict": "PASS",
                    "product_verdict": "NOT_REACHED",
                    "error_code": exc.code,
                    "sample_count": 0,
                    "requested_sample_count": len(selected),
                },
            )
            print(json.dumps({"state": "CAPABILITY_FAILED", "error_code": exc.code}, ensure_ascii=False))
            return 3

    results = []
    for record in selected:
        sample_id = str(record["sample_id"])
        source = TOOLBOX / str(record["source_ref"])
        if sha256_file(source) != record["sha256"]:
            raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
        if args.provider == "fixed":
            if sample_id not in fixed:
                raise RuntimeError(f"fixed_translation_missing:{sample_id}")
            provider = FixedTranslationProvider(fixed[sample_id])
        elif args.provider == "recorded":
            provider = RecordedTranslationProvider(
                args.recorded_run / "cases" / sample_id / "output" / "translation_bundle.json"
            )
        else:
            prompt_name = (
                "page_translation.en-zh.zh-CN.md"
                if str(record["source_language"]).startswith("en")
                else "page_translation.zh-en.zh-CN.md"
            )
            provider = ValidatedQwenRetryProvider(
                qwen_config,
                (TOOLBOX / "prompts" / prompt_name).read_text(encoding="utf-8"),
            )
        result = run_p16_page(
            source_pdf=source,
            page_id=sample_id,
            run_dir=run_root / "cases" / sample_id,
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=str(record["source_language"]),
            target_language=str(record["target_language"]),
        )
        results.append(result)
        print(
            json.dumps(
                {
                    "sample_id": sample_id,
                    "process": result.process_verdict,
                    "product": result.product_verdict,
                    "state": result.terminal_state,
                    "failure_owner": result.failure_owner,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    state_counts: dict[str, int] = {}
    for result in results:
        state_counts[result.terminal_state] = state_counts.get(result.terminal_state, 0) + 1
    passed = state_counts.get("PAGE_PASSED", 0)
    write_json(
        run_root / "reports" / "batch_result.json",
        {
            "terminal_state": "BATCH_PASSED" if passed == len(results) else "BATCH_FAILED",
            "provider": args.provider,
            "sample_count": len(results),
            "passed_count": passed,
            "state_counts": state_counts,
            "holdout_accessed": holdout_accessed,
            "formal_promotion_eligible": False,
            "promotion_blockers": ["P6_GATE_NOT_PROMOTED", "P13_GATE_NOT_PROMOTED"],
        },
    )
    return 0 if passed == len(results) else 2


def _select(parser, records, args):
    if args.sample_id:
        requested = set(args.sample_id)
        selected = [record for record in records if record["sample_id"] in requested]
        missing = requested - {record["sample_id"] for record in selected}
        if missing:
            parser.error("unknown sample IDs: " + ",".join(sorted(missing)))
        return selected
    if args.initial:
        return [record for record in records if record["validation_phase"] == "initial"]
    if args.initial_expansion:
        return [record for record in records if record["validation_phase"] == "initial_expansion"]
    if args.non_holdout:
        return [record for record in records if record["split"] != "holdout"]
    return records


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_fixed(path: Path) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(sample): {str(container_id): str(value) for container_id, value in translations.items()}
        for sample, translations in payload.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
