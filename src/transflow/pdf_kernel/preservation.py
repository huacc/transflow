"""实现 P6 PDF Preservation 支持矩阵、预检、快照和发布后验证。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pymupdf

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError

LOGGER = logging.getLogger("transflow.pdf_kernel.preservation")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_SUPPORT_MATRIX = (
    REPOSITORY_ROOT / "resources" / "manifests" / "p6_preservation_support.json"
)
DEFAULT_FIXTURE_CATALOG = (
    REPOSITORY_ROOT / "resources" / "manifests" / "p6_fixture_catalog.json"
)
KNOWN_DETECTORS = frozenset(
    {
        "document.metadata",
        "document.get_toc",
        "document.get_page_labels",
        "page.get_links",
        "page.annots",
        "page.widgets",
        "document.embfile_names",
        "signature_widgets",
        "document.needs_pass",
        "catalog_key",
    }
)
KNOWN_VALIDATORS = frozenset(
    {
        "stable_metadata_hash",
        "stable_toc_hash",
        "stable_page_label_hash",
        "stable_link_hash",
        "stable_annotation_hash",
        "stable_form_hash",
        "stable_attachment_hash",
        "whole_source_byte_identity",
    }
)
RectTuple = tuple[float, float, float, float]


class FeatureDisposition(StrEnum):
    """定义一个 PDF 特征在 P6 中的唯一处理承诺。"""

    VERIFY = "VERIFY"
    PASSTHROUGH = "PASSTHROUGH"


class PreflightDecision(StrEnum):
    """定义文档进入写路径、整文透传或直接失败的预检结果。"""

    PROCESS = "PROCESS"
    PASSTHROUGH = "PASSTHROUGH"
    PROCESS_FAILED = "PROCESS_FAILED"


def _rect_tuple(rect: pymupdf.Rect) -> RectTuple:
    """把页面框规范为可跨进程比较的四位小数坐标。"""

    return (
        round(float(rect.x0), 4),
        round(float(rect.y0), 4),
        round(float(rect.x1), 4),
        round(float(rect.y1), 4),
    )


def _content_hash(document: pymupdf.Document, page: pymupdf.Page) -> str:
    """聚合页面内容流，证明未批准页面没有被重建或修改。"""

    digest = hashlib.sha256()
    for xref in page.get_contents():
        digest.update(int(xref).to_bytes(8, "big"))
        digest.update(document.xref_stream(xref))
    return digest.hexdigest()


def _mechanical_value(value: object) -> object:
    """去除 PyMuPDF 进程对象和非确定字段，形成稳定 JSON 值。"""

    if isinstance(value, pymupdf.Rect):
        return _rect_tuple(value)
    if isinstance(value, pymupdf.Point):
        return (round(float(value.x), 4), round(float(value.y), 4))
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, dict):
        ignored = {"xref", "id"}
        return {
            str(key): _mechanical_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
            if str(key) not in ignored
        }
    if isinstance(value, list | tuple):
        return tuple(_mechanical_value(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class FeatureSupport:
    """记录一个显式 PDF 特征的处置、检测器、验证器和真实 fixture 身份。"""

    name: str
    disposition: FeatureDisposition
    detector: str
    validator: str
    fixture_id: str
    catalog_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreservationSupportMatrix:
    """保存 P6 唯一受版本控制的 Preservation 支持矩阵。"""

    schema_version: str
    features: tuple[FeatureSupport, ...]
    matrix_hash: str

    def feature(self, name: str) -> FeatureSupport:
        """按稳定名称返回特征定义，未声明的能力绝不隐式承诺。"""

        for feature in self.features:
            if feature.name == name:
                return feature
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"支持矩阵缺少特征: {name}")


def load_support_matrix(
    path: Path = DEFAULT_SUPPORT_MATRIX,
    fixture_catalog_path: Path = DEFAULT_FIXTURE_CATALOG,
) -> PreservationSupportMatrix:
    """从集中配置文件读取并严格校验 P6 Preservation 支持矩阵。"""

    LOGGER.info("调用 Preservation 支持矩阵，意图=冻结特征承诺 path=%s", path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.preservation-support/v1":
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵版本不受支持")
    raw_features = payload.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵没有特征")
    fixture_catalog = json.loads(fixture_catalog_path.read_text(encoding="utf-8"))
    if fixture_catalog.get("schema_version") != "transflow.p6-fixture-catalog/v1":
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "P6 fixture catalog 版本无效")
    fixture_items = fixture_catalog.get("fixtures")
    if not isinstance(fixture_items, list):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "P6 fixture catalog 格式无效")
    fixture_ids = {
        item.get("fixture_id")
        for item in fixture_items
        if isinstance(item, dict)
        and isinstance(item.get("fixture_id"), str)
        and isinstance(item.get("test_id"), str)
        and isinstance(item.get("producer"), str)
    }
    features: list[FeatureSupport] = []
    for item in raw_features:
        if not isinstance(item, dict):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵特征格式无效")
        required = ("name", "disposition", "detector", "validator", "fixture_id")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵缺必填证据")
        if item["detector"] not in KNOWN_DETECTORS or item["validator"] not in KNOWN_VALIDATORS:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵引用未实现检测或验证器")
        if item["fixture_id"] not in fixture_ids:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵引用未登记 fixture")
        catalog_keys = item.get("catalog_keys", [])
        if not isinstance(catalog_keys, list) or any(
            not isinstance(value, str) or not value for value in catalog_keys
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "catalog_keys 格式无效")
        features.append(
            FeatureSupport(
                name=item["name"],
                disposition=FeatureDisposition(item["disposition"]),
                detector=item["detector"],
                validator=item["validator"],
                fixture_id=item["fixture_id"],
                catalog_keys=tuple(catalog_keys),
            )
        )
    names = tuple(item.name for item in features)
    if len(names) != len(set(names)):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "支持矩阵特征名重复")
    return PreservationSupportMatrix(
        schema_version=payload["schema_version"],
        features=tuple(features),
        matrix_hash=content_sha256(payload),
    )


@dataclass(frozen=True, slots=True)
class PageStructure:
    """记录 Preservation 所需的页序、框、旋转和内容流事实。"""

    page_no: int
    page_xref: int
    media_box: RectTuple
    crop_box: RectTuple
    rotation: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class DocumentStructure:
    """记录一份 PDF 的页面结构和 P6 文档级特征快照。"""

    pages: tuple[PageStructure, ...]
    feature_hashes: tuple[tuple[str, str], ...] = ()
    feature_counts: tuple[tuple[str, int], ...] = ()
    encrypted: bool = False
    signature_count: int = 0
    catalog_features: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        """返回快照中的完整页数。"""

        return len(self.pages)

    def feature_hash(self, name: str) -> str:
        """读取某个稳定文档特征哈希，不存在时返回空集合哈希。"""

        return dict(self.feature_hashes).get(name, content_sha256(()))

    def feature_count(self, name: str) -> int:
        """读取某个稳定文档特征数量，不存在时返回零。"""

        return dict(self.feature_counts).get(name, 0)


@dataclass(frozen=True, slots=True)
class PreservationPreflightResult:
    """记录写入前的处置、全部原因、支持矩阵和可选结构快照。"""

    decision: PreflightDecision
    reason_codes: tuple[str, ...]
    matrix_hash: str
    structure: DocumentStructure | None


@dataclass(frozen=True, slots=True)
class PreservationResult:
    """返回结构与显式 PDF 特征是否通过及全部稳定失败码。"""

    passed: bool
    page_count_rate: float
    page_order_rate: float
    geometry_rate: float
    unmodified_content_rate: float
    failure_codes: tuple[str, ...]
    verified_features: tuple[str, ...] = ()


def _catalog_has_key(document: pymupdf.Document, key: str) -> bool:
    """只读检测 Catalog 键是否存在且不是 PDF null。"""

    value_type, value = document.xref_get_key(document.pdf_catalog(), key)
    return value_type != "null" and value != "null"


def _feature_payloads(document: pymupdf.Document) -> dict[str, tuple[object, ...]]:
    """一次遍历收集 metadata、书签、链接、注释、表单与附件稳定事实。"""

    links: list[object] = []
    annotations: list[object] = []
    forms: list[object] = []
    signature_count = 0
    for page_index in range(document.page_count):
        page_no = page_index + 1
        page = document.load_page(page_index)
        links.extend(
            {"page_no": page_no, "link": _mechanical_value(link)}
            for link in page.get_links()
        )
        annotations.extend(
            {
                "page_no": page_no,
                "type": annotation.type[1],
                "rect": _rect_tuple(annotation.rect),
                "info": _mechanical_value(annotation.info),
                "flags": int(annotation.flags),
            }
            for annotation in (page.annots() or ())
        )
        for widget in page.widgets() or ():
            widget_payload = {
                "page_no": page_no,
                "field_name": str(widget.field_name or ""),
                "field_type": int(widget.field_type),
                "field_value": _mechanical_value(widget.field_value),
                "field_flags": int(widget.field_flags),
                "rect": _rect_tuple(widget.rect),
            }
            forms.append(widget_payload)
            signature_count += int(widget.field_type == 6)
    attachments = tuple(
        {
            "name": name,
            "content": _mechanical_value(document.embfile_get(name)),
        }
        for name in sorted(document.embfile_names())
    )
    metadata_items = (document.metadata or {}).items()
    metadata = tuple(
        sorted((str(key), str(value or "")) for key, value in metadata_items)
    )
    return {
        "metadata": (metadata,),
        "bookmarks": tuple(_mechanical_value(item) for item in document.get_toc(simple=False)),
        "page_labels": tuple(_mechanical_value(item) for item in document.get_page_labels()),
        "links": tuple(links),
        "annotations": tuple(annotations),
        "forms": tuple(forms),
        "attachments": attachments,
        "digital_signatures": tuple(range(signature_count)),
    }


def _open_authenticated(path: Path, password: str | None) -> tuple[pymupdf.Document, bool]:
    """打开 PDF 并在需要时认证；无法认证时返回稳定来源错误。"""

    try:
        document = pymupdf.open(path)
    except Exception as error:
        raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "PDF 无法打开") from error
    encrypted = bool(document.needs_pass)
    if encrypted and (not password or document.authenticate(password) <= 0):
        document.close()
        raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "加密 PDF 无法认证")
    return document, encrypted


def capture_document_structure(
    path: Path,
    *,
    password: str | None = None,
    support_matrix: PreservationSupportMatrix | None = None,
) -> DocumentStructure:
    """真实打开 PDF 并捕获页面及全部显式支持特征快照。"""

    LOGGER.info("调用文档结构快照，意图=登记 Preservation 基线 path=%s", path.name)
    document, encrypted = _open_authenticated(path, password)
    try:
        pages = tuple(
            PageStructure(
                page_no=index + 1,
                page_xref=document.load_page(index).xref,
                media_box=_rect_tuple(document.load_page(index).mediabox),
                crop_box=_rect_tuple(document.load_page(index).cropbox),
                rotation=int(document.load_page(index).rotation),
                content_hash=_content_hash(document, document.load_page(index)),
            )
            for index in range(document.page_count)
        )
        payloads = _feature_payloads(document)
        matrix = support_matrix or load_support_matrix()
        catalog_features = tuple(
            feature.name
            for feature in matrix.features
            if feature.catalog_keys
            and any(_catalog_has_key(document, key) for key in feature.catalog_keys)
        )
        signature_count = len(payloads["digital_signatures"])
        hashes = tuple(
            (feature.name, content_sha256(payloads.get(feature.name, ())))
            for feature in matrix.features
        )
        counts = tuple(
            (feature.name, len(payloads.get(feature.name, ())))
            for feature in matrix.features
        )
        return DocumentStructure(
            pages=pages,
            feature_hashes=hashes,
            feature_counts=counts,
            encrypted=encrypted,
            signature_count=signature_count,
            catalog_features=catalog_features,
        )
    finally:
        document.close()


def preflight_document(
    path: Path,
    *,
    support_matrix_path: Path = DEFAULT_SUPPORT_MATRIX,
    password: str | None = None,
) -> PreservationPreflightResult:
    """写入前检测不安全特征；未知或不可验证能力一律不进入修改路径。"""

    matrix = load_support_matrix(support_matrix_path)
    try:
        structure = capture_document_structure(
            path,
            password=password,
            support_matrix=matrix,
        )
    except PortCallError:
        return PreservationPreflightResult(
            PreflightDecision.PROCESS_FAILED,
            ("SOURCE_UNREADABLE_OR_PASSWORD_REQUIRED",),
            matrix.matrix_hash,
            None,
        )
    reasons: list[str] = []
    for feature in matrix.features:
        if feature.disposition is not FeatureDisposition.PASSTHROUGH:
            continue
        present = (
            (feature.name == "encryption" and structure.encrypted)
            or (
                feature.name == "digital_signatures"
                and structure.signature_count > 0
            )
            or feature.name in structure.catalog_features
        )
        if present:
            reasons.append(f"UNSAFE_{feature.name.upper()}")
    decision = PreflightDecision.PASSTHROUGH if reasons else PreflightDecision.PROCESS
    LOGGER.info(
        "调用 Preservation 预检，意图=决定修改或整文透传 decision=%s reasons=%s",
        decision,
        reasons,
    )
    return PreservationPreflightResult(
        decision,
        tuple(sorted(reasons)),
        matrix.matrix_hash,
        structure,
    )


def validate_minimal_preservation(
    source: DocumentStructure,
    target: DocumentStructure,
    modified_page_numbers: frozenset[int],
) -> PreservationResult:
    """兼容 P4 API，校验页数、页序、几何和所有未修改页内容流。"""

    LOGGER.info("调用最小 Preservation 校验，意图=阻止漏页、乱序和越权修改")
    failures: list[str] = []
    page_count_rate = 1.0 if source.page_count == target.page_count else 0.0
    if page_count_rate != 1.0:
        failures.append("PAGE_COUNT_CHANGED")
    comparable = min(source.page_count, target.page_count)
    order_matches = sum(
        source.pages[index].page_xref == target.pages[index].page_xref
        for index in range(comparable)
    )
    page_order_rate = order_matches / source.page_count if source.page_count else 0.0
    if page_order_rate != 1.0:
        failures.append("PAGE_ORDER_CHANGED")
    geometry_matches = sum(
        (
            source.pages[index].media_box,
            source.pages[index].crop_box,
            source.pages[index].rotation,
        )
        == (
            target.pages[index].media_box,
            target.pages[index].crop_box,
            target.pages[index].rotation,
        )
        for index in range(comparable)
    )
    geometry_rate = geometry_matches / source.page_count if source.page_count else 0.0
    if geometry_rate != 1.0:
        failures.append("PAGE_GEOMETRY_CHANGED")
    unmodified = tuple(item for item in source.pages if item.page_no not in modified_page_numbers)
    content_matches = sum(
        item.page_no <= target.page_count
        and item.content_hash == target.pages[item.page_no - 1].content_hash
        for item in unmodified
    )
    unmodified_rate = content_matches / len(unmodified) if unmodified else 1.0
    if unmodified_rate != 1.0:
        failures.append("UNAUTHORIZED_PAGE_CHANGED")
    return PreservationResult(
        passed=not failures,
        page_count_rate=page_count_rate,
        page_order_rate=page_order_rate,
        geometry_rate=geometry_rate,
        unmodified_content_rate=unmodified_rate,
        failure_codes=tuple(failures),
    )


def validate_preservation(
    source: DocumentStructure,
    target: DocumentStructure,
    modified_page_numbers: frozenset[int],
    support_matrix: PreservationSupportMatrix,
) -> PreservationResult:
    """在最小页面合同之上逐项验证支持矩阵声明的全部 VERIFY 特征。"""

    minimal = validate_minimal_preservation(source, target, modified_page_numbers)
    failures = list(minimal.failure_codes)
    verified: list[str] = []
    for feature in support_matrix.features:
        if feature.disposition is not FeatureDisposition.VERIFY:
            continue
        verified.append(feature.name)
        if (
            source.feature_hash(feature.name) != target.feature_hash(feature.name)
            or source.feature_count(feature.name) != target.feature_count(feature.name)
        ):
            failures.append(f"FEATURE_{feature.name.upper()}_CHANGED")
    return PreservationResult(
        passed=not failures,
        page_count_rate=minimal.page_count_rate,
        page_order_rate=minimal.page_order_rate,
        geometry_rate=minimal.geometry_rate,
        unmodified_content_rate=minimal.unmodified_content_rate,
        failure_codes=tuple(sorted(set(failures))),
        verified_features=tuple(verified),
    )


def main() -> int:
    """读取支持矩阵，展示 P6 不对未列入矩阵的 PDF 特征作能力承诺。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    matrix = load_support_matrix()
    LOGGER.info("Preservation 示例，意图=展示显式支持项 count=%s", len(matrix.features))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
