from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
    write_json,
)
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import (
    FixedTranslationProvider,
    ProviderError,
    QwenConfig,
    QwenPageTranslationProvider,
)

from .engine import run_p12_page, translation_validation


TOOLBOX = Path(__file__).resolve().parents[1]


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "recorded-p12-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self.bundle_path = bundle_path

    def translate(self, request):
        payload = json.loads(self.bundle_path.read_text(encoding="utf-8"))
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider="recorded",
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


class ValidatedQwenRetryProvider:
    provider_name = "qwen-validated-retry"

    def __init__(self, config: QwenConfig, prompt_text: str) -> None:
        self.config = config
        self.prompt_text = prompt_text
        self.model_name = config.model
        self.primary = QwenPageTranslationProvider(config, prompt_text)

    def translate(self, request):
        first = self.primary.translate(request)
        validation = translation_validation(request, first)
        if validation["status"] == "PASS":
            return first
        failed_ids = _failed_container_ids(validation)
        retry_request = PageTranslationRequest(
            request_id=request.request_id,
            page_id=request.page_id,
            source_language=request.source_language,
            target_language=request.target_language,
            units=tuple(
                _retry_unit(unit, validation)
                for unit in request.units
                if unit.container_id in failed_ids
            ),
        )
        retry_prompt = self.prompt_text + "\n\n" + _retry_appendix(validation, first)
        second = QwenPageTranslationProvider(self.config, retry_prompt).translate(retry_request)
        repaired_by_id = {item.container_id: item for item in second.translations}
        repaired = PageTranslationBundle(
            request_id=second.request_id,
            page_id=second.page_id,
            provider=self.provider_name,
            model=second.model,
            translations=tuple(repaired_by_id.get(item.container_id, item) for item in first.translations),
            provider_request_id=",".join(
                item for item in (first.provider_request_id, second.provider_request_id) if item
            ) or None,
            latency_ms=(first.latency_ms or 0) + (second.latency_ms or 0),
            response_sha256=hashlib.sha256(
                ((first.response_sha256 or "") + (second.response_sha256 or "")).encode("ascii")
            ).hexdigest(),
        )
        repaired.validate_against(request)
        return repaired


def _failed_container_ids(validation: dict[str, object]) -> set[str]:
    failed: set[str] = set()
    for key in ("missing_required_literals", "source_language_residue"):
        value = validation.get(key)
        if isinstance(value, dict):
            failed.update(str(container_id) for container_id in value)
    incomplete = validation.get("structurally_incomplete_translations")
    if isinstance(incomplete, list):
        failed.update(str(container_id) for container_id in incomplete)
    return failed


def _retry_unit(unit, validation: dict[str, object]):
    requirements: list[str] = []
    missing = validation.get("missing_required_literals")
    if isinstance(missing, dict) and unit.container_id in missing:
        requirements.append(
            "MANDATORY_LITERAL_SUBSTRINGS="
            + json.dumps(missing[unit.container_id], ensure_ascii=False)
        )
    residue = validation.get("source_language_residue")
    if isinstance(residue, dict) and unit.container_id in residue:
        requirements.append("NO_SOURCE_LANGUAGE_CHARACTERS")
    incomplete = validation.get("structurally_incomplete_translations")
    if isinstance(incomplete, list) and unit.container_id in incomplete:
        requirements.append("COMPLETE_ALL_SENTENCES_PARENTHESES_AND_QUOTATIONS")
    instruction = (
        "[MACHINE_REPAIR_CONSTRAINT; this is an instruction, not source content; "
        "do not copy it into translated_text: " + "; ".join(requirements) + "]"
    )
    return replace(unit, source_text=unit.source_text + "\n\n" + instruction)


def _retry_appendix(validation: dict[str, object], first: PageTranslationBundle) -> str:
    return (
        "# 强制复核重试\n\n"
        "上轮译文未通过机械校验。重新翻译完整请求，并在返回前逐 ID 自检："
        "required_literals 必须逐字符原样出现；英文目标不得残留中文；每句话、括号和引语必须完整。"
        "不得换算数值；量纲翻译会引起换算时保留原数字并直译量纲，例如 N亿元写作 RMB N hundred million；"
        "数字月份用数字日期或 month M，不得只写月份名称。英文引语只用单引号，避免双引号截断。"
        "机械验收条件：missing_required_literals 中每个 token 都必须是对应 translated_text 的连续子串；"
        "输出前按 Python 的 token in translated_text 语义逐项检查，这不是可选建议。\n\n"
        "上轮校验：" + json.dumps(validation, ensure_ascii=False, sort_keys=True) + "\n\n"
        "上轮译文（仅用于定位错误；只重新输出本次请求内的失败 ID）："
        + json.dumps(
            [
                {"container_id": item.container_id, "translated_text": item.translated_text}
                for item in first.translations
                if item.container_id in _failed_container_ids(validation)
            ],
            ensure_ascii=False,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P12 body.flow_text.visual_anchored page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true")
    selection.add_argument("--initial-expansion", action="store_true")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--provider", choices=("fixed", "qwen", "recorded"), required=True)
    parser.add_argument("--fixed-translations", type=Path, default=TOOLBOX / "fixtures" / "fixed_translations.json")
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
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")

    run_root = TOOLBOX / "runs" / args.run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("cases", "reports", "input"):
        (run_root / name).mkdir()
    shutil.copy2(TOOLBOX / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "body.flow_text.visual_anchored",
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
            provider = RecordedTranslationProvider(args.recorded_run / "cases" / sample_id / "output" / "translation_bundle.json")
        else:
            prompt = "page_translation.en-zh.zh-CN.md" if str(record["source_language"]).startswith("en") else "page_translation.zh-en.zh-CN.md"
            provider = ValidatedQwenRetryProvider(
                qwen_config,
                (TOOLBOX / "prompts" / prompt).read_text(encoding="utf-8"),
            )
        result = run_p12_page(
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

    passed = sum(result.terminal_state == "PAGE_PASSED" for result in results)
    states: dict[str, int] = {}
    for result in results:
        states[result.terminal_state] = states.get(result.terminal_state, 0) + 1
    write_json(
        run_root / "reports" / "batch_result.json",
        {
            "terminal_state": "BATCH_PASSED" if passed == len(results) else "BATCH_FAILED",
            "provider": args.provider,
            "sample_count": len(results),
            "passed_count": passed,
            "state_counts": states,
            "holdout_accessed": holdout_accessed,
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
        return [record for record in records if record.get("validation_phase") == "initial"]
    if args.initial_expansion:
        return [record for record in records if record.get("validation_phase") in {"initial", "initial_expansion"}]
    return records


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_fixed(path: Path) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(sample): {str(key): str(value) for key, value in rows.items()} for sample, rows in payload.items()}


if __name__ == "__main__":
    raise SystemExit(main())
