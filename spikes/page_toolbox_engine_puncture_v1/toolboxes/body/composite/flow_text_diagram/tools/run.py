from __future__ import annotations

import argparse
import json
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    TranslationResult,
    write_json,
)
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import (
    QwenConfig,
    QwenPageTranslationProvider,
)

from .. import TOOLBOX_KEY
from .engine import run_p18_page


TOOLBOX_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = TOOLBOX_DIR.parents[5]
MANIFEST = TOOLBOX_DIR / "samples" / "manifest.jsonl"


class MechanicalTranslationProvider:
    provider_name = "fixed"
    model_name = "p18-mechanical-expansion"

    def __init__(self, target_language: str) -> None:
        self.target_language = target_language

    def translate(self, request):
        rows = []
        for unit in request.units:
            length = max(1, min(24, len(unit.source_text.strip()) // 18 + 1))
            if self.target_language.casefold().startswith("zh"):
                text = "机械翻译文本" * length
            else:
                text = "Mechanical translated text " * length
            for literal in unit.required_literals:
                if literal not in text:
                    text += f" {literal}"
            rows.append(TranslationResult(unit.container_id, text.strip()))
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(rows),
        )
        bundle.validate_against(request)
        return bundle


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "recorded-p18-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self.bundle_path = bundle_path

    def translate(self, request):
        payload = json.loads(self.bundle_path.read_text(encoding="utf-8"))
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
    parser = argparse.ArgumentParser(description="Run P18 flow_text_diagram puncture batches")
    parser.add_argument("--batch", choices=("initial", "three", "all"), default="initial")
    parser.add_argument("--provider", choices=("fixed", "qwen", "recorded"), default="fixed")
    parser.add_argument("--recorded-run", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--sample-id", action="append", dest="sample_ids")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    args = parser.parse_args()

    records = _selected_records(_load_manifest(), args.batch, args.sample_ids)
    if not records:
        parser.error("no samples selected")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("p18-%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_DIR / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("input", "reports"):
        (run_root / name).mkdir()
    shutil.copy2(MANIFEST, run_root / "input" / "sample_manifest.jsonl")
    holdout_accessed = any(item["split"] == "holdout" for item in records)
    write_json(
        run_root / "input" / "selection.json",
        {
            "schema_version": "p18-selection/v1",
            "toolbox_key": TOOLBOX_KEY,
            "run_id": run_id,
            "batch": args.batch,
            "provider": args.provider,
            "sample_ids": [item["sample_id"] for item in records],
            "holdout_accessed": holdout_accessed,
            "holdout_integrity": "NON_BLIND_PREVIEWED_BEFORE_FREEZE",
            "cadence_exception": "USER_DIRECTED_P18_WHILE_P17_IS_MISSING",
        },
    )

    config = QwenConfig.from_environment() if args.provider == "qwen" else None
    results = []
    started = time.perf_counter()
    for record in records:
        source_pdf = (WORKSPACE_ROOT / str(record["source_ref"])).resolve()
        recorded_bundle = (
            args.recorded_run
            / str(record["sample_id"])
            / "output"
            / "translation_bundle.json"
            if args.recorded_run is not None
            else None
        )
        provider = _provider(
            args.provider,
            config,
            str(record["target_language"]),
            recorded_bundle,
        )
        case_started = time.perf_counter()
        result = run_p18_page(
            source_pdf=source_pdf,
            page_id=str(record["sample_id"]),
            run_dir=run_root / str(record["sample_id"]),
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=str(record["source_language"]),
            target_language=str(record["target_language"]),
        )
        row = {
            **result.__dict__,
            "sample_id": record["sample_id"],
            "split": record["split"],
            "phase": record["phase"],
            "elapsed_seconds": round(time.perf_counter() - case_started, 3),
            "translated_candidate": _translated_candidate_ready(Path(result.run_dir)),
        }
        results.append(row)
        write_json(
            run_root / "reports" / "progress.json",
            {
                "schema_version": "p18-progress/v1",
                "run_id": run_id,
                "completed_count": len(results),
                "sample_count": len(records),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "results": results,
            },
        )
        print(
            json.dumps(
                {
                    "sample_id": result.page_id,
                    "state": result.terminal_state,
                    "flow_mode": result.flow_mode,
                    "flow": result.flow_container_count,
                    "diagram": result.diagram_container_count,
                    "shared": result.shared_container_count,
                    "translated_candidate": row["translated_candidate"],
                    "elapsed_seconds": row["elapsed_seconds"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    state_counts = Counter(str(item["terminal_state"]) for item in results)
    summary = {
        "schema_version": "p18-flow-text-diagram-batch/v1",
        "stage": "P18",
        "toolbox_key": TOOLBOX_KEY,
        "run_id": run_id,
        "batch": args.batch,
        "provider": args.provider,
        "sample_count": len(results),
        "process_pass_count": sum(item["process_verdict"] == "PASS" for item in results),
        "product_pass_count": sum(item["product_verdict"] == "PASS" for item in results),
        "candidate_count": sum(bool(item["candidate_pdf"]) for item in results),
        "translated_candidate_count": sum(bool(item["translated_candidate"]) for item in results),
        "terminal_state_counts": dict(sorted(state_counts.items())),
        "sample_ids": [item["sample_id"] for item in results],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "blind_status": "NON_BLIND_PREVIEWED_BEFORE_FREEZE",
        "formal_promotion_eligible": False,
        "promotion_blockers": [
            "P4 flow_text.single formal gate and promotion manifest are absent",
            "P5 flow_text.multi gate is NOT_EVALUATED and has no promotion manifest",
            "P14 diagram gate is PASS_NON_BLIND and has no promotion manifest",
            "P18 corpus was previewed before a workflow freeze",
            *(["fixed output is mechanical evidence only"] if args.provider == "fixed" else []),
            *(
                ["recorded output replays existing semantics and is not independent model evidence"]
                if args.provider == "recorded"
                else []
            ),
        ],
        "results": results,
    }
    sheet = _batch_contact_sheet(run_root, results)
    if sheet:
        summary["contact_sheet"] = str(sheet)
    write_json(run_root / "reports" / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if state_counts == {"PAGE_PASSED": len(results)} else 2


def _provider(
    kind: str,
    config: QwenConfig | None,
    target_language: str,
    recorded_bundle: Path | None = None,
):
    if kind == "fixed":
        return MechanicalTranslationProvider(target_language)
    if kind == "recorded":
        if recorded_bundle is None:
            raise RuntimeError("recorded bundle path missing")
        return RecordedTranslationProvider(recorded_bundle)
    if config is None:
        raise RuntimeError("qwen config missing")
    prompt_name = (
        "page_translation.en-zh.zh-CN.md"
        if target_language.casefold().startswith("zh")
        else "page_translation.zh-en.zh-CN.md"
    )
    return QwenPageTranslationProvider(
        config,
        (TOOLBOX_DIR / "prompts" / prompt_name).read_text(encoding="utf-8"),
    )


def _load_manifest() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _selected_records(records, batch: str, sample_ids: list[str] | None):
    if sample_ids:
        requested = set(sample_ids)
        selected = [item for item in records if item["sample_id"] in requested]
        missing = requested - {str(item["sample_id"]) for item in selected}
        if missing:
            raise ValueError("unknown sample IDs: " + ",".join(sorted(missing)))
        return selected
    phases = {
        "initial": {"initial"},
        "three": {"initial", "expansion"},
        "all": {"initial", "expansion", "full"},
    }[batch]
    return [item for item in records if item["phase"] in phases]


def _translated_candidate_ready(run_dir: Path) -> bool:
    source = run_dir / "input" / "source.pdf"
    candidate = run_dir / "output" / "candidate.pdf"
    bundle = run_dir / "output" / "translation_bundle.json"
    validation = run_dir / "reports" / "translation_validation.json"
    failure = run_dir / "reports" / "failure_candidate.json"
    if not all(item.is_file() for item in (source, candidate, bundle, validation)) or failure.is_file():
        return False
    audit = json.loads(validation.read_text(encoding="utf-8"))
    return audit.get("final", {}).get("status") == "PASS" and sha256_file(source) != sha256_file(candidate)


def _batch_contact_sheet(run_root: Path, results) -> Path | None:
    rows = []
    for result in results:
        comparison = Path(str(result["run_dir"])) / "previews" / "comparison.png"
        if comparison.is_file():
            rows.append((str(result["sample_id"]), comparison))
    if not rows:
        return None
    cards = []
    for sample_id, path in rows:
        image = Image.open(path).convert("RGB")
        image.thumbnail((900, 620))
        card = Image.new("RGB", (920, 680), "white")
        card.paste(image, ((920 - image.width) // 2, 8))
        ImageDraw.Draw(card).text((12, 646), sample_id, fill="black")
        cards.append(card)
    columns = 2
    sheet = Image.new(
        "RGB",
        (columns * 920, ((len(cards) + columns - 1) // columns) * 680),
        "#d8d8d8",
    )
    for index, card in enumerate(cards):
        sheet.paste(card, ((index % columns) * 920, (index // columns) * 680))
    output = run_root / "reports" / "contact_sheet.jpg"
    sheet.save(output, quality=92)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
