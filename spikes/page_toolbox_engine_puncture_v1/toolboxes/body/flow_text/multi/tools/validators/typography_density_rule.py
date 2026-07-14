from __future__ import annotations


def evaluate_typography_density_failure(decision: dict[str, object]) -> dict[str, object]:
    verdict = str(decision.get("verdict", ""))
    if verdict == "acceptable":
        return {
            "schema_version": "p5-typography-density-rule/v1",
            "rule_verdict": "PASS",
            "observed_verdict": verdict,
            "selected_failure_class": None,
            "repair_atom": None,
        }
    if verdict in {"too_tight", "too_small_and_tight"}:
        failure_class = "body_line_height_too_tight"
        repair_atom = "line_height_recovery"
    elif verdict == "too_small":
        failure_class = "body_font_scale_too_small"
        repair_atom = "font_scale_recovery"
    else:
        raise ValueError(f"unsupported_typography_density_verdict:{verdict}")
    return {
        "schema_version": "p5-typography-density-rule/v1",
        "rule_verdict": "FAIL",
        "observed_verdict": verdict,
        "selected_failure_class": failure_class,
        "repair_atom": repair_atom,
        "reason": str(decision.get("reason", "")),
    }
