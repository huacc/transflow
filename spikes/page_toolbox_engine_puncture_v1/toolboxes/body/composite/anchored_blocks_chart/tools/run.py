from __future__ import annotations

import argparse
import json
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
from page_toolbox_puncture.translation import QwenConfig
from toolboxes.body.chart.tools.run import ValidatedQwenRetryProvider

from .engine import run_p15_page


TOOLBOX_DIR = Path(__file__).resolve().parents[1]
MANIFEST = TOOLBOX_DIR / "samples" / "manifest.jsonl"


class MechanicalTranslationProvider:
    provider_name = "fixed-mechanical"
    model_name = "p15-ownership-layout-probe"

    def translate(self, request):
        prefix = "译" if request.target_language.casefold().startswith("zh") else "T"
        translations = tuple(
            TranslationResult(
                unit.container_id,
                prefix + (" " if unit.required_literals else "") + " ".join(unit.required_literals),
            )
            for unit in request.units
        )
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=translations,
        )
        bundle.validate_against(request)
        return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P15 anchored_blocks_chart puncture batches")
    parser.add_argument("--batch", choices=("initial", "three", "all"), default="initial")
    parser.add_argument("--provider", choices=("fixed", "qwen"), default="fixed")
    parser.add_argument("--run-id")
    parser.add_argument("--sample-id", action="append", dest="sample_ids")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    args = parser.parse_args()

    records = _selected_records(_load_manifest(), args.batch, args.sample_ids)
    if not records:
        parser.error("no samples selected")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_DIR / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)

    qwen_config = QwenConfig.from_environment() if args.provider == "qwen" else None
    results = []
    for record in records:
        provider = _provider(args.provider, qwen_config, record["target_language"])
        source_pdf = (TOOLBOX_DIR / record["source_ref"]).resolve()
        if not source_pdf.is_file():
            raise FileNotFoundError(source_pdf)
        result = run_p15_page(
            source_pdf=source_pdf,
            page_id=record["sample_id"],
            run_dir=run_root / record["sample_id"],
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=record["source_language"],
            target_language=record["target_language"],
            semantic_evaluation=args.provider == "qwen",
        )
        results.append(result)
        print(
            json.dumps(
                {
                    "sample_id": result.page_id,
                    "terminal_state": result.terminal_state,
                    "candidate_pdf": result.candidate_pdf,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    reports = run_root / "reports"
    reports.mkdir()
    state_counts = Counter(item.terminal_state for item in results)
    candidate_count = sum(Path(item.candidate_pdf).is_file() for item in results)
    untranslated_candidate_ids = [
        item.page_id for item in results if not _translated_candidate_ready(Path(item.run_dir))
    ]
    batch_result = {
        "schema_version": "p15-batch-result/v1",
        "stage": "P15",
        "toolbox_key": "body.composite.anchored_blocks_chart",
        "run_id": run_id,
        "batch": args.batch,
        "provider": args.provider,
        "semantic_evaluation": args.provider == "qwen",
        "sample_count": len(results),
        "candidate_count": candidate_count,
        "translated_candidate_count": len(results) - len(untranslated_candidate_ids),
        "untranslated_candidate_ids": untranslated_candidate_ids,
        "delivery_complete": not untranslated_candidate_ids,
        "terminal_state_counts": dict(sorted(state_counts.items())),
        "sample_ids": [item.page_id for item in results],
        "results": results,
        "blind_status": "NON_BLIND_PREVIEWED_BEFORE_FREEZE",
        "formal_promotion_eligible": False,
        "promotion_blockers": [
            "P11 prerequisite gate is EVIDENCE_INSUFFICIENT",
            "P13 prerequisite gate is PASS_NON_BLIND without promotion manifest",
            "P15 corpus was previewed before the split was frozen",
            *(
                ["fixed-mechanical output is not semantic translation evidence"]
                if args.provider == "fixed"
                else []
            ),
        ],
    }
    write_json(reports / "batch_result.json", batch_result)
    sheet = _batch_contact_sheet(run_root, results)
    if sheet:
        batch_result["contact_sheet"] = str(sheet)
        write_json(reports / "batch_result.json", batch_result)
    print(json.dumps(batch_result, ensure_ascii=False, default=str), flush=True)
    return 0


def _provider(kind: str, config: QwenConfig | None, target_language: str):
    if kind == "fixed":
        return MechanicalTranslationProvider()
    prompt_name = (
        "page_translation.en-zh.zh-CN.md"
        if target_language.casefold().startswith("zh")
        else "page_translation.zh-en.zh-CN.md"
    )
    return ValidatedQwenRetryProvider(
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
        wanted = set(sample_ids)
        return [item for item in records if item["sample_id"] in wanted]
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
    return (
        source.is_file()
        and candidate.is_file()
        and bundle.is_file()
        and sha256_file(source) != sha256_file(candidate)
    )


def _batch_contact_sheet(run_root: Path, results) -> Path | None:
    rows = []
    for result in results:
        path = Path(result.run_dir) / "previews" / "comparison.png"
        if path.is_file():
            rows.append((result.page_id, result.terminal_state, path))
    if not rows:
        return None
    thumb_width, thumb_height = 520, 400
    columns = 3
    cell_height = thumb_height + 42
    sheet = Image.new(
        "RGB",
        (columns * thumb_width, ((len(rows) + columns - 1) // columns) * cell_height),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    for index, (sample_id, state, path) in enumerate(rows):
        image = Image.open(path).convert("RGB")
        image.thumbnail((thumb_width - 12, thumb_height - 8))
        x = (index % columns) * thumb_width + (thumb_width - image.width) // 2
        y = (index // columns) * cell_height + 30
        sheet.paste(image, (x, y))
        draw.text(((index % columns) * thumb_width + 8, (index // columns) * cell_height + 8), f"{sample_id} | {state}", fill="black")
    destination = run_root / "reports" / "contact_sheet.jpg"
    sheet.save(destination, quality=88)
    return destination


if __name__ == "__main__":
    raise SystemExit(main())
