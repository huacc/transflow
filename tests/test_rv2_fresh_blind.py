"""验证 RV2 新严格盲集的分层、匿名和解封计分合同。"""

from __future__ import annotations

from collections import Counter

import pytest

from scripts.run_rv2_fresh_blind_revalidation import (
    build_audit_case,
    build_public_cases,
    evaluate_predictions,
    select_stratified_cases,
    summarize_rule_audit,
)


def _case(route: str, suffix: str, document: str) -> dict[str, object]:
    return {
        "case_id": f"source-{suffix}",
        "content_sha256": suffix * 64,
        "expected_route": route,
        "gold_provenance": "fixture",
        "path": f"private/{route}/{suffix}.pdf",
        "sample_id": f"sample-{suffix}",
        "size_bytes": 10,
        "source_document_id": document,
    }


def test_fresh_blind_selection_is_deterministic_stratified_and_document_distinct() -> None:
    candidates = [
        _case("route.a", "a", "doc-a1"),
        _case("route.a", "b", "doc-a2"),
        _case("route.a", "c", "doc-shared"),
        _case("route.b", "d", "doc-b1"),
        _case("route.b", "e", "doc-b2"),
        _case("route.b", "f", "doc-shared"),
    ]

    first = select_stratified_cases(candidates, "fixture-salt", 2)
    second = select_stratified_cases(list(reversed(candidates)), "fixture-salt", 2)

    assert [item["content_sha256"] for item in first] == [
        item["content_sha256"] for item in second
    ]
    assert Counter(str(item["expected_route"]) for item in first) == {
        "route.a": 2,
        "route.b": 2,
    }
    assert len({item["source_document_id"] for item in first}) == 4


def test_fresh_blind_public_manifest_removes_gold_and_source_identity() -> None:
    selected = [_case("route.a", "a", "doc-a1"), _case("route.b", "b", "doc-b1")]

    public = build_public_cases(selected)

    assert [item["case_id"] for item in public] == ["fresh-001", "fresh-002"]
    assert all(item["path"].startswith("input/pages/fresh-") for item in public)
    forbidden = {"expected_route", "gold_provenance", "sample_id", "source_document_id"}
    assert all(forbidden.isdisjoint(item) for item in public)


def test_fresh_blind_preflight_resolves_anonymous_path_from_run_root(tmp_path) -> None:
    public = {
        "case_id": "fresh-001",
        "content_sha256": "a" * 64,
        "path": "input/pages/fresh-001.pdf",
    }
    answer = {"expected_route": "route.a", "gold_provenance": "fixture"}

    audit_case = build_audit_case(tmp_path / "run", public, answer, tmp_path)

    assert audit_case["path"] == "run/input/pages/fresh-001.pdf"
    assert audit_case["expected_route"] == "route.a"


def test_fresh_blind_score_requires_complete_unique_predictions() -> None:
    public = [
        {"case_id": "fresh-001"},
        {"case_id": "fresh-002"},
    ]
    answer_key = {
        "fresh-001": "route.a",
        "fresh-002": "route.b",
    }
    predictions = [
        {"case_id": "fresh-001", "predicted_route": "route.a", "model_failure_codes": []},
        {"case_id": "fresh-002", "predicted_route": "route.a", "model_failure_codes": []},
    ]

    score = evaluate_predictions(public, answer_key, predictions)

    assert score["case_count"] == 2
    assert score["passed"] == 1
    assert score["route_accuracy"] == 0.5
    assert score["failures"] == [
        {
            "case_id": "fresh-002",
            "expected_route": "route.b",
            "predicted_route": "route.a",
        }
    ]

    with pytest.raises(ValueError, match="预测 case 不完整或重复"):
        evaluate_predictions(public, answer_key, predictions[:1])


def test_fresh_blind_rule_summary_only_counts_conflicting_direct_skip_as_unsafe() -> None:
    results = [
        {
            "decisions": {
                "correct-direct": {
                    "rule_conflict": False,
                    "high_confidence_conflict": False,
                    "model_skip_direct_evidence": True,
                },
                "wrong-direct": {
                    "rule_conflict": True,
                    "high_confidence_conflict": True,
                    "model_skip_direct_evidence": True,
                },
            }
        }
    ]

    summary = summarize_rule_audit(results)

    assert summary == {
        "node_count": 2,
        "rule_conflict_count": 1,
        "high_confidence_rule_conflict_count": 1,
        "unsafe_model_skip_count": 1,
    }
