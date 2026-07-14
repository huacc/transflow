"""
tool_name: margin_text_translation_rule
category: validators
input_contract: one source TextObjectFact already located in a locked page-margin container
output_contract: TRANSLATE for natural-language text, PRESERVE for non-language running marks
failure_signals: none; undecidable symbol-only objects remain preserved
fallback: preserve the source object and do not redact it
anti_overfit_statement: the rule uses Unicode character categories only; no company name, year, page number, literal text, or coordinate is encoded
"""

from __future__ import annotations

from page_toolbox_puncture.contracts import TextObjectFact


def classify_margin_text_object(source_object: TextObjectFact) -> dict[str, object]:
    has_natural_language = any(character.isalpha() for character in source_object.text)
    return {
        "rule_verdict": "TRANSLATE" if has_natural_language else "PRESERVE",
        "selected_failure_class": "translatable_margin_text_omitted" if has_natural_language else None,
        "repair_atom": "margin_text_template_inclusion" if has_natural_language else None,
        "source_object_id": source_object.object_id,
        "evidence": {
            "contains_natural_language_character": has_natural_language,
            "source_geometry_unchanged": True,
        },
    }
