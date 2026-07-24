"""从完整只读 PDF 提取 P4 需要的稳定页面直接事实。"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pymupdf

from transflow.domain.common import canonical_json_bytes, content_sha256, json_ready
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageFacts

LOGGER = logging.getLogger("transflow.pdf_kernel.facts")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FACTS_SCHEMA_VERSION = "transflow.pdf-kernel.facts/v2"
RectTuple = tuple[float, float, float, float]


def _sha256_file(path: Path) -> str:
    """流式计算源 PDF 哈希，避免把完整年报一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rect_tuple(rect: pymupdf.Rect) -> RectTuple:
    """把 PyMuPDF 矩形转换为可序列化、精度稳定的四元组。"""

    return (
        round(float(rect.x0), 4),
        round(float(rect.y0), 4),
        round(float(rect.x1), 4),
        round(float(rect.y1), 4),
    )


def _values_rect_tuple(values: object) -> RectTuple:
    """把 PyMuPDF 返回的四元素坐标值转换为固定长度矩形。"""

    x0, y0, x1, y1 = cast(tuple[float, float, float, float], values)
    return (
        round(float(x0), 4),
        round(float(y0), 4),
        round(float(x1), 4),
        round(float(y1), 4),
    )


def _mechanical_json_value(value: object) -> object:
    """把 PyMuPDF 几何和绘图值规范化为不含进程内对象的 JSON 数据。"""

    if isinstance(value, pymupdf.Rect):
        return _rect_tuple(value)
    if isinstance(value, pymupdf.Point):
        return (round(float(value.x), 4), round(float(value.y), 4))
    if isinstance(value, pymupdf.Quad):
        return tuple(
            _mechanical_json_value(point)
            for point in (value.ul, value.ur, value.ll, value.lr)
        )
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, dict):
        return {
            str(key): _mechanical_json_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, list | tuple):
        return tuple(_mechanical_json_value(item) for item in value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def stable_page_identity(source_hash: str, page_no: int, geometry_hash: str) -> str:
    """由源哈希、1-based 页码和几何哈希生成稳定页面身份。"""

    return hashlib.sha256(f"{source_hash}\0{page_no}\0{geometry_hash}".encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class PageObjectFact:
    """记录一个页面文本或受保护视觉对象的稳定身份与边界。"""

    object_id: str
    kind: str
    bbox: RectTuple
    text: str
    protected: bool


@dataclass(frozen=True, slots=True)
class KernelTextFact:
    """记录 span 级文字、字体、颜色和稳定来源位置。"""

    object_id: str
    text: str
    bbox: RectTuple
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int
    span_index: int


@dataclass(frozen=True, slots=True)
class KernelImageFact:
    """记录图片几何、像素尺寸和内容哈希，不读取图片内部语义。"""

    object_id: str
    bbox: RectTuple
    width: int
    height: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class KernelDrawingFact:
    """记录矢量绘图几何和规范化机械内容哈希。"""

    object_id: str
    bbox: RectTuple
    content_hash: str


@dataclass(frozen=True, slots=True)
class KernelTableFact:
    """记录 PyMuPDF 直接检测到的候选表格、单元格及原生文字归属。"""

    object_id: str
    bbox: RectTuple
    cell_bboxes: tuple[RectTuple, ...]
    text_object_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KernelAnnotationFact:
    """记录页面注释的稳定类型、边界和机械内容哈希。"""

    object_id: str
    bbox: RectTuple
    annotation_type: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class KernelLinkFact:
    """记录页面链接的稳定类型、边界和机械内容哈希。"""

    object_id: str
    bbox: RectTuple
    kind: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class KernelFontFact:
    """记录页面引用字体、嵌入状态及 ToUnicode 映射事实。"""

    object_id: str
    xref: int
    extension: str
    font_type: str
    base_font: str
    resource_name: str
    encoding: str
    embedded: bool
    has_to_unicode: bool
    content_hash: str


@dataclass(frozen=True, slots=True)
class PageTextSpanFact:
    """记录分类规则识别行列锚点所需的原生文字 span。"""

    block_id: str
    bbox: RectTuple
    text: str


@dataclass(frozen=True, slots=True)
class PageTextBlockFact:
    """记录分类证据使用的文字块、行数和稳定边界。"""

    block_id: str
    bbox: RectTuple
    line_count: int
    text: str


@dataclass(frozen=True, slots=True)
class PageClassificationFacts:
    """保存仅用于页面分类的结构事实和匿名页面渲染。"""

    page_image_png: bytes
    page_image_sha256: str
    font_sizes: tuple[float, ...]
    text_blocks: tuple[PageTextBlockFact, ...]
    layout_spans: tuple[PageTextSpanFact, ...]
    table_bboxes: tuple[RectTuple, ...]
    image_bboxes: tuple[RectTuple, ...]
    drawing_count: int


@dataclass(frozen=True, slots=True)
class ExtractedPageFacts:
    """组合纯领域 PageFacts 与 P4 Kernel 使用的机械对象事实。"""

    page: PageFacts
    page_identity: str
    media_box: RectTuple
    crop_box: RectTuple
    rotation: int
    objects: tuple[PageObjectFact, ...]
    text_spans: tuple[KernelTextFact, ...]
    image_objects: tuple[KernelImageFact, ...]
    drawing_objects: tuple[KernelDrawingFact, ...]
    table_objects: tuple[KernelTableFact, ...]
    annotation_objects: tuple[KernelAnnotationFact, ...]
    link_objects: tuple[KernelLinkFact, ...]
    font_objects: tuple[KernelFontFact, ...]
    locked_objects_hash: str
    kernel_facts_hash: str
    classification: PageClassificationFacts | None = None

    @property
    def owned_object_ids(self) -> tuple[str, ...]:
        """返回可由页面计划声明所有权的块级与 span 级原生文本对象。"""

        return tuple(
            dict.fromkeys(
                (
                    *(item.object_id for item in self.objects if not item.protected),
                    *(item.object_id for item in self.text_spans),
                )
            )
        )

    @property
    def protected_object_ids(self) -> tuple[str, ...]:
        """返回禁止 PagePatch 修改的视觉对象、注释和链接身份。"""

        return tuple(
            dict.fromkeys(
                (
                    *(item.object_id for item in self.objects if item.protected),
                    *(item.object_id for item in self.image_objects),
                    *(item.object_id for item in self.drawing_objects),
                    *(item.object_id for item in self.table_objects),
                    *(item.object_id for item in self.annotation_objects),
                    *(item.object_id for item in self.link_objects),
                )
            )
        )

    @property
    def protected_regions(self) -> tuple[RectTuple, ...]:
        """返回不可擦除区域；表格 bbox 仅作结构 anchor，允许改写表内文字。"""

        return tuple(
            dict.fromkeys(
                (
                    *(item.bbox for item in self.objects if item.protected),
                    *(item.bbox for item in self.image_objects),
                    *(item.bbox for item in self.drawing_objects),
                    *(item.bbox for item in self.annotation_objects),
                    *(item.bbox for item in self.link_objects),
                )
            )
        )

    @property
    def table_text_object_ids(self) -> tuple[str, ...]:
        """返回全部候选表格内部的原生 span 身份，保持首次出现顺序。"""

        return tuple(
            dict.fromkeys(
                object_id
                for table in self.table_objects
                for object_id in table.text_object_ids
            )
        )

    @property
    def outside_table_text_object_ids(self) -> tuple[str, ...]:
        """返回不属于候选表格的原生 span 身份。"""

        table_ids = set(self.table_text_object_ids)
        return tuple(
            item.object_id for item in self.text_spans if item.object_id not in table_ids
        )

    def to_serializable_dict(self) -> dict[str, Any]:
        """把完整事实 DTO 编码为 JSON 可序列化值，页面图像使用显式 Base64。"""

        payload = cast(dict[str, Any], json_ready(self))
        classification = payload.get("classification")
        if isinstance(classification, dict):
            image_bytes = self.classification.page_image_png if self.classification else b""
            classification["page_image_png"] = base64.b64encode(image_bytes).decode("ascii")
        return payload


def serialize_kernel_contract(value: object) -> bytes:
    """序列化允许的 Kernel DTO，并拒绝打开的 Document/Page 等宿主对象。"""

    if not isinstance(value, ExtractedPageFacts):
        raise DomainContractError(
            ErrorCode.PORT_CONTRACT_VIOLATION,
            "Kernel 序列化边界只接受 ExtractedPageFacts",
        )
    try:
        return canonical_json_bytes(value.to_serializable_dict())
    except (TypeError, ValueError) as error:
        raise DomainContractError(
            ErrorCode.PORT_CONTRACT_VIOLATION,
            "Kernel DTO 包含不可序列化对象",
        ) from error


def extract_page_contract_bytes(
    source_path: str,
    expected_hash: str,
    page_no: int,
) -> bytes:
    """供 ProcessPool 调用：独立打开一页并只返回规范 JSON 字节。"""

    facts = PageFactsExtractor().extract_page(Path(source_path), expected_hash, page_no)
    return serialize_kernel_contract(facts)


class PageFactsExtractor:
    """只读打开完整 PDF，并按原始顺序提取 1-based 页面事实。"""

    def page_count(self, source_path: Path, expected_hash: str) -> int:
        """只读取文档页数，不把任何页面对象带出打开边界。"""

        if _sha256_file(source_path) != expected_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "页数预检源哈希不一致")
        try:
            with pymupdf.open(source_path) as document:
                return document.page_count
        except Exception as error:
            raise PortCallError(
                ErrorCode.SOURCE_NOT_READABLE,
                False,
                f"页数读取失败:{type(error).__name__}",
            ) from error

    def iter_pages(
        self,
        source_path: Path,
        expected_hash: str,
        *,
        include_classification: bool = False,
    ) -> Iterator[ExtractedPageFacts]:
        """一次打开文档但逐页产出事实，调用方消费后即可释放当前页重对象。"""

        LOGGER.info("调用流式页面事实提取，意图=逐页建立事实 path=%s", source_path.name)
        if _sha256_file(source_path) != expected_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "预检前源哈希不一致")
        try:
            with pymupdf.open(source_path) as document:
                for index in range(document.page_count):
                    yield self._extract_page(
                        document[index],
                        expected_hash,
                        index + 1,
                        include_classification=include_classification,
                    )
            if _sha256_file(source_path) != expected_hash:
                raise PortCallError(
                    ErrorCode.SOURCE_CHANGED_DURING_RUN,
                    False,
                    "流式枚举期间源哈希变化",
                )
        except PortCallError:
            raise
        except Exception as error:
            raise PortCallError(
                ErrorCode.SOURCE_NOT_READABLE,
                False,
                f"页面事实提取失败:{type(error).__name__}",
            ) from error

    def extract_all(
        self,
        source_path: Path,
        expected_hash: str,
        *,
        include_classification: bool = False,
    ) -> tuple[ExtractedPageFacts, ...]:
        """核对源哈希后一次打开整本 PDF，返回稳定且有序的全部页面事实。"""

        LOGGER.info("调用整本页面事实提取，意图=兼容旧调用并保持稳定原始页序")
        return tuple(
            self.iter_pages(
                source_path,
                expected_hash,
                include_classification=include_classification,
            )
        )

    def extract_page(
        self,
        source_path: Path,
        expected_hash: str,
        page_no: int,
        *,
        include_classification: bool = False,
    ) -> ExtractedPageFacts:
        """由当前 worker 独立打开并关闭文档，只提取指定 1-based 页面。"""

        LOGGER.info("调用单页事实提取，意图=保证 PDF worker 独立打开 page_no=%s", page_no)
        if page_no < 1 or _sha256_file(source_path) != expected_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "单页提取输入无效")
        try:
            with pymupdf.open(source_path) as document:
                if page_no > document.page_count:
                    raise PortCallError(
                        ErrorCode.INPUT_SHAPE_INVALID,
                        False,
                        "page_no 越出文档页数",
                    )
                return self._extract_page(
                    document[page_no - 1],
                    expected_hash,
                    page_no,
                    include_classification=include_classification,
                )
        except PortCallError:
            raise
        except Exception as error:
            raise PortCallError(
                ErrorCode.SOURCE_NOT_READABLE,
                False,
                f"单页事实提取失败:{type(error).__name__}",
            ) from error

    def _extract_page(
        self,
        page: pymupdf.Page,
        source_hash: str,
        page_no: int,
        *,
        include_classification: bool,
    ) -> ExtractedPageFacts:
        """从一个已打开页面提取几何、文本块和受保护视觉对象。"""

        media_box = _rect_tuple(page.mediabox)
        crop_box = _rect_tuple(page.cropbox)
        rotation = int(page.rotation)
        geometry_payload = {
            "crop_box": crop_box,
            "media_box": media_box,
            "rotation": rotation,
        }
        geometry_hash = content_sha256(geometry_payload)
        raw_dictionary = page.get_text("dict")
        text_spans: list[KernelTextFact] = []
        image_objects: list[KernelImageFact] = []
        for block_index, block in enumerate(raw_dictionary.get("blocks", [])):
            if block.get("type") == 1:
                bbox = _values_rect_tuple(block.get("bbox", (0, 0, 0, 0)))
                image_bytes = bytes(block.get("image") or b"")
                image_width = int(block.get("width") or 0)
                image_height = int(block.get("height") or 0)
                image_content_hash = hashlib.sha256(image_bytes).hexdigest()
                image_payload = {
                    "bbox": bbox,
                    "height": image_height,
                    "index": block_index,
                    "page_no": page_no,
                    "source_hash": source_hash,
                    "width": image_width,
                    "content_hash": image_content_hash,
                }
                image_objects.append(
                    KernelImageFact(
                        object_id=hashlib.sha256(
                            canonical_json_bytes(image_payload)
                        ).hexdigest(),
                        bbox=bbox,
                        width=image_width,
                        height=image_height,
                        content_hash=image_content_hash,
                    )
                )
                continue
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines", [])):
                for span_index, span in enumerate(line.get("spans", [])):
                    text = str(span.get("text") or "")
                    bbox = _values_rect_tuple(span.get("bbox", (0, 0, 0, 0)))
                    if not text.strip() or pymupdf.Rect(bbox).is_empty:
                        continue
                    font_name = str(span.get("font") or "")
                    font_size = round(float(span.get("size") or 0.0), 4)
                    color_srgb = int(span.get("color") or 0)
                    span_payload = {
                        "bbox": bbox,
                        "block_index": block_index,
                        "color_srgb": color_srgb,
                        "font_name": font_name,
                        "font_size": font_size,
                        "line_index": line_index,
                        "page_no": page_no,
                        "source_hash": source_hash,
                        "span_index": span_index,
                        "text": text,
                    }
                    text_spans.append(
                        KernelTextFact(
                            object_id=hashlib.sha256(
                                canonical_json_bytes(span_payload)
                            ).hexdigest(),
                            text=text,
                            bbox=bbox,
                            font_name=font_name,
                            font_size=font_size,
                            color_srgb=color_srgb,
                            block_index=block_index,
                            line_index=line_index,
                            span_index=span_index,
                        )
                    )
        objects: list[PageObjectFact] = []
        for block_index, block in enumerate(page.get_text("blocks")):
            legacy_bbox: RectTuple = (
                round(float(block[0]), 4),
                round(float(block[1]), 4),
                round(float(block[2]), 4),
                round(float(block[3]), 4),
            )
            text = str(block[4]).strip()
            kind = "image" if int(block[6]) == 1 else "text"
            protected = kind != "text" or not text
            identity_payload = {
                "bbox": legacy_bbox,
                "index": block_index,
                "kind": kind,
                "page_no": page_no,
                "source_hash": source_hash,
                "text": text,
            }
            objects.append(
                PageObjectFact(
                    object_id=hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest(),
                    kind=kind,
                    bbox=legacy_bbox,
                    text=text,
                    protected=protected,
                )
            )
        drawings = page.get_drawings()
        drawing_objects: list[KernelDrawingFact] = []
        for drawing_index, drawing in enumerate(drawings):
            bbox = _rect_tuple(drawing["rect"])
            drawing_content = {
                "close_path": bool(drawing.get("closePath")),
                "color": _mechanical_json_value(drawing.get("color")),
                "fill": _mechanical_json_value(drawing.get("fill")),
                "fill_opacity": round(float(drawing.get("fill_opacity") or 0.0), 4),
                "items": _mechanical_json_value(drawing.get("items", ())),
                "rect": bbox,
                "stroke_opacity": round(float(drawing.get("stroke_opacity") or 0.0), 4),
                "type": drawing.get("type"),
                "width": round(float(drawing.get("width") or 0.0), 4),
            }
            drawing_content_hash = content_sha256(drawing_content)
            detailed_identity_payload = {
                "bbox": bbox,
                "content_hash": drawing_content_hash,
                "index": drawing_index,
                "kind": "drawing",
                "page_no": page_no,
                "source_hash": source_hash,
            }
            legacy_identity_payload = {
                "bbox": bbox,
                "index": drawing_index,
                "kind": "drawing",
                "page_no": page_no,
                "source_hash": source_hash,
            }
            drawing_object_id = hashlib.sha256(
                canonical_json_bytes(detailed_identity_payload)
            ).hexdigest()
            legacy_object_id = hashlib.sha256(
                canonical_json_bytes(legacy_identity_payload)
            ).hexdigest()
            drawing_objects.append(
                KernelDrawingFact(
                    object_id=drawing_object_id,
                    bbox=bbox,
                    content_hash=drawing_content_hash,
                )
            )
            objects.append(
                PageObjectFact(
                    object_id=legacy_object_id,
                    kind="drawing",
                    bbox=bbox,
                    text="",
                    protected=True,
                )
            )
        try:
            detected_tables = tuple(page.find_tables().tables)
        except Exception:
            detected_tables = ()
        table_objects_list: list[KernelTableFact] = []
        for table_index, table in enumerate(detected_tables):
            bbox = _values_rect_tuple(table.bbox)
            cell_bboxes = tuple(
                dict.fromkeys(
                    _values_rect_tuple(cell)
                    for cell in table.cells
                    if cell is not None
                )
            )
            table_rect = pymupdf.Rect(bbox)
            text_object_ids = tuple(
                item.object_id
                for item in text_spans
                if table_rect.contains(
                    pymupdf.Point(
                        (item.bbox[0] + item.bbox[2]) / 2,
                        (item.bbox[1] + item.bbox[3]) / 2,
                    )
                )
            )
            table_objects_list.append(
                KernelTableFact(
                    object_id=content_sha256(
                        {
                            "bbox": bbox,
                            "cell_bboxes": cell_bboxes,
                            "index": table_index,
                            "kind": "table",
                            "page_no": page_no,
                            "source_hash": source_hash,
                        }
                    ),
                    bbox=bbox,
                    cell_bboxes=cell_bboxes,
                    text_object_ids=text_object_ids,
                )
            )
        table_objects = tuple(table_objects_list)
        table_bboxes = tuple(item.bbox for item in table_objects)

        annotation_objects: list[KernelAnnotationFact] = []
        for annotation_index, annotation in enumerate(page.annots() or ()):
            annotation_type = f"{int(annotation.type[0])}:{annotation.type[1]}"
            bbox = _rect_tuple(annotation.rect)
            annotation_content = {
                "bbox": bbox,
                "flags": int(annotation.flags),
                "info": _mechanical_json_value(annotation.info),
                "type": annotation_type,
            }
            annotation_content_hash = content_sha256(annotation_content)
            annotation_objects.append(
                KernelAnnotationFact(
                    object_id=content_sha256(
                        {
                            "content_hash": annotation_content_hash,
                            "index": annotation_index,
                            "kind": "annotation",
                            "page_no": page_no,
                            "source_hash": source_hash,
                        }
                    ),
                    bbox=bbox,
                    annotation_type=annotation_type,
                    content_hash=annotation_content_hash,
                )
            )

        link_objects: list[KernelLinkFact] = []
        for link_index, link in enumerate(page.get_links()):
            bbox = _rect_tuple(link["from"])
            link_content = {
                str(key): _mechanical_json_value(value)
                for key, value in sorted(link.items(), key=lambda row: str(row[0]))
                if str(key) not in {"id", "xref"}
            }
            link_content_hash = content_sha256(link_content)
            link_objects.append(
                KernelLinkFact(
                    object_id=content_sha256(
                        {
                            "content_hash": link_content_hash,
                            "index": link_index,
                            "kind": "link",
                            "page_no": page_no,
                            "source_hash": source_hash,
                        }
                    ),
                    bbox=bbox,
                    kind=int(link.get("kind") or 0),
                    content_hash=link_content_hash,
                )
            )

        font_objects: list[KernelFontFact] = []
        for font_index, raw_font in enumerate(page.get_fonts(full=True)):
            xref = int(raw_font[0])
            extension = str(raw_font[1] or "")
            font_type = str(raw_font[2] or "")
            base_font = str(raw_font[3] or "")
            resource_name = str(raw_font[4] or "")
            encoding = str(raw_font[5] or "")
            referencer = int(raw_font[6]) if len(raw_font) > 6 else 0
            embedded = xref > 0 and extension.lower() not in {"", "n/a", "unknown"}
            unicode_key = (
                page.parent.xref_get_key(xref, "ToUnicode")
                if page.parent is not None and xref > 0
                else ("null", "null")
            )
            has_to_unicode = unicode_key[0] != "null" and unicode_key[1] != "null"
            font_content = {
                "base_font": base_font,
                "embedded": embedded,
                "encoding": encoding,
                "extension": extension,
                "font_type": font_type,
                "has_to_unicode": has_to_unicode,
                "referencer": referencer,
                "resource_name": resource_name,
                "xref": xref,
            }
            font_content_hash = content_sha256(font_content)
            font_objects.append(
                KernelFontFact(
                    object_id=content_sha256(
                        {
                            "content_hash": font_content_hash,
                            "index": font_index,
                            "kind": "font",
                            "page_no": page_no,
                            "source_hash": source_hash,
                        }
                    ),
                    xref=xref,
                    extension=extension,
                    font_type=font_type,
                    base_font=base_font,
                    resource_name=resource_name,
                    encoding=encoding,
                    embedded=embedded,
                    has_to_unicode=has_to_unicode,
                    content_hash=font_content_hash,
                )
            )
        locked_objects_hash = content_sha256(
            {
                "annotations": tuple(
                    (item.bbox, item.annotation_type, item.content_hash)
                    for item in annotation_objects
                ),
                "drawings": tuple(
                    (item.bbox, item.content_hash) for item in drawing_objects
                ),
                "geometry": geometry_payload,
                "images": tuple(
                    (item.bbox, item.width, item.height, item.content_hash)
                    for item in image_objects
                ),
                "links": tuple(
                    (item.bbox, item.kind, item.content_hash) for item in link_objects
                ),
                "tables": tuple(
                    (item.bbox, item.cell_bboxes) for item in table_objects
                ),
            }
        )
        kernel_facts_hash = content_sha256(
            {
                "annotations": annotation_objects,
                "drawings": drawing_objects,
                "fonts": font_objects,
                "geometry": geometry_payload,
                "images": image_objects,
                "links": link_objects,
                "objects": objects,
                "page_no": page_no,
                "source_hash": source_hash,
                "tables": table_objects,
                "text_spans": text_spans,
            }
        )
        classification = (
            self._extract_classification_facts(page, drawings, table_bboxes)
            if include_classification
            else None
        )
        classification_summary = None
        if classification is not None:
            classification_summary = {
                "drawing_count": classification.drawing_count,
                "font_sizes": classification.font_sizes,
                "image_bboxes": classification.image_bboxes,
                "layout_spans": classification.layout_spans,
                "page_image_sha256": classification.page_image_sha256,
                "table_bboxes": classification.table_bboxes,
                "text_blocks": classification.text_blocks,
            }
        facts_hash = content_sha256(
            {
                "classification": classification_summary,
                "geometry": geometry_payload,
                "kernel_facts_hash": kernel_facts_hash,
                "page_no": page_no,
                "source_hash": source_hash,
            }
        )
        domain_facts = PageFacts(
            source_hash=source_hash,
            page_no=page_no,
            width_points=float(page.rect.width),
            height_points=float(page.rect.height),
            geometry_hash=geometry_hash,
            facts_hash=facts_hash,
        )
        return ExtractedPageFacts(
            page=domain_facts,
            page_identity=stable_page_identity(source_hash, page_no, geometry_hash),
            media_box=media_box,
            crop_box=crop_box,
            rotation=rotation,
            objects=tuple(objects),
            text_spans=tuple(text_spans),
            image_objects=tuple(image_objects),
            drawing_objects=tuple(drawing_objects),
            table_objects=table_objects,
            annotation_objects=tuple(annotation_objects),
            link_objects=tuple(link_objects),
            font_objects=tuple(font_objects),
            locked_objects_hash=locked_objects_hash,
            kernel_facts_hash=kernel_facts_hash,
            classification=classification,
        )

    def _extract_classification_facts(
        self,
        page: pymupdf.Page,
        drawings: list[dict[str, object]],
        table_bboxes: tuple[RectTuple, ...],
    ) -> PageClassificationFacts:
        """在同一次 PDF 打开中提取分类所需 span、表格、图片和页面渲染。"""

        LOGGER.info("调用分类事实提取，意图=生成匿名结构证据 page_no=%s", page.number + 1)
        raw = page.get_text(
            "dict",
            flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES,
        )
        spans: list[PageTextSpanFact] = []
        text_blocks: list[PageTextBlockFact] = []
        font_sizes: list[float] = []
        for block_index, block in enumerate(raw.get("blocks", []), 1):
            if block.get("type") != 0:
                continue
            block_id = f"B{block_index:03d}"
            line_texts: list[str] = []
            for line in block.get("lines", []):
                line_parts: list[str] = []
                for span in line.get("spans", []):
                    raw_text = str(span.get("text", ""))
                    line_parts.append(raw_text)
                    text = raw_text.strip()
                    if not text:
                        continue
                    font_sizes.append(round(float(span.get("size", 0.0)), 4))
                    spans.append(
                        PageTextSpanFact(
                            block_id=block_id,
                            bbox=_values_rect_tuple(span.get("bbox", (0, 0, 0, 0))),
                            text=text,
                        )
                    )
                joined = "".join(line_parts).strip()
                if joined:
                    line_texts.append(joined)
            block_text = "\n".join(line_texts)
            if block_text:
                text_blocks.append(
                    PageTextBlockFact(
                        block_id=block_id,
                        bbox=_values_rect_tuple(block.get("bbox", (0, 0, 0, 0))),
                        line_count=len(line_texts),
                        text=block_text,
                    )
                )
        image_bboxes = tuple(
            _values_rect_tuple(image.get("bbox", (0, 0, 0, 0)))
            for image in page.get_image_info(hashes=False, xrefs=False)
        )
        image_bytes = page.get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0), alpha=False).tobytes(
            "png"
        )
        # 中位数字体只由 evidence 层计算；这里保留排序稳定的原始字号序列。
        ordered_sizes = tuple(sorted(font_sizes))
        return PageClassificationFacts(
            page_image_png=image_bytes,
            page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            font_sizes=ordered_sizes,
            text_blocks=tuple(text_blocks),
            layout_spans=tuple(spans),
            table_bboxes=table_bboxes,
            image_bboxes=image_bboxes,
            drawing_count=len(drawings),
        )


def main() -> int:
    """记录 PageFactsExtractor 必须由完整 PDF 请求驱动。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PageFactsExtractor 示例，意图=只读枚举完整 PDF 的全部原始页面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
