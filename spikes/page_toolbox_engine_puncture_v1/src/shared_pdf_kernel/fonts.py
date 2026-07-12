from __future__ import annotations

from pathlib import Path

import fitz

from .models import FontProbe


def probe_font(font_file: Path, text: str) -> FontProbe:
    if not font_file.exists():
        return FontProbe(str(font_file), False, False, None, tuple(_codepoint(char) for char in _unique_nonspace(text)))
    try:
        font = fitz.Font(fontfile=str(font_file))
        missing = tuple(_codepoint(char) for char in _unique_nonspace(text) if not font.has_glyph(ord(char)))
        return FontProbe(str(font_file), True, True, int(font.glyph_count), missing)
    except Exception:
        return FontProbe(str(font_file), True, False, None, tuple(_codepoint(char) for char in _unique_nonspace(text)))


def embedded_font_resources(pdf_path: Path, page_index: int = 0) -> tuple[str, ...]:
    with fitz.open(pdf_path) as document:
        resources = []
        for row in document.get_page_fonts(page_index, full=True):
            xref, extension, _font_type, _basefont, resource_name = row[:5]
            if int(xref) > 0 and str(extension).strip():
                resources.append(str(resource_name))
        return tuple(sorted(set(resources)))


def missing_embedded_resources(pdf_path: Path, expected_resources: set[str], page_index: int = 0) -> tuple[str, ...]:
    actual = set(embedded_font_resources(pdf_path, page_index))
    return tuple(sorted(expected_resources - actual))


def _unique_nonspace(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(char for char in text if not char.isspace()))


def _codepoint(char: str) -> str:
    return f"U+{ord(char):04X}"
