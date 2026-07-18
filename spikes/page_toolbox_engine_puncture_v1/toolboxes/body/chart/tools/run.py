from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationBundle, PageTranslationRequest, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import (
    FixedTranslationProvider,
    ProviderError,
    QwenConfig,
    QwenPageTranslationProvider,
)

from .engine import run_p13_page, translation_validation


TOOLBOX = Path(__file__).resolve().parents[1]


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "recorded-p13-bundle"

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
        self.last_audit: dict[str, object] = {"status": "NOT_RUN"}

    def translate(self, request):
        self.transient_retry_count = 0
        current = self._translate_with_transient_retry(self.primary, request)
        observed_texts = {
            item.container_id: [item.translated_text]
            for item in current.translations
        }
        retried_ids: set[str] = set()
        provider_request_ids = [current.provider_request_id] if current.provider_request_id else []
        response_hashes = [current.response_sha256] if current.response_sha256 else []
        latency_ms = current.latency_ms or 0
        retry_prompt = self.prompt_text + (
            "\n\n# 机械失败定点重试\n"
            "只返回本次请求中的失败 ID。翻译标记以外的全部语义，逐字符原样复制 [[P13_KEEP_n]] 标记并保持其语义位置。"
            "每个输入标记必须在对应译文中恰好出现一次。每个 ID 只翻译自己的源文，"
            "不得把一个合并后的长译文复制给多个不同 ID。不得输出问号占位符或布局信息。"
        )
        for _ in range(2):
            validation = translation_validation(request, current)
            if validation["status"] == "PASS":
                self.last_audit = {
                    "status": "PASS",
                    "retried_container_ids": sorted(retried_ids),
                    "segmented_container_ids": [],
                    "confirmed_proper_name_ids": [],
                    "transient_retry_count": self.transient_retry_count,
                }
                return current
            failed_ids = _failed_container_ids(validation)
            retried_ids.update(failed_ids)
            retry_units = []
            markers_by_id: dict[str, dict[str, str]] = {}
            for unit in request.units:
                if unit.container_id not in failed_ids:
                    continue
                markers: dict[str, str] = {}
                marker_by_literal: dict[str, str] = {}
                for index, literal in enumerate(unit.required_literals):
                    marker = f"[[P13_KEEP_{index}]]"
                    markers[marker] = literal
                    marker_by_literal[literal] = marker
                if marker_by_literal:
                    pattern = re.compile(
                        "|".join(re.escape(literal) for literal in sorted(marker_by_literal, key=len, reverse=True))
                    )
                    masked_text = pattern.sub(lambda match: marker_by_literal[match.group(0)], unit.source_text)
                else:
                    masked_text = unit.source_text
                markers_by_id[unit.container_id] = markers
                retry_units.append(replace(unit, source_text=masked_text, required_literals=()))
            if not retry_units:
                break
            retry_request = PageTranslationRequest(
                request_id=request.request_id,
                page_id=request.page_id,
                source_language=request.source_language,
                target_language=request.target_language,
                units=tuple(retry_units),
            )
            retried = self._translate_with_transient_retry(
                QwenPageTranslationProvider(self.config, retry_prompt),
                retry_request,
            )
            if retried.provider_request_id:
                provider_request_ids.append(retried.provider_request_id)
            if retried.response_sha256:
                response_hashes.append(retried.response_sha256)
            latency_ms += retried.latency_ms or 0
            repaired_by_id = {}
            for item in retried.translations:
                translated_text = item.translated_text
                for marker, literal in markers_by_id[item.container_id].items():
                    translated_text = translated_text.replace(marker, literal)
                repaired_by_id[item.container_id] = replace(item, translated_text=translated_text)
                observed_texts.setdefault(item.container_id, []).append(translated_text)
            current = PageTranslationBundle(
                request_id=request.request_id,
                page_id=request.page_id,
                provider=self.provider_name,
                model=retried.model,
                translations=tuple(repaired_by_id.get(item.container_id, item) for item in current.translations),
                provider_request_id=",".join(provider_request_ids) or None,
                latency_ms=latency_ms,
                response_sha256=hashlib.sha256("".join(response_hashes).encode("ascii")).hexdigest(),
            )
            current.validate_against(request)
        final_validation = translation_validation(request, current)
        current, segmented_ids = self._retry_long_units_by_segment(
            request,
            current,
            final_validation,
            retry_prompt,
        )
        if segmented_ids:
            for item in current.translations:
                if item.container_id in segmented_ids:
                    observed_texts.setdefault(item.container_id, []).append(item.translated_text)
            final_validation = translation_validation(request, current)
        confirmed = _confirmed_retained_proper_name_ids(
            request,
            final_validation,
            observed_texts,
        )
        unconfirmed_residue = {
            container_id: values
            for container_id, values in final_validation["source_language_residue"].items()
            if container_id not in confirmed
        }
        self.last_audit = {
            "status": (
                "PASS"
                if not unconfirmed_residue
                and not final_validation["missing_required_literals"]
                and not final_validation["placeholder_outputs"]
                and not final_validation["inadequate_outputs"]
                and not final_validation["magnitude_unit_mismatches"]
                and not final_validation["cross_container_duplicate_outputs"]
                else "FAIL"
            ),
            "retried_container_ids": sorted(retried_ids),
            "segmented_container_ids": sorted(segmented_ids),
            "confirmed_proper_name_ids": confirmed,
            "transient_retry_count": self.transient_retry_count,
        }
        return current

    def _retry_long_units_by_segment(self, request, current, validation, retry_prompt):
        failed_ids = _failed_container_ids(validation)
        segment_units = []
        segment_ids_by_container: dict[str, list[str]] = {}
        markers_by_segment: dict[str, dict[str, str]] = {}
        for unit in request.units:
            if unit.container_id not in failed_ids or len(unit.source_text) < 80:
                continue
            segments = _translation_segments(unit.source_text)
            if len(segments) < 2:
                continue
            marker_by_literal = {
                literal: f"[[P13_KEEP_{index}]]"
                for index, literal in enumerate(unit.required_literals)
            }
            pattern = (
                re.compile("|".join(re.escape(literal) for literal in sorted(marker_by_literal, key=len, reverse=True)))
                if marker_by_literal
                else None
            )
            segment_ids = []
            for index, segment in enumerate(segments):
                segment_id = f"{unit.container_id}::segment::{index:03d}"
                markers: dict[str, str] = {}
                if pattern is not None:
                    def replace_literal(match):
                        literal = match.group(0)
                        marker = marker_by_literal[literal]
                        markers[marker] = literal
                        return marker

                    segment = pattern.sub(replace_literal, segment)
                markers_by_segment[segment_id] = markers
                segment_ids.append(segment_id)
                segment_units.append(
                    replace(
                        unit,
                        container_id=segment_id,
                        source_text=segment,
                        reading_order=len(segment_units),
                        required_literals=(),
                    )
                )
            segment_ids_by_container[unit.container_id] = segment_ids
        if not segment_units:
            return current, set()

        segment_request = PageTranslationRequest(
            request_id=request.request_id,
            page_id=request.page_id,
            source_language=request.source_language,
            target_language=request.target_language,
            units=tuple(segment_units),
        )
        segment_prompt = retry_prompt + (
            "\n\n# Long-unit segmented retry\n"
            "Translate every segment completely. Return every segment ID exactly once. "
            "Preserve each [[P13_KEEP_n]] marker exactly once and do not join or omit segments."
        )
        segment_config = replace(self.config, chunk_max_units=min(self.config.chunk_max_units, 3))
        retried = self._translate_with_transient_retry(
            QwenPageTranslationProvider(segment_config, segment_prompt),
            segment_request,
        )
        translated_segments = {}
        for item in retried.translations:
            translated_text = item.translated_text
            for marker, literal in markers_by_segment[item.container_id].items():
                translated_text = translated_text.replace(marker, literal)
            translated_segments[item.container_id] = translated_text.strip()

        separator = " " if request.target_language.casefold().startswith("en") else ""
        repaired_by_id = {
            container_id: separator.join(translated_segments[segment_id] for segment_id in segment_ids)
            for container_id, segment_ids in segment_ids_by_container.items()
        }
        request_ids = [value for value in (current.provider_request_id, retried.provider_request_id) if value]
        response_hashes = [value for value in (current.response_sha256, retried.response_sha256) if value]
        latency_ms = (current.latency_ms or 0) + (retried.latency_ms or 0)
        repaired = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=retried.model,
            translations=tuple(
                replace(item, translated_text=repaired_by_id.get(item.container_id, item.translated_text))
                for item in current.translations
            ),
            provider_request_id=",".join(request_ids) or None,
            latency_ms=latency_ms or None,
            response_sha256=(
                hashlib.sha256("".join(response_hashes).encode("ascii")).hexdigest()
                if response_hashes
                else None
            ),
        )
        repaired.validate_against(request)
        return repaired, set(segment_ids_by_container)

    def _translate_with_transient_retry(self, provider, request):
        for attempt in range(3):
            try:
                return provider.translate(request)
            except ProviderError as exc:
                transient = exc.code == "QWEN_TIMEOUT" or exc.code in {
                    "QWEN_CLIENT_JSONDecodeError",
                    "QWEN_CLIENT_ReadError",
                    "QWEN_HTTP_429",
                    "QWEN_HTTP_500",
                    "QWEN_HTTP_502",
                    "QWEN_HTTP_503",
                    "QWEN_HTTP_504",
                }
                if not transient or attempt == 2:
                    raise
                self.transient_retry_count += 1
        raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P13 body.chart page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true")
    selection.add_argument("--initial-expansion", action="store_true")
    selection.add_argument("--non-holdout", action="store_true")
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
    for name in ("cases", "input", "reports"):
        (run_root / name).mkdir()
    shutil.copy2(TOOLBOX / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "body.chart",
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
            prompt_name = "page_translation.en-zh.zh-CN.md" if str(record["source_language"]).startswith("en") else "page_translation.zh-en.zh-CN.md"
            provider = ValidatedQwenRetryProvider(
                qwen_config,
                (TOOLBOX / "prompts" / prompt_name).read_text(encoding="utf-8"),
            )
        result = run_p13_page(
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
                    "passthrough": result.passthrough,
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
            "passthrough_count": sum(result.passthrough for result in results),
            "holdout_accessed": holdout_accessed,
        },
    )
    return 0 if passed == len(results) else 2


