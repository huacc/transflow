"""Run real bidirectional translation and layout for the frozen 30-page chart pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import sys
from dataclasses import replace
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pymupdf

_BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
for _bootstrap_path in (_BOOTSTRAP_ROOT, _BOOTSTRAP_ROOT / "src"):
    if str(_bootstrap_path) not in sys.path:
        sys.path.insert(0, str(_bootstrap_path))

from scripts.run_toolbox_leaf_migration import (  # noqa: E402
    MigrationContractError,
    provider_configuration_snapshot,
    store_translation_bundle,
)
from scripts.toolbox_leaf_migration_chart_run import (  # noqa: E402
    ROUTE,
    _normalized,
)
from scripts.toolbox_leaf_migration_visual_only import (  # noqa: E402
    FONT_MANIFEST,
    P8_POLICY,
    _compose_comparison,
    _relative,
    _render_page,
    _sha256_file,
    _write_json,
)
from tests.migration.p9_qwen_translation_adapter import (  # noqa: E402
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from transflow.application.document_coordinator import DocumentCoordinator  # noqa: E402
from transflow.application.toolbox_page_coordinator import (  # noqa: E402
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.domain.common import content_sha256  # noqa: E402
from transflow.domain.completeness import CompletenessStatus  # noqa: E402
from transflow.domain.jobs import DocumentRunRequest  # noqa: E402
from transflow.domain.translation import (  # noqa: E402
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
)
from transflow.pdf_kernel import (  # noqa: E402
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
)
from transflow.toolboxes.leaves.body_chart.prompt import (  # noqa: E402
    chart_translation_system_prompt,
)
from transflow.toolboxes.leaves.body_chart.template import (  # noqa: E402
    build_chart_template,
)
from transflow.toolboxes.leaves.body_chart.toolbox import ChartToolbox  # noqa: E402
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy  # noqa: E402

REPO_ROOT = _BOOTSTRAP_ROOT
RUNS_ROOT = REPO_ROOT / "runs/toolbox_leaf_migration/TM3"
CHART_ROOT = REPO_ROOT / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/chart"
POOL_MANIFEST = CHART_ROOT / "samples/manifest.jsonl"
FONT_ID = "noto-sans-cjk-sc-regular"
HAN = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD = re.compile(r"\b[A-Za-z]{2,}\b")


class _RecordingTranslationPort:
    """Persist validated bundles without retaining raw provider responses."""

    def __init__(
        self,
        delegate: MigrationQwenTranslationAdapter,
        storage_root: Path,
    ) -> None:
        self.delegate = delegate
        self.storage_root = storage_root
        self.records: list[
            tuple[TranslationBatch, TranslationBundle, str, Path, str]
        ] = []

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        bundle = self.delegate.translate(batch)
        self._record(batch, bundle, "INITIAL")
        return bundle

    def repair(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle,
    ) -> TranslationBundle:
        bundle = self.delegate.repair(batch, previous)
        self._record(batch, bundle, "TARGETED_RETRY")
        return bundle

    def _record(
        self,
        batch: TranslationBatch,
        bundle: TranslationBundle,
        call_kind: str,
    ) -> None:
        stored = store_translation_bundle(
            batch,
            bundle,
            self.storage_root,
            provider_configuration_snapshot(),
        )
        self.records.append(
            (batch, bundle, stored.bundle_hash, stored.path, call_kind)
        )


def _load_cases() -> tuple[dict[str, Any], ...]:
    """Load and verify the immutable one-to-one classification/toolbox pool."""

    records = tuple(
        json.loads(line)
        for line in POOL_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len(records) != 30:
        raise MigrationContractError("TM3_CHART_POOL_SIZE_INVALID", str(len(records)))
    allowed_directions = {("en", "zh-CN"), ("zh-CN", "en")}
    identities: set[str] = set()
    for record in records:
        sample_id = str(record["sample_id"])
        if sample_id in identities:
            raise MigrationContractError("TM3_CHART_POOL_ID_DUPLICATE", sample_id)
        identities.add(sample_id)
        direction = (
            str(record["source_language"]),
            str(record["target_language"]),
        )
        if direction not in allowed_directions:
            raise MigrationContractError(
                "TM3_CHART_POOL_DIRECTION_INVALID",
                sample_id,
            )
        source = CHART_ROOT / str(record["source_ref"])
        upstream = REPO_ROOT / "spikes" / str(record["upstream_ref"])
        expected_hash = str(record["sha256"])
        if (
            not source.is_file()
            or not upstream.is_file()
            or _sha256_file(source) != expected_hash
            or _sha256_file(upstream) != expected_hash
        ):
            raise MigrationContractError("TM3_CHART_POOL_SOURCE_DRIFT", sample_id)
    return records


def _write_translated_pdf(
    source: Path,
    target: Path,
    page: Any,
    result: Any,
    interpreter: PagePatchInterpreter,
) -> None:
    _write_patch_pdf(source, target, page, result.patch, interpreter)


def _write_patch_pdf(
    source: Path,
    target: Path,
    page: Any,
    patch: Any,
    interpreter: PagePatchInterpreter,
    *,
    diagnostic: bool = False,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        applied = interpreter.apply(
            document,
            page.context,
            page.facts,
            patch,
            ROUTE,
            diagnostic=diagnostic,
        )
        if not applied.fits:
            missing = ",".join(
                operation_id
                for operation_id, remainder in zip(
                    applied.operation_ids,
                    applied.layout_remainders,
                    strict=True,
                )
                if remainder < 0
            )
            raise MigrationContractError(
                "TM3_CHART_PATCH_TEXT_NOT_MATERIALIZED",
                missing,
            )
        document.save(target, garbage=4, deflate=True)


def _materialization_metrics(
    source: Path,
    output: Path,
    source_language: str,
    batch: TranslationBatch,
    bundle: TranslationBundle,
) -> dict[str, object]:
    with pymupdf.open(source) as source_document:
        source_text = source_document[0].get_text("text")
    with pymupdf.open(output) as output_document:
        output_text = output_document[0].get_text("text")
    translated_by_id = {
        item.unit_id: item.translated_text for item in bundle.units
    }
    source_residue_count = sum(
        _normalized(unit.source_text) in _normalized(output_text)
        for unit in batch.units
        if len(_normalized(unit.source_text)) >= 8
        and _normalized(translated_by_id[unit.unit_id])
        != _normalized(unit.source_text)
    )
    target_script_count = (
        len(HAN.findall(output_text))
        if source_language == "en"
        else len(LATIN_WORD.findall(output_text))
    )
    return {
        "output_text_sha256": hashlib.sha256(
            output_text.encode("utf-8")
        ).hexdigest(),
        "source_residue_count": source_residue_count,
        "source_text_sha256": hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        "target_script_count": target_script_count,
    }


def _layout_gate(
    template: Any,
    patch: Any,
    facts: Any,
    minimum_font_size: float,
) -> dict[str, object]:
    """Expose and enforce row binding plus page-wide font degradation."""

    spans_by_id = {item.object_id: item for item in facts.text_spans}
    table_roles = {
        "TABLE_HEADER",
        "TABLE_SECTION",
        "TABLE_CELL",
        "TABLE_TOTAL",
    }
    operations: list[dict[str, object]] = []
    unmapped_operation_ids: list[str] = []
    row_binding_failures: list[str] = []
    minimum_font_operation_count = 0
    source_above_minimum_count = 0
    for operation in patch.operations:
        target_ids = set(operation.target_object_ids)
        container = next(
            (
                item
                for item in template.containers
                if target_ids == set(item.source_object_ids)
            ),
            None,
        )
        if container is None:
            container = next(
                (
                    item
                    for item in template.containers
                    if target_ids
                    and target_ids <= set(item.source_object_ids)
                ),
                None,
            )
        if container is None:
            unmapped_operation_ids.append(operation.operation_id)
            continue
        source_font_size = statistics.median(
            spans_by_id[object_id].font_size
            for object_id in operation.target_object_ids
        )
        output_font_size = float(operation.font_size or 0.0)
        at_minimum = output_font_size <= minimum_font_size + 0.001
        minimum_font_operation_count += int(at_minimum)
        source_above_minimum_count += int(
            source_font_size > minimum_font_size + 0.5
        )
        row_bound = (
            container.role not in table_roles
            or _contains_rect(container.allowed_bbox, operation.rect)
        )
        if not row_bound:
            row_binding_failures.append(operation.operation_id)
        operations.append(
            {
                "operation_id": operation.operation_id,
                "container_id": container.container_id,
                "role": container.role,
                "source_text": container.source_text,
                "source_bbox": list(container.source_bbox),
                "allowed_bbox": list(container.allowed_bbox),
                "output_bbox": list(operation.rect),
                "source_font_size": round(source_font_size, 4),
                "output_font_size": output_font_size,
                "row_bound": row_bound,
            }
        )
    global_minimum_font_degradation = (
        len(operations) > 1
        and minimum_font_operation_count == len(operations)
        and source_above_minimum_count > 0
    )
    return {
        "schema_version": "transflow.tm3-chart-layout-gate/v1",
        "operation_count": len(operations),
        "unmapped_operation_ids": unmapped_operation_ids,
        "table_row_binding_failures": row_binding_failures,
        "minimum_font_operation_count": minimum_font_operation_count,
        "global_minimum_font_degradation": global_minimum_font_degradation,
        "operations": operations,
    }


def _materialized_layout_gate(
    output: Path,
    template: Any,
    facts: Any,
    patch: Any,
) -> dict[str, object]:
    """Judge real chart glyphs; a writable Patch alone is not product PASS."""

    candidate_facts = PageFactsExtractor().extract_page(
        output,
        _sha256_file(output),
        patch.page_no,
    )
    with pymupdf.open(output) as document:
        page = document[patch.page_no - 1]
        page_lines: list[dict[str, object]] = []
        page_spans: list[dict[str, object]] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = tuple(
                    span
                    for span in line.get("spans", [])
                    if str(span.get("text", "")).strip()
                )
                if not spans:
                    continue
                page_spans.extend(
                    {
                        "bbox": tuple(float(value) for value in span["bbox"]),
                        "font_size": float(span["size"]),
                        "origin_y": float(span["origin"][1]),
                        "text": str(span["text"]),
                    }
                    for span in spans
                )
                raw_bbox = line["bbox"]
                page_lines.append(
                    {
                        "bbox": tuple(float(value) for value in raw_bbox),
                        "font_size": max(float(span["size"]) for span in spans),
                        "origin_y": statistics.median(
                            float(span["origin"][1]) for span in spans
                        ),
                        "text": "".join(str(span["text"]) for span in spans),
                    }
                )
    page_lines.sort(
        key=lambda item: (
            item["bbox"][1],
            item["bbox"][0],
        )
    )
    table_roles = {
        "TABLE_HEADER",
        "TABLE_SECTION",
        "TABLE_CELL",
        "TABLE_TOTAL",
    }
    visual_by_id = {
        item.object_id: item.bbox
        for item in (*facts.image_objects, *facts.drawing_objects)
    }
    records: list[dict[str, object]] = []
    glyphs: list[tuple[str, tuple[float, float, float, float]]] = []
    missing: list[str] = []
    allowed_region_failures: list[str] = []
    row_failures: list[str] = []
    anchor_failures: list[str] = []
    font_failures: list[str] = []
    spacing_failures: list[str] = []
    semantic_row_baseline_failures: list[str] = []
    visual_collision_ids: list[str] = []
    for operation in patch.operations:
        target_ids = set(operation.target_object_ids)
        container = next(
            (
                item
                for item in template.containers
                if target_ids == set(item.source_object_ids)
            ),
            None,
        )
        if container is None:
            container = next(
                (
                    item
                    for item in template.containers
                    if target_ids and target_ids <= set(item.source_object_ids)
                ),
                None,
            )
        if (
            container is None
            or operation.rect is None
            or operation.replacement_text is None
        ):
            missing.append(operation.operation_id)
            continue
        planned_font = float(operation.font_size or 0.0)
        search_tolerance = max(1.0, planned_font * 1.35)
        nearby = tuple(
            item
            for item in page_lines
            if operation.rect[0] - search_tolerance
            <= (item["bbox"][0] + item["bbox"][2]) / 2.0
            <= operation.rect[2] + search_tolerance
            and operation.rect[1] - search_tolerance
            <= (item["bbox"][1] + item["bbox"][3]) / 2.0
            <= operation.rect[3] + search_tolerance
        )
        lines = _matching_materialized_lines(
            nearby,
            operation.replacement_text,
            operation.rect,
        )
        if not lines:
            missing.append(operation.operation_id)
            records.append(
                {
                    "operation_id": operation.operation_id,
                    "container_id": container.container_id,
                    "role": container.role,
                    "materialized": False,
                }
            )
            continue
        kept_numeric_prefix = _kept_numeric_prefix_span(
            container,
            operation,
            facts,
        )
        semantic_row_baseline_delta: float | None = None
        semantic_row_baseline_ok: bool | None = None
        if kept_numeric_prefix is not None:
            materialized_prefix = min(
                (
                    span
                    for span in page_spans
                    if _materialized_text_key(str(span["text"]))
                    == _materialized_text_key(kept_numeric_prefix.text)
                ),
                key=lambda span: (
                    abs(float(span["bbox"][0]) - kept_numeric_prefix.bbox[0])
                    + abs(float(span["bbox"][1]) - kept_numeric_prefix.bbox[1])
                ),
                default=None,
            )
            if materialized_prefix is not None and len(lines) == 1:
                semantic_row_baseline_delta = (
                    float(lines[0]["origin_y"])
                    - float(materialized_prefix["origin_y"])
                )
                semantic_row_baseline_ok = abs(
                    semantic_row_baseline_delta
                ) <= max(0.75, float(lines[0]["font_size"]) * 0.10)
            else:
                semantic_row_baseline_ok = False
            if not semantic_row_baseline_ok:
                semantic_row_baseline_failures.append(operation.operation_id)
        glyph_bbox = (
            min(item["bbox"][0] for item in lines),
            min(item["bbox"][1] for item in lines),
            max(item["bbox"][2] for item in lines),
            max(item["bbox"][3] for item in lines),
        )
        glyphs.append((operation.operation_id, glyph_bbox))
        actual_font = statistics.median(
            float(item["font_size"]) for item in lines
        )
        font_ratio = actual_font / max(planned_font, 0.01)
        font_ok = 0.90 <= font_ratio <= 1.10
        if not font_ok:
            font_failures.append(operation.operation_id)
        region_tolerance = max(1.0, actual_font * 0.25)
        within_allowed = _contains_rect_tolerant(
            container.allowed_bbox,
            glyph_bbox,
            region_tolerance,
        )
        if not within_allowed:
            allowed_region_failures.append(operation.operation_id)
            if container.role in table_roles:
                row_failures.append(operation.operation_id)
        anchor_delta: float | None = None
        anchor_ok: bool | None = None
        if not container.rotation:
            if container.alignment == "RIGHT":
                anchor_delta = glyph_bbox[2] - operation.rect[2]
            elif container.alignment == "CENTER":
                anchor_delta = (
                    (glyph_bbox[0] + glyph_bbox[2]) / 2.0
                    - (operation.rect[0] + operation.rect[2]) / 2.0
                )
            else:
                anchor_delta = glyph_bbox[0] - operation.rect[0]
            anchor_ok = abs(anchor_delta) <= max(1.0, actual_font * 0.25)
            if not anchor_ok:
                anchor_failures.append(operation.operation_id)
        line_spacing_ratios = [
            (float(current["bbox"][1]) - float(previous["bbox"][1]))
            / max(float(previous["font_size"]), 0.01)
            for previous, current in pairwise(lines)
            if float(current["bbox"][1]) > float(previous["bbox"][1]) + 0.1
        ]
        minimum_spacing = max(
            0.75,
            float(operation.line_height or 1.0) - 0.12,
        )
        spacing_ok = all(
            ratio >= minimum_spacing for ratio in line_spacing_ratios
        )
        if not spacing_ok:
            spacing_failures.append(operation.operation_id)
        source_visual_ids = {
            object_id
            for object_id, bbox in visual_by_id.items()
            if _intersection_area(container.source_bbox, bbox) > 0.05
        } | set(container.anchor_object_ids)
        collided_visuals = [
            object_id
            for object_id, bbox in visual_by_id.items()
            if object_id not in source_visual_ids
            and _intersection_area(glyph_bbox, bbox) > 0.5
        ]
        if collided_visuals:
            visual_collision_ids.append(operation.operation_id)
        records.append(
            {
                "operation_id": operation.operation_id,
                "container_id": container.container_id,
                "role": container.role,
                "alignment": container.alignment,
                "materialized": True,
                "actual_glyph_bbox": list(glyph_bbox),
                "actual_line_count": len(lines),
                "actual_font_size": round(actual_font, 4),
                "planned_font_size": planned_font,
                "actual_to_planned_font_ratio": round(font_ratio, 4),
                "actual_font_within_10_percent": font_ok,
                "horizontal_anchor_delta": (
                    None if anchor_delta is None else round(anchor_delta, 4)
                ),
                "horizontal_anchor_stable": anchor_ok,
                "vertical_anchor_delta": round(
                    glyph_bbox[1] - container.source_bbox[1],
                    4,
                ),
                "semantic_row_baseline_delta": (
                    None
                    if semantic_row_baseline_delta is None
                    else round(semantic_row_baseline_delta, 4)
                ),
                "semantic_row_baseline_aligned": semantic_row_baseline_ok,
                "within_allowed_region": within_allowed,
                "row_bound": (
                    within_allowed
                    if container.role in table_roles
                    else None
                ),
                "line_spacing_ratios": [
                    round(value, 4) for value in line_spacing_ratios
                ],
                "line_spacing_minimum": round(minimum_spacing, 4),
                "line_spacing_acceptable": spacing_ok,
                "new_protected_visual_collision_ids": collided_visuals,
            }
        )
    collision_pairs = [
        [left_id, right_id]
        for index, (left_id, left_bbox) in enumerate(glyphs)
        for right_id, right_bbox in glyphs[index + 1 :]
        if (
            min(left_bbox[2], right_bbox[2])
            - max(left_bbox[0], right_bbox[0])
            > 1.0
            and min(left_bbox[3], right_bbox[3])
            - max(left_bbox[1], right_bbox[1])
            > 1.0
        )
    ]
    locked_objects_changed = (
        _locked_objects_signature(candidate_facts)
        != _locked_objects_signature(facts)
    )
    passed = not any(
        (
            missing,
            allowed_region_failures,
            row_failures,
            anchor_failures,
            font_failures,
            spacing_failures,
            semantic_row_baseline_failures,
            visual_collision_ids,
            collision_pairs,
        )
    ) and not locked_objects_changed
    return {
        "schema_version": "transflow.tm3-chart-materialized-layout-gate/v1",
        "expected_operation_count": len(patch.operations),
        "materialized_operation_count": len(patch.operations) - len(missing),
        "missing_operation_ids": missing,
        "allowed_region_failures": allowed_region_failures,
        "table_row_binding_failures": row_failures,
        "horizontal_anchor_failures": anchor_failures,
        "actual_font_failures": font_failures,
        "line_spacing_failures": spacing_failures,
        "semantic_row_baseline_failures": semantic_row_baseline_failures,
        "protected_visual_collision_operation_ids": visual_collision_ids,
        "translated_glyph_collision_pairs": collision_pairs,
        "locked_objects_changed": locked_objects_changed,
        "passed": passed,
        "operations": records,
    }


def _kept_numeric_prefix_span(
    container: Any,
    operation: Any,
    facts: Any,
) -> Any | None:
    """Find the protected numeric prefix that shares one source table row."""

    if container.role not in {
        "TABLE_HEADER",
        "TABLE_SECTION",
        "TABLE_CELL",
        "TABLE_TOTAL",
    }:
        return None
    target_ids = set(operation.target_object_ids)
    spans_by_id = {item.object_id: item for item in facts.text_spans}
    target_spans = [
        spans_by_id[object_id]
        for object_id in operation.target_object_ids
        if object_id in spans_by_id
    ]
    if not target_spans:
        return None
    target_left = min(item.bbox[0] for item in target_spans)
    candidates = []
    for object_id in container.source_object_ids:
        if object_id in target_ids or object_id not in spans_by_id:
            continue
        item = spans_by_id[object_id]
        vertical_overlap = max(
            0.0,
            min(container.source_bbox[3], item.bbox[3])
            - max(container.source_bbox[1], item.bbox[1]),
        )
        if (
            re.fullmatch(r"[-+]?\d+(?:[.,:/-]\d+)*%?", item.text.strip())
            and item.bbox[2] <= target_left + 0.2
            and vertical_overlap > 0.0
        ):
            candidates.append(item)
    return (
        None
        if not candidates
        else max(candidates, key=lambda item: item.bbox[2])
    )


def _locked_objects_signature(facts: Any) -> tuple[object, ...]:
    """Compare protected objects as visual multisets, not PDF stream order."""

    return (
        facts.media_box,
        facts.crop_box,
        facts.rotation,
        tuple(
            sorted(
                (item.bbox, item.width, item.height, item.content_hash)
                for item in facts.image_objects
            )
        ),
        tuple(
            sorted(
                (item.bbox, item.content_hash)
                for item in facts.drawing_objects
            )
        ),
        tuple(
            sorted(
                (item.bbox, item.annotation_type, item.content_hash)
                for item in facts.annotation_objects
            )
        ),
        tuple(
            sorted(
                (item.bbox, item.kind, item.content_hash)
                for item in facts.link_objects
            )
        ),
    )


def _matching_materialized_lines(
    lines: tuple[dict[str, object], ...],
    expected_text: str,
    target_rect: tuple[float, float, float, float],
) -> tuple[dict[str, object], ...]:
    expected = _materialized_text_key(expected_text)
    best: tuple[
        tuple[float, float, int],
        tuple[dict[str, object], ...],
    ] | None = None
    for start in range(len(lines)):
        observed = ""
        for end in range(start, len(lines)):
            observed += _materialized_text_key(str(lines[end]["text"]))
            if expected in observed:
                window = lines[start : end + 1]
                bbox = (
                    min(float(item["bbox"][0]) for item in window),
                    min(float(item["bbox"][1]) for item in window),
                    max(float(item["bbox"][2]) for item in window),
                    max(float(item["bbox"][3]) for item in window),
                )
                center_distance = abs(
                    (bbox[1] + bbox[3]) / 2.0
                    - (target_rect[1] + target_rect[3]) / 2.0
                ) + 0.20 * abs(
                    (bbox[0] + bbox[2]) / 2.0
                    - (target_rect[0] + target_rect[2]) / 2.0
                )
                score = (
                    float(len(observed) - len(expected)),
                    center_distance,
                    len(window),
                )
                if best is None or score < best[0]:
                    best = (score, window)
                break
            if len(observed) > len(expected) * 2 + 64:
                break
    return () if best is None else best[1]


def _materialized_text_key(value: str) -> str:
    normalized = _normalized(value)
    for dash in (
        "\u2010",
        "\u2011",
        "\u2012",
        "\u2013",
        "\u2014",
        "\u2015",
        "\u2212",
    ):
        normalized = normalized.replace(dash, "-")
    for bullet in ("\u00b7", "\u2027", "\u2219", "\u30fb"):
        normalized = normalized.replace(bullet, "\u2022")
    return normalized


def _contains_rect_tolerant(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _contains_rect(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float] | None,
) -> bool:
    return bool(
        inner is not None
        and inner[0] >= outer[0] - 0.05
        and inner[1] >= outer[1] - 0.05
        and inner[2] <= outer[2] + 0.05
        and inner[3] <= outer[3] + 0.05
    )


def _copy_case_input(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _render_case_artifacts(
    case_root: Path,
    input_pdf: Path,
    output_pdf: Path,
) -> dict[str, Path]:
    source_png = case_root / "input/source.png"
    output_png = case_root / "output/transflow.png"
    comparison_pdf = case_root / "review/source_vs_transflow.pdf"
    comparison_png = case_root / "review/source_vs_transflow.png"
    _render_page(input_pdf, 1, source_png)
    _render_page(output_pdf, 1, output_png)
    _compose_comparison(
        (("SOURCE", input_pdf), ("TRANSFLOW", output_pdf)),
        comparison_pdf,
        comparison_png,
    )
    return {
        "input": input_pdf,
        "output": output_pdf,
        "review": comparison_png,
        "review_pdf": comparison_pdf,
    }


def _ensure_failure_artifacts(
    *,
    case_root: Path,
    source: Path,
    run_root: Path,
    sample_id: str,
    error: Exception,
) -> dict[str, object]:
    """Guarantee an inspectable FAIL PDF without presenting it as accepted."""

    input_pdf = case_root / "input/source.pdf"
    output_pdf = case_root / "output/transflow.pdf"
    if not input_pdf.is_file():
        _copy_case_input(source, input_pdf)
    if not output_pdf.is_file():
        _copy_case_input(input_pdf, output_pdf)
    artifact_mode = (
        "SOURCE_FALLBACK"
        if _sha256_file(input_pdf) == _sha256_file(output_pdf)
        else "TRANSLATED_REJECTED"
    )
    try:
        artifacts = _render_case_artifacts(case_root, input_pdf, output_pdf)
    except Exception:
        _copy_case_input(input_pdf, output_pdf)
        artifact_mode = "SOURCE_FALLBACK"
        artifacts = _render_case_artifacts(case_root, input_pdf, output_pdf)

    case_manifest = case_root / "process/case_manifest.json"
    if case_manifest.is_file():
        process_path = case_manifest
        process = json.loads(case_manifest.read_text(encoding="utf-8"))
        artifact_mode = str(process.get("artifact_mode", artifact_mode))
    else:
        process_path = case_root / "process/failure_manifest.json"
        _write_json(
            process_path,
            {
                "schema_version": "transflow.tm3-chart-pool-failure/v1",
                "sample_id": sample_id,
                "status": "FAIL",
                "product_acceptance": False,
                "artifact_mode": artifact_mode,
                "error": {
                    "error_type": type(error).__name__,
                    "code": getattr(error, "code", None),
                    "detail": str(error),
                },
                "artifacts": {
                    name: _relative(path, run_root)
                    for name, path in artifacts.items()
                },
            },
            run_root,
        )
    return {
        "sample_id": sample_id,
        "status": "FAIL",
        "product_acceptance": False,
        "artifact_mode": artifact_mode,
        "output": _relative(output_pdf, run_root),
        "review": _relative(artifacts["review"], run_root),
        "process": _relative(process_path, run_root),
    }


def _diagnostic_bundle(
    batch: TranslationBatch,
    records: list[
        tuple[TranslationBatch, TranslationBundle, str, Path, str]
    ],
) -> TranslationBundle | None:
    translated_by_id: dict[str, str] = {}
    expected = set(batch.ordered_unit_ids)
    for _, bundle, _, _, _ in records:
        translated_by_id.update(
            {
                unit.unit_id: unit.translated_text
                for unit in bundle.units
                if unit.unit_id in expected
            }
        )
    if set(translated_by_id) != expected:
        return None
    return TranslationBundle.from_batch(
        batch,
        tuple(
            TranslatedUnit(unit.unit_id, translated_by_id[unit.unit_id])
            for unit in batch.units
        ),
    )


def _run_case(
    *,
    record: dict[str, Any],
    index: int,
    run_id: str,
    run_root: Path,
    base_policy: Any,
    font_path: Path,
    interpreter: PagePatchInterpreter,
    translation_port: _RecordingTranslationPort,
) -> dict[str, object]:
    sample_id = str(record["sample_id"])
    case_root = run_root / "cases" / f"{index:02d}-{sample_id}"
    input_pdf = case_root / "input/source.pdf"
    output_pdf = case_root / "output/transflow.pdf"
    _copy_case_input(CHART_ROOT / str(record["source_ref"]), input_pdf)
    source_hash = _sha256_file(input_pdf)
    source_language = str(record["source_language"])
    target_language = str(record["target_language"])
    policy = replace(
        base_policy,
        source_language=source_language,
        target_language=target_language,
    )
    request = DocumentRunRequest(
        source_pdf_path=str(input_pdf.resolve()),
        source_hash=source_hash,
        source_language=source_language,
        target_language=target_language,
        config_snapshot_hash=content_sha256(policy),
        job_id=f"job-{run_id}-{sample_id}",
        run_id=f"{run_id}-{sample_id}",
    )
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    template = build_chart_template(page.facts)
    toolbox = ChartToolbox(policy, font_path)
    page_template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(page_template)
    if batch is None:
        raise MigrationContractError("TM3_CHART_POOL_BATCH_MISSING", sample_id)
    record_start = len(translation_port.records)
    result = ToolboxPageCoordinator(translation_port).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            toolbox,
            target_language=target_language,
        )
    )
    case_records = translation_port.records[record_start:]
    if (
        result.patch is None
        or result.translation_bundle is None
        or result.completeness_decision is None
        or result.completeness_decision.status is not CompletenessStatus.PASS
        or result.outcome.quality.value != "PASS"
        or result.outcome.fallback.value != "NONE"
    ):
        diagnostic_bundle = result.translation_bundle or _diagnostic_bundle(
            batch,
            case_records,
        )
        diagnostic_records: list[dict[str, object]] = []
        artifact_mode = "SOURCE_FALLBACK"
        diagnostic_patch = result.proposed_patch
        if diagnostic_patch is not None:
            try:
                _write_patch_pdf(
                    input_pdf,
                    output_pdf,
                    page,
                    diagnostic_patch,
                    interpreter,
                )
                artifact_mode = "REJECTED_PRODUCT_CANDIDATE"
                diagnostic_records.append(
                    {
                        "operation_type": "rejected_product_candidate",
                        "operation_count": len(diagnostic_patch.operations),
                        "product_acceptance": False,
                    }
                )
            except Exception as error:
                diagnostic_patch = None
                diagnostic_records.append(
                    {
                        "operation_type": "rejected_product_candidate_failed",
                        "failure": str(error),
                        "product_acceptance": False,
                    }
                )
        if diagnostic_patch is None and diagnostic_bundle is not None:
            diagnostic_patch, records = toolbox.build_diagnostic_patch(
                page_template,
                batch,
                diagnostic_bundle,
            )
            diagnostic_records.extend(records)
            if diagnostic_patch is not None:
                _write_patch_pdf(
                    input_pdf,
                    output_pdf,
                    page,
                    diagnostic_patch,
                    interpreter,
                    diagnostic=True,
                )
                artifact_mode = "TRANSLATED_DIAGNOSTIC"
        if not output_pdf.is_file():
            _copy_case_input(input_pdf, output_pdf)
        artifacts = _render_case_artifacts(case_root, input_pdf, output_pdf)
        decision = result.completeness_decision
        process = {
            "schema_version": "transflow.tm3-chart-pool-case/v1",
            "sample_id": sample_id,
            "direction": f"{source_language}->{target_language}",
            "source_hash": source_hash,
            "status": "FAIL",
            "product_acceptance": False,
            "artifact_mode": artifact_mode,
            "translation": {
                "batch_hash": content_sha256(batch),
                "bundle_hash": (
                    content_sha256(diagnostic_bundle)
                    if diagnostic_bundle is not None
                    else None
                ),
                "provider_records": [
                    {
                        "bundle_hash": bundle_hash,
                        "bundle_path": _relative(path, REPO_ROOT),
                        "call_kind": call_kind,
                        "unit_count": len(recorded_batch.units),
                    }
                    for (
                        recorded_batch,
                        _,
                        bundle_hash,
                        path,
                        call_kind,
                    ) in case_records
                ],
            },
            "completeness": {
                "status": decision.status.value if decision is not None else None,
                "errors": (
                    [
                        {
                            "code": error.code.value,
                            "detail": error.detail,
                            "unit_id": error.unit_id,
                        }
                        for error in decision.errors
                    ]
                    if decision is not None
                    else []
                ),
            },
            "outcome": {
                "fallback": result.outcome.fallback.value,
                "finding_codes": list(result.outcome.finding_codes),
                "quality": result.outcome.quality.value,
                "translation_coverage": result.outcome.translation_coverage.value,
            },
            "diagnostic_records": diagnostic_records,
            "artifacts": {
                name: _relative(path, run_root)
                for name, path in artifacts.items()
            },
        }
        _write_json(case_root / "process/case_manifest.json", process, run_root)
        raise MigrationContractError(
            "TM3_CHART_POOL_PAGE_NOT_DELIVERABLE",
            sample_id,
        )
    rule_trace = toolbox.rule_trace(f"plan-{page_template.template_id}")
    gate_rejection_codes: list[str] = []
    if len(rule_trace) != len(result.patch.operations):
        gate_rejection_codes.append("TM3_CHART_LAYOUT_RULE_TRACE_INCOMPLETE")
    layout_gate = _layout_gate(
        template,
        result.patch,
        page.facts,
        policy.minimum_font_size,
    )
    if layout_gate["unmapped_operation_ids"]:
        gate_rejection_codes.append("TM3_CHART_LAYOUT_OPERATION_UNMAPPED")
    if layout_gate["table_row_binding_failures"]:
        gate_rejection_codes.append("TM3_CHART_TABLE_ROW_BINDING_CHANGED")
    if layout_gate["global_minimum_font_degradation"]:
        gate_rejection_codes.append(
            "TM3_CHART_GLOBAL_MINIMUM_FONT_DEGRADATION"
        )
    _write_translated_pdf(input_pdf, output_pdf, page, result, interpreter)
    artifacts = _render_case_artifacts(case_root, input_pdf, output_pdf)
    metrics = _materialization_metrics(
        input_pdf,
        output_pdf,
        source_language,
        batch,
        result.translation_bundle,
    )
    translated_content_materialized = not (
        _sha256_file(input_pdf) == _sha256_file(output_pdf)
        or int(metrics["target_script_count"]) < 1
    )
    if not translated_content_materialized:
        gate_rejection_codes.append(
            "TM3_CHART_POOL_TRANSLATION_NOT_MATERIALIZED"
        )
    materialized_layout_gate = _materialized_layout_gate(
        output_pdf,
        template,
        page.facts,
        result.patch,
    )
    if not materialized_layout_gate["passed"]:
        gate_rejection_codes.append("TM3_CHART_MATERIALIZED_LAYOUT_FAILED")
    product_acceptance = not gate_rejection_codes
    process = {
        "schema_version": "transflow.tm3-chart-pool-case/v1",
        "sample_id": sample_id,
        "direction": f"{source_language}->{target_language}",
        "source_hash": source_hash,
        "status": "PASS" if product_acceptance else "FAIL",
        "product_acceptance": product_acceptance,
        "artifact_mode": (
            "PRODUCT_ACCEPTED"
            if product_acceptance
            else "TRANSLATED_GATE_REJECTED"
        ),
        "gate_rejection_codes": gate_rejection_codes,
        "template": {
            "container_count": len(template.containers),
            "protected_object_count": len(template.protected_object_ids),
            "structure_hash": template.structure_hash,
        },
        "translation": {
            "batch_hash": content_sha256(batch),
            "bundle_hash": content_sha256(result.translation_bundle),
            "provider_records": [
                {
                    "bundle_hash": bundle_hash,
                    "bundle_path": _relative(path, REPO_ROOT),
                    "call_kind": call_kind,
                    "unit_count": len(recorded_batch.units),
                }
                for recorded_batch, _, bundle_hash, path, call_kind in case_records
            ],
            "translated_unit_count": len(result.translation_bundle.units),
        },
        "outcome": {
            "fallback": result.outcome.fallback.value,
            "finding_codes": list(result.outcome.finding_codes),
            "patch_operation_count": len(result.patch.operations),
            "quality": result.outcome.quality.value,
            "translation_coverage": result.outcome.translation_coverage.value,
        },
        "layout_gate": layout_gate,
        "layout_rule_trace": list(rule_trace),
        "materialization": metrics,
        "materialized_layout_gate": materialized_layout_gate,
        "artifacts": {
            name: _relative(path, run_root)
            for name, path in artifacts.items()
        },
    }
    _write_json(case_root / "process/case_manifest.json", process, run_root)
    if gate_rejection_codes:
        raise MigrationContractError(
            gate_rejection_codes[0],
            sample_id,
        )
    return process


def run(run_id: str) -> Path:
    """Execute all 30 cases and preserve complete evidence under one new run."""

    if not migration_translation_environment_ready():
        raise MigrationContractError(
            "REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED",
            "TM3 chart pool environment is incomplete",
        )
    run_root = RUNS_ROOT / run_id
    if run_root.exists():
        raise MigrationContractError("RUN_ID_ALREADY_EXISTS", run_id)
    run_root.mkdir(parents=True)
    records = _load_cases()
    _write_json(
        run_root / "input/pool_manifest.json",
        {
            "schema_version": "transflow.tm3-chart-pool-input/v1",
            "case_count": len(records),
            "manifest_hash": _sha256_file(POOL_MANIFEST),
            "manifest_ref": _relative(POOL_MANIFEST, REPO_ROOT),
            "directions": {
                "en->zh-CN": sum(
                    item["source_language"] == "en" for item in records
                ),
                "zh-CN->en": sum(
                    item["source_language"] == "zh-CN" for item in records
                ),
            },
            "cases": records,
        },
        run_root,
    )
    fonts = ControlledFontRegistry(REPO_ROOT / FONT_MANIFEST, REPO_ROOT)
    font_path = fonts.resolve(FONT_ID).path
    interpreter = PagePatchInterpreter(fonts)
    base_policy = load_p8_toolbox_policy(REPO_ROOT / P8_POLICY)
    adapter = MigrationQwenTranslationAdapter(
        timeout_seconds=180.0,
        chunk_size=48,
        system_prompt=chart_translation_system_prompt(),
    )
    translation_port = _RecordingTranslationPort(
        adapter,
        run_root / "process/translation_store",
    )
    case_results: list[dict[str, object]] = []
    case_artifacts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for index, record in enumerate(records, start=1):
        try:
            result = _run_case(
                record=record,
                index=index,
                run_id=run_id,
                run_root=run_root,
                base_policy=base_policy,
                font_path=font_path,
                interpreter=interpreter,
                translation_port=translation_port,
            )
            case_results.append(result)
            case_artifacts.append(
                {
                    "sample_id": result["sample_id"],
                    "direction": result["direction"],
                    "status": "PASS",
                    "product_acceptance": True,
                    "artifact_mode": result["artifact_mode"],
                    "output": result["artifacts"]["output"],
                    "review": result["artifacts"]["review"],
                    "process": (
                        f"cases/{index:02d}-{record['sample_id']}"
                        "/process/case_manifest.json"
                    ),
                }
            )
            print(
                f"[{index:02d}/30] PASS {record['sample_id']} "
                f"{record['source_language']}->{record['target_language']}",
                flush=True,
            )
        except Exception as error:
            sample_id = str(record["sample_id"])
            artifact = _ensure_failure_artifacts(
                case_root=run_root / "cases" / f"{index:02d}-{sample_id}",
                source=CHART_ROOT / str(record["source_ref"]),
                run_root=run_root,
                sample_id=sample_id,
                error=error,
            )
            artifact["direction"] = (
                f"{record['source_language']}->{record['target_language']}"
            )
            case_artifacts.append(artifact)
            failures.append(
                {
                    "sample_id": sample_id,
                    "error_type": type(error).__name__,
                    "detail": str(error),
                    "artifact_mode": artifact["artifact_mode"],
                    "output": artifact["output"],
                    "review": artifact["review"],
                    "process": artifact["process"],
                }
            )
            print(
                f"[{index:02d}/30] FAIL {record['sample_id']} "
                f"{type(error).__name__}",
                flush=True,
            )
    status = "PASS" if not failures and len(case_results) == 30 else "FAIL"
    summary = {
        "schema_version": "transflow.tm3-chart-pool-run/v1",
        "run_id": run_id,
        "status": status,
        "case_count": 30,
        "passed_case_count": len(case_results),
        "failed_case_count": len(failures),
        "real_provider_call_count": adapter.call_count,
        "mock_response_count": 0,
        "raw_provider_response_persisted": False,
        "failures": failures,
        "cases": case_artifacts,
    }
    _write_json(run_root / "run_manifest.json", summary, run_root)
    (run_root / "report.md").write_text(
        "\n".join(
            [
                "# TM3 body.chart 30-page real translation regression",
                "",
                f"- Run: `{run_id}`",
                f"- Status: `{status}`",
                f"- Passed: `{len(case_results)}/30`",
                f"- Real provider calls: `{adapter.call_count}`",
                "- Directions: `15 en->zh-CN`, `15 zh-CN->en`",
                "- Every PASS/FAIL case contains an inspectable PDF, process data and comparison.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if status != "PASS":
        raise MigrationContractError(
            "TM3_CHART_POOL_REGRESSION_FAILED",
            ",".join(item["sample_id"] for item in failures),
        )
    return run_root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TM3 real translation on the frozen 30-page chart pool"
    )
    parser.add_argument(
        "--run-id",
        default=f"06-body-chart-30-page-regression-{datetime.now():%Y%m%d-%H%M%S}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        run_root = run(arguments.run_id)
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run": _relative(run_root, REPO_ROOT),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except MigrationContractError as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_code": error.code,
                    "detail": error.detail,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
