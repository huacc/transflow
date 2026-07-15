from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = ROOT / "toolboxes" / "end"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import PageTranslationBundle, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, QwenConfig, QwenPageTranslationProvider
from toolboxes.end.tools.engine import run_p10_page


class FixtureTranslationProvider:
    provider_name = "fixed"
    model_name = "p10-human-authored-layout-fixture"

    def __init__(self, catalog_path: Path) -> None:
        self.catalog = json.loads(catalog_path.read_text(encoding="utf-8"))["pages"]

    def translate(self, request):
        values = self.catalog.get(request.page_id)
        if not isinstance(values, list) or len(values) != len(request.units):
            raise ProviderError("FIXED_TRANSLATION_MISSING")
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(
                TranslationResult(unit.container_id, str(values[index]))
                for index, unit in enumerate(request.units)
            ),
        )
        bundle.validate_against(request)
        return bundle


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "p10-recorded-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self.bundle_path = bundle_path

    def translate(self, request):
        if not self.bundle_path.is_file():
            raise ProviderError("RECORDED_TRANSLATION_MISSING")
        value = json.loads(self.bundle_path.read_text(encoding="utf-8"))
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=str(value.get("model") or self.model_name),
            translations=tuple(
                TranslationResult(str(item["container_id"]), str(item["translated_text"]))
                for item in value["translations"]
            ),
            response_sha256=value.get("response_sha256"),
        )
        bundle.validate_against(request)
        return bundle


class LazyQwenProvider:
    provider_name = "qwen"
    model_name = os.environ.get("PAGE_TOOLBOX_QWEN_MODEL", "Qwen/Qwen3.6-35B-A3B")

    def __init__(self, prompt_path: Path) -> None:
        self.prompt_path = prompt_path

    def translate(self, request):
        provider = QwenPageTranslationProvider(
            QwenConfig.from_environment(),
            self.prompt_path.read_text(encoding="utf-8"),
        )
        return provider.translate(request)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P10 end page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--provider", choices=("qwen", "fixed", "recorded"), default="qwen")
    parser.add_argument("--recorded-run", type=Path)
    parser.add_argument("--allow-holdout", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    args = parser.parse_args()

    records = _read_manifest(TOOLBOX_ROOT / "samples" / "manifest.jsonl")
    if args.sample_id:
        requested = set(args.sample_id)
        selected = [record for record in records if record["sample_id"] in requested]
        missing = requested - {record["sample_id"] for record in selected}
        if missing:
            parser.error("unknown sample IDs: " + ",".join(sorted(missing)))
    elif args.initial:
        selected = [record for record in records if record.get("validation_phase") == "initial"]
    else:
        selected = records
    if any(record["split"] == "holdout" for record in selected) and not args.allow_holdout:
        parser.error("holdout selection requires --allow-holdout")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("p10-%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("cases", "reports", "input"):
        (run_root / name).mkdir()
    shutil.copy2(TOOLBOX_ROOT / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "end",
            "run_id": run_id,
            "provider": args.provider,
            "sample_ids": [record["sample_id"] for record in selected],
            "holdout_accessed": any(record["split"] == "holdout" for record in selected),
            "holdout_integrity": "NON_BLIND_PREVIEWED_BEFORE_PARTITION",
        },
    )

    results = []
    for record in selected:
        sample_id = record["sample_id"]
        source = TOOLBOX_ROOT / record["source_ref"]
        if sha256_file(source) != record["sha256"]:
            raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
        provider = _provider(args.provider, sample_id, record, args.recorded_run)
        result = run_p10_page(
            source_pdf=source,
            page_id=sample_id,
            run_dir=run_root / "cases" / sample_id,
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=record["source_language"],
            target_language=record["target_language"],
        )
        results.append(result)
        print(
            json.dumps(
                {
                    "sample_id": sample_id,
                    "mode": result.mode,
                    "process": result.process_verdict,
                    "product": result.product_verdict,
                    "state": result.terminal_state,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    states = sorted({result.terminal_state for result in results})
    summary = {
        "schema_version": "p10-end-batch/v1",
        "toolbox_key": "end",
        "run_id": run_id,
        "provider": args.provider,
        "sample_count": len(results),
        "process_pass_count": sum(result.process_verdict == "PASS" for result in results),
        "product_pass_count": sum(result.product_verdict == "PASS" for result in results),
        "translated_page_count": sum(result.mode == "translated" for result in results),
        "passthrough_page_count": sum(result.mode == "passthrough" for result in results),
        "terminal_states": {state: sum(result.terminal_state == state for result in results) for state in states},
        "results": results,
    }
    write_json(run_root / "reports" / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, default=lambda value: value.__dict__, indent=2))
    return 0 if all(result.terminal_state == "PAGE_PASSED" for result in results) else 2


def _provider(kind: str, sample_id: str, record: dict[str, object], recorded_run: Path | None):
    if kind == "fixed":
        return FixtureTranslationProvider(TOOLBOX_ROOT / "fixtures" / "fixed_translations.json")
    if kind == "recorded":
        assert recorded_run is not None
        return RecordedTranslationProvider(recorded_run / "cases" / sample_id / "output" / "translation_bundle.json")
    prompt_name = (
        "page_translation.en-zh.zh-CN.md"
        if str(record["source_language"]).startswith("en")
        else "page_translation.zh-en.zh-CN.md"
    )
    return LazyQwenProvider(TOOLBOX_ROOT / "prompts" / prompt_name)


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
