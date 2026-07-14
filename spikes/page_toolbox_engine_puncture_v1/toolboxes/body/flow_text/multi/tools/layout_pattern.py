"""识别同一多栏正文页内部的排版范式，并为各区段绑定本工具箱的回填策略。"""

from __future__ import annotations

import hashlib
import json
import time
from statistics import median
from typing import Protocol

import httpx

from page_toolbox_puncture.translation import ProviderError, QwenConfig

from .models import FlowBand, MultiColumnTemplate


PATTERNS = (
    "multi_only",
    "single_multi",
    "multi_single",
    "single_multi_single",
    "repeated_multi_bands",
)


def build_flow_bands(template: MultiColumnTemplate) -> tuple[FlowBand, ...]:
    """按当前页的容器归属和纵向范围分段，不读取样本文字或固定坐标。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    column_ids = {item.column_id for item in template.columns}
    column_containers = [
        item for item in template.containers if assignment[item.container_id] in column_ids
    ]
    if not column_containers:
        raise ValueError("p5_flow_pattern_requires_column_content")

    spans = [item for item in template.containers if assignment[item.container_id] == "span"]
    bands: list[FlowBand] = []
    multi_variant = infer_multi_band_variant(template)
    content = sorted(
        [(item, "single") for item in spans]
        + [(item, "multi") for item in column_containers],
        key=lambda item: (item[0].source_bbox[1], item[0].source_bbox[0]),
    )
    grouped: list[tuple[str, list[object]]] = []
    for container, mode in content:
        if not grouped or grouped[-1][0] != mode:
            grouped.append((mode, [container]))
        else:
            grouped[-1][1].append(container)
    for mode, containers in grouped:
        if mode == "single":
            bands.append(_band("single", containers, 1, "multi_owned_single_vertical_reflow"))
        else:
            bands.append(
                FlowBand(
                    band_id="flow-band-multi",
                    reading_order=0,
                    mode="multi",
                    top=round(min(item.source_bbox[1] for item in containers), 4),
                    bottom=round(max(item.source_bbox[3] for item in containers), 4),
                    column_count=len(template.columns),
                    container_ids=tuple(item.container_id for item in containers),
                    refill_strategy=(
                        "paired_row_synchronous_reflow"
                        if multi_variant == "paired_row_columns"
                        else "independent_column_vertical_reflow"
                    ),
                )
            )

    for mode, owner, strategy in (
        ("fixed", "fixed", "locked_visual_overlay_refill"),
        ("margin", "margin", "bottom_anchored_margin_refill"),
    ):
        containers = [item for item in template.containers if assignment[item.container_id] == owner]
        if containers:
            bands.append(_band(mode, containers, 0, strategy))

    # 固定区可能与正文纵向重叠；reading_order 仅表达源页自上而下的检查顺序。
    ordered = sorted(bands, key=lambda item: (item.top, item.bottom, item.mode))
    return tuple(
        FlowBand(
            band_id=f"flow-band-{index:02d}-{item.mode}",
            reading_order=index,
            mode=item.mode,
            top=item.top,
            bottom=item.bottom,
            column_count=item.column_count,
            container_ids=item.container_ids,
            refill_strategy=item.refill_strategy,
        )
        for index, item in enumerate(ordered, start=1)
    )


def infer_multi_band_variant(template: MultiColumnTemplate) -> str:
    """用重复行首对齐和短单元证据区分成对行与独立栏流。"""

    if len(template.columns) != 2:
        return "independent_columns"
    assignment = {item.container_id: item.column_id for item in template.assignments}
    values = [
        [item for item in template.containers if assignment[item.container_id] == column.column_id]
        for column in template.columns
    ]
    if min(len(items) for items in values) < 4:
        return "independent_columns"
    aligned = sum(
        1
        for left in values[0]
        if any(
            abs(left.source_bbox[1] - right.source_bbox[1])
            <= max(left.font_size, right.font_size) * 0.45
            for right in values[1]
        )
    )
    shorter = min(len(items) for items in values)
    height_ratios = sorted(
        (item.source_bbox[3] - item.source_bbox[1]) / max(item.font_size, 0.01)
        for items in values
        for item in items
    )
    median_height_ratio = height_ratios[len(height_ratios) // 2]
    short_cell_evidence = min(
        median(
            (item.source_bbox[2] - item.source_bbox[0])
            / max(column.right - column.left, item.font_size)
            for item in items
        )
        for column, items in zip(template.columns, values)
    )
    if (
        aligned >= 3
        and aligned / shorter >= 0.60
        and median_height_ratio <= 4.5
        and short_cell_evidence <= 0.95
    ):
        return "paired_row_columns"
    return "independent_columns"


def build_layout_pattern_rule_decision(template: MultiColumnTemplate) -> dict[str, object]:
    bands = build_flow_bands(template)
    content_modes = [item.mode for item in bands if item.mode in {"single", "multi"}]
    pattern_by_modes = {
        ("multi",): "multi_only",
        ("single", "multi"): "single_multi",
        ("multi", "single"): "multi_single",
        ("single", "multi", "single"): "single_multi_single",
    }
    pattern = pattern_by_modes.get(tuple(content_modes))
    if pattern is None and content_modes.count("multi") >= 2 and all(
        previous != current for previous, current in zip(content_modes, content_modes[1:])
    ):
        pattern = "repeated_multi_bands"
    if pattern is None:
        raise ValueError(f"p5_unsupported_layout_pattern:{'-'.join(content_modes)}")
    return {
        "schema_version": "p5-layout-pattern-rule/v1",
        "rule_verdict": "PATTERN_CANDIDATE_READY",
        "pattern": pattern,
        "multi_band_variant": infer_multi_band_variant(template),
        "content_band_modes": content_modes,
        "flow_bands": bands,
        "evidence_basis": "current_page_container_ownership_and_source_vertical_intervals",
    }


class LayoutPatternAdjudicator(Protocol):
    def adjudicate(self, rule_decision: dict[str, object]) -> dict[str, object]: ...


class QwenLayoutPatternAdjudicator:
    """让千问一次只复核范式，不同时决定坐标、间距或工具参数。"""

    def __init__(self, config: QwenConfig, prompt_text: str) -> None:
        self.config = config
        self.prompt_text = prompt_text

    def adjudicate(self, rule_decision: dict[str, object]) -> dict[str, object]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.prompt_text},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "rule_pattern": rule_decision["pattern"],
                            "rule_multi_band_variant": rule_decision["multi_band_variant"],
                            "content_band_modes": rule_decision["content_band_modes"],
                            "flow_bands": [
                                {
                                    "reading_order": item.reading_order,
                                    "mode": item.mode,
                                    "top": item.top,
                                    "bottom": item.bottom,
                                    "column_count": item.column_count,
                                    "refill_strategy": item.refill_strategy,
                                }
                                for item in rule_decision["flow_bands"]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "top_p": 1,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "layout_pattern_adjudication",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "enum": list(PATTERNS)},
                            "multi_band_variant": {
                                "type": "string",
                                "enum": ["independent_columns", "paired_row_columns"],
                            },
                            "reason": {"type": "string", "minLength": 1},
                        },
                        "required": ["pattern", "multi_band_variant", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "max_tokens": 512,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            latency_ms = round((time.perf_counter() - started) * 1000)
            response_sha256 = hashlib.sha256(response.content).hexdigest()
            response.raise_for_status()
            body = response.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(content)
            pattern = str(parsed.get("pattern", ""))
            multi_band_variant = str(parsed.get("multi_band_variant", ""))
            reason = str(parsed.get("reason", "")).strip()
            if pattern not in PATTERNS or multi_band_variant not in {"independent_columns", "paired_row_columns"} or not reason:
                raise ProviderError("INVALID_LAYOUT_PATTERN_RESPONSE")
            return {
                "schema_version": "p5-layout-pattern-qwen/v1",
                "judge": "qwen",
                "model": self.config.model,
                "pattern": pattern,
                "multi_band_variant": multi_band_variant,
                "reason": reason,
                "provider_request_id": response.headers.get("x-request-id") or body.get("id"),
                "latency_ms": latency_ms,
                "response_sha256": response_sha256,
            }
        except ProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"QWEN_LAYOUT_PATTERN_HTTP_{exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"QWEN_LAYOUT_PATTERN_{type(exc).__name__}") from exc


def _band(mode: str, containers, column_count: int, strategy: str) -> FlowBand:
    return FlowBand(
        band_id=f"flow-band-{mode}",
        reading_order=0,
        mode=mode,
        top=round(min(item.source_bbox[1] for item in containers), 4),
        bottom=round(max(item.source_bbox[3] for item in containers), 4),
        column_count=column_count,
        container_ids=tuple(item.container_id for item in containers),
        refill_strategy=strategy,
    )