def _translation_segments(text: str) -> tuple[str, ...]:
    segments = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])\s*|\r?\n+", text)
        if item.strip()
    ]
    if len(segments) < 2:
        segments = [
            item.strip()
            for item in re.split(r"(?<=[，,：:])\s*", text)
            if item.strip()
        ]
    return tuple(segments)


def _failed_container_ids(validation: dict[str, object]) -> set[str]:
    failed: set[str] = set()
    for key in (
        "missing_required_literals",
        "source_language_residue",
        "inadequate_outputs",
        "magnitude_unit_mismatches",
        "cross_container_duplicate_outputs",
    ):
        value = validation.get(key)
        if isinstance(value, dict):
            failed.update(str(container_id) for container_id in value)
    placeholders = validation.get("placeholder_outputs")
    if isinstance(placeholders, list):
        failed.update(str(container_id) for container_id in placeholders)
    return failed


def _confirmed_retained_proper_name_ids(
    request: PageTranslationRequest,
    validation: dict[str, object],
    observed_texts: dict[str, list[str]],
) -> list[str]:
    residue = validation.get("source_language_residue")
    if not isinstance(residue, dict):
        return []
    units = {unit.container_id: unit for unit in request.units}
    confirmed = []
    for container_id in residue:
        unit = units.get(str(container_id))
        if unit is None:
            continue
        source = unit.source_text.strip()
        words = re.findall(r"[A-Za-z][A-Za-z'’&.-]*", source)
        title_cased = words and all(
            word.casefold() in {"a", "an", "and", "of", "the", "vs"}
            or word[0].isupper()
            for word in words
        )
        observations = observed_texts.get(str(container_id), [])
        if (
            1 <= len(words) <= 4
            and title_cased
            and len(observations) >= 3
            and all(text.strip() == source for text in observations)
        ):
            confirmed.append(str(container_id))
    return sorted(confirmed)


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
    return {str(sample): {str(key): str(value) for key, value in rows.items()} for sample, rows in payload.items()}


if __name__ == "__main__":
    raise SystemExit(main())
