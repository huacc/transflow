from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    pass


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name}_is_required")


def _sha256(value: str, name: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise ContractError(f"{name}_must_be_lowercase_sha256")


@dataclass(frozen=True)
class SampleManifest:
    sample_id: str
    classification_path: str
    leaf_key: str | None
    upstream_pdf: str
    upstream_sha256: str
    snapshot_pdf: str
    snapshot_sha256: str
    original_document_id: str
    original_page_number: int
    source_document_sha256: str

    def __post_init__(self) -> None:
        for name in ("sample_id", "classification_path", "upstream_pdf", "snapshot_pdf", "original_document_id"):
            _required(getattr(self, name), name)
        if self.leaf_key is not None:
            _required(self.leaf_key, "leaf_key")
        _sha256(self.upstream_sha256, "upstream_sha256")
        _sha256(self.snapshot_sha256, "snapshot_sha256")
        _sha256(self.source_document_sha256, "source_document_sha256")
        if self.original_page_number < 1:
            raise ContractError("original_page_number_must_be_positive")


RectTuple = tuple[float, float, float, float]


def _rect(value: RectTuple, name: str) -> None:
    if len(value) != 4 or value[2] <= value[0] or value[3] <= value[1]:
        raise ContractError(f"{name}_must_be_nonempty_rect")


@dataclass(frozen=True)
class TextObjectFact:
    object_id: str
    text: str
    bbox: RectTuple
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int
    span_index: int

    def __post_init__(self) -> None:
        _required(self.object_id, "object_id")
        _rect(self.bbox, "bbox")


@dataclass(frozen=True)
class ImageObjectFact:
    object_id: str
    bbox: RectTuple
    width: int
    height: int
    content_sha256: str

    def __post_init__(self) -> None:
        _required(self.object_id, "object_id")
        _rect(self.bbox, "bbox")
        _sha256(self.content_sha256, "content_sha256")


@dataclass(frozen=True)
class DrawingObjectFact:
    object_id: str
    bbox: RectTuple
    content_sha256: str

    def __post_init__(self) -> None:
        _required(self.object_id, "object_id")
        _rect(self.bbox, "bbox")
        _sha256(self.content_sha256, "content_sha256")


@dataclass(frozen=True)
class PageFacts:
    page_id: str
    source_pdf_sha256: str
    width: float
    height: float
    native_text_object_count: int
    origin: str
    page_index: int = 0
    rotation: int = 0
    text_objects: tuple[TextObjectFact, ...] = ()
    image_objects: tuple[ImageObjectFact, ...] = ()
    drawing_objects: tuple[DrawingObjectFact, ...] = ()
    geometry_sha256: str | None = None
    text_objects_sha256: str | None = None
    locked_objects_sha256: str | None = None

    def __post_init__(self) -> None:
        _required(self.page_id, "page_id")
        _required(self.origin, "origin")
        _sha256(self.source_pdf_sha256, "source_pdf_sha256")
        if self.width <= 0 or self.height <= 0:
            raise ContractError("page_dimensions_must_be_positive")
        if self.native_text_object_count < 0:
            raise ContractError("native_text_object_count_must_be_nonnegative")
        if self.page_index < 0:
            raise ContractError("page_index_must_be_nonnegative")
        for name in ("geometry_sha256", "text_objects_sha256", "locked_objects_sha256"):
            value = getattr(self, name)
            if value is not None:
                _sha256(value, name)


@dataclass(frozen=True)
class TranslationUnit:
    container_id: str
    source_text: str
    reading_order: int

    def __post_init__(self) -> None:
        _required(self.container_id, "container_id")
        _required(self.source_text, "source_text")
        if self.reading_order < 0:
            raise ContractError("reading_order_must_be_nonnegative")


@dataclass(frozen=True)
class PageTemplate:
    page_id: str
    toolbox_key: str
    containers: tuple[TranslationUnit, ...]

    def __post_init__(self) -> None:
        _required(self.page_id, "page_id")
        _required(self.toolbox_key, "toolbox_key")
        _validate_units(self.containers)


@dataclass(frozen=True)
class PageTranslationRequest:
    request_id: str
    page_id: str
    source_language: str
    target_language: str
    units: tuple[TranslationUnit, ...]

    def __post_init__(self) -> None:
        for name in ("request_id", "page_id", "source_language", "target_language"):
            _required(getattr(self, name), name)
        _validate_units(self.units)


def _validate_units(units: tuple[TranslationUnit, ...]) -> None:
    if not units:
        raise ContractError("translation_units_are_required")
    ids = [item.container_id for item in units]
    orders = [item.reading_order for item in units]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate_container_id")
    if len(orders) != len(set(orders)) or orders != sorted(orders):
        raise ContractError("reading_order_must_be_unique_and_sorted")


@dataclass(frozen=True)
class TranslationResult:
    container_id: str
    translated_text: str

    def __post_init__(self) -> None:
        _required(self.container_id, "container_id")
        _required(self.translated_text, "translated_text")


@dataclass(frozen=True)
class PageTranslationBundle:
    request_id: str
    page_id: str
    provider: str
    model: str
    translations: tuple[TranslationResult, ...]
    provider_request_id: str | None = None
    latency_ms: int | None = None
    response_sha256: str | None = None

    def validate_against(self, request: PageTranslationRequest) -> None:
        if self.request_id != request.request_id or self.page_id != request.page_id:
            raise ContractError("translation_bundle_request_mismatch")
        actual = [item.container_id for item in self.translations]
        expected = [item.container_id for item in request.units]
        if len(actual) != len(set(actual)):
            raise ContractError("duplicate_translation_container_id")
        if actual != expected:
            raise ContractError("translation_container_ids_must_match_request_order")


@dataclass(frozen=True)
class ContainerWrite:
    container_id: str
    translated_text: str
    output_bbox: RectTuple
    allowed_bbox: RectTuple
    font_file: str
    font_resource: str
    font_size: float
    line_height: float = 1.2

    def __post_init__(self) -> None:
        _required(self.container_id, "container_id")
        _required(self.translated_text, "translated_text")
        _required(self.font_file, "font_file")
        _required(self.font_resource, "font_resource")
        _rect(self.output_bbox, "output_bbox")
        _rect(self.allowed_bbox, "allowed_bbox")
        if self.font_size <= 0 or self.line_height <= 0:
            raise ContractError("font_size_and_line_height_must_be_positive")


@dataclass(frozen=True)
class PagePatch:
    page_id: str
    toolbox_key: str
    writes: tuple[ContainerWrite, ...]
    source_pdf_sha256: str
    page_index: int = 0

    def __post_init__(self) -> None:
        _required(self.page_id, "page_id")
        _required(self.toolbox_key, "toolbox_key")
        _sha256(self.source_pdf_sha256, "source_pdf_sha256")
        if self.page_index < 0:
            raise ContractError("page_index_must_be_nonnegative")
        ids = [item.container_id for item in self.writes]
        if not ids or len(ids) != len(set(ids)):
            raise ContractError("page_patch_writes_must_be_nonempty_and_unique")


@dataclass(frozen=True)
class Finding:
    finding_id: str
    owner: str
    severity: str
    message: str


@dataclass(frozen=True)
class PageQualityDecision:
    page_id: str
    process_verdict: str
    product_verdict: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    sample_id: str
    terminal_state: str
    process_verdict: str
    product_verdict: str
    versions: dict[str, Any]
    artifacts: tuple[ArtifactRef, ...]
    error_code: str | None = None


@dataclass(frozen=True)
class PromotionManifest:
    toolbox_key: str
    toolbox_version: str
    status: str
    evidence_refs: tuple[str, ...]


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
