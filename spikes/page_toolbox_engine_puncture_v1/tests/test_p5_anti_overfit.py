from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MULTI_ROOT = PROJECT_ROOT / "toolboxes" / "body" / "flow_text" / "multi"


class P5AntiOverfitTest(unittest.TestCase):
    def test_runtime_code_and_prompts_do_not_embed_regression_sample_signatures(self) -> None:
        signatures = {
            "case_id": re.compile(r"\bS2P\d{4}\b", re.IGNORECASE),
            "known_company_clouds": re.compile(r"Clouds Technology Holdings", re.IGNORECASE),
            "known_company_caihua": re.compile(r"Caihua Group", re.IGNORECASE),
            "known_company_derivative_asia": re.compile(r"Derivative Asia", re.IGNORECASE),
            "known_audit_heading": re.compile(r"Key Audit Matters", re.IGNORECASE),
            "known_audit_response_heading": re.compile(
                r"How the Matter Was Addressed in the Audit",
                re.IGNORECASE,
            ),
            "known_report_label": re.compile(r"Annual Report", re.IGNORECASE),
            "known_chinese_sentence": re.compile(r"奋楫笃行"),
        }
        violations: list[str] = []
        for root in (MULTI_ROOT / "tools", MULTI_ROOT / "prompts"):
            for path in sorted(root.rglob("*")):
                if path.suffix not in {".py", ".md"}:
                    continue
                content = path.read_text(encoding="utf-8")
                for name, pattern in signatures.items():
                    if pattern.search(content):
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{name}")

        self.assertEqual([], violations)

    def test_runtime_does_not_branch_on_page_identity_literals(self) -> None:
        violations: list[str] = []
        for path in sorted((MULTI_ROOT / "tools").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                operands = (node.left, *node.comparators)
                has_page_identity = any(
                    (isinstance(item, ast.Name) and item.id in {"case_id", "page_id"})
                    or (isinstance(item, ast.Attribute) and item.attr in {"case_id", "page_id"})
                    for item in operands
                )
                has_literal = any(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in operands
                )
                if has_page_identity and has_literal:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
