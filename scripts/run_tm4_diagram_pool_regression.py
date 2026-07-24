"""Run the frozen 30-page body.diagram pool and preserve every review artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf

_BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
for _bootstrap_path in (_BOOTSTRAP_ROOT, _BOOTSTRAP_ROOT / "src"):
    if str(_bootstrap_path) not in sys.path:
        sys.path.insert(0, str(_bootstrap_path))

from scripts.run_toolbox_leaf_migration import MigrationContractError  # noqa: E402
from scripts.toolbox_leaf_migration_visual_only import (  # noqa: E402
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
from transflow.application.translation_completeness import (  # noqa: E402
    extract_required_literals,
)
from transflow.domain.common import content_sha256, json_ready  # noqa: E402
from transflow.domain.completeness import CompletenessStatus  # noqa: E402
from transflow.domain.jobs import DocumentRunRequest  # noqa: E402
from transflow.domain.toolbox import DecisionDisposition  # noqa: E402
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
from transflow.toolboxes.leaves.body_diagram.prompt import (  # noqa: E402
    diagram_translation_system_prompt,
)
from transflow.toolboxes.leaves.body_diagram.template import (  # noqa: E402
    build_diagram_template,
)
from transflow.toolboxes.leaves.body_diagram.toolbox import DiagramToolbox  # noqa: E402
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy  # noqa: E402

REPO_ROOT = _BOOTSTRAP_ROOT
RUNS_ROOT = REPO_ROOT / "runs/toolbox_leaf_migration/TM4"
DIAGRAM_ROOT = REPO_ROOT / "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/diagram"
POOL_MANIFEST = DIAGRAM_ROOT / "samples/manifest.jsonl"
RECORDED_ROOT = DIAGRAM_ROOT / "recorded_sources/36-p14-qwen-final/cases"
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
P8_POLICY = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"
ROUTE = "body.diagram"
HAN = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD = re.compile(r"\b[A-Za-z]{2,}\b")


class _RecordedTranslationPort:
    """Replay semantic text by container identity for deterministic structure probing."""

    def __init__(
        self,
        translations: dict[str, str],
        target_language: str,
    ) -> None:
        self._translations = translations
        self._target_language = target_language
        self.call_count = 0
        self.initial_bundle: TranslationBundle | None = None
        self.last_bundle: TranslationBundle | None = None

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        translated = tuple(
            TranslatedUnit(
                unit.unit_id,
                self._translation(unit, batch),
            )
            for unit in batch.units
        )
        self.last_bundle = TranslationBundle.from_batch(batch, translated)
        if self.initial_bundle is None:
            self.initial_bundle = self.last_bundle
        return self.last_bundle

    def repair(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle,
    ) -> TranslationBundle:
        return self.translate(batch)

    def _translation(self, unit: Any, batch: TranslationBatch) -> str:
        prefix = f"body-diagram-p{unit.page_no:04d}-"
        container_id = (
            unit.region_id[len(prefix) :] if unit.region_id.startswith(prefix) else unit.region_id
        )
        fallback = (
            "示意图译文" if self._target_language.startswith("zh") else "Translated diagram label"
        )
        text = self._translations.get(container_id, fallback).strip() or fallback
        for literal in extract_required_literals(unit.source_text):
            if literal not in text:
                text = f"{text} {literal}"
        return text


class _RecordingTranslationPort:
    """Retain only the validated bundle object needed for failure materialization."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.call_count = 0
        self.initial_bundle: TranslationBundle | None = None
        self.last_bundle: TranslationBundle | None = None

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        self.call_count += 1
        self.last_bundle = self.delegate.translate(batch)
        if self.initial_bundle is None:
            self.initial_bundle = self.last_bundle
        return self.last_bundle

    def repair(
        self,
        batch: TranslationBatch,
        previous: TranslationBundle,
    ) -> TranslationBundle:
        self.call_count += 1
        self.last_bundle = self.delegate.repair(batch, previous)
        return self.last_bundle


def _load_cases() -> tuple[dict[str, Any], ...]:
    records = tuple(
        json.loads(line)
        for line in POOL_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len(records) != 30:
        raise MigrationContractError(
            "TM4_DIAGRAM_POOL_SIZE_INVALID",
            str(len(records)),
        )
    identities: set[str] = set()
    for record in records:
        sample_id = str(record["sample_id"])
        source = DIAGRAM_ROOT / str(record["source_ref"])
        upstream = REPO_ROOT / "spikes" / str(record["upstream_ref"])
        if sample_id in identities:
            raise MigrationContractError(
                "TM4_DIAGRAM_POOL_ID_DUPLICATE",
                sample_id,
            )
        identities.add(sample_id)
        if (
            not source.is_file()
            or not upstream.is_file()
            or _sha256_file(source) != str(record["sha256"])
            or _sha256_file(upstream) != str(record["sha256"])
        ):
            raise MigrationContractError(
                "TM4_DIAGRAM_POOL_SOURCE_DRIFT",
                sample_id,
            )
    return records


def _recorded_translations(sample_id: str) -> dict[str, str]:
    path = RECORDED_ROOT / sample_id / "output/translation_bundle.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item["container_id"]): str(item["translated_text"]) for item in payload["translations"]
    }


def _request(
    source: Path,
    *,
    run_id: str,
    sample_id: str,
    source_language: str,
    target_language: str,
    config_hash: str,
) -> DocumentRunRequest:
    return DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language=source_language,
        target_language=target_language,
        config_snapshot_hash=config_hash,
        job_id=f"job-{run_id}-{sample_id}",
        run_id=f"{run_id}-{sample_id}",
    )


def _protected_signature(facts: Any) -> str:
    return content_sha256(
        {
            "images": tuple(
                (item.bbox, item.width, item.height, item.content_hash)
                for item in facts.image_objects
            ),
            "drawings": tuple((item.bbox, item.content_hash) for item in facts.drawing_objects),
        }
    )


def _terminal_hard_finding_codes(result: Any) -> list[str]:
    final_finding_ids = set(result.verdict.finding_ids)
    return list(
        dict.fromkeys(
            finding.code
            for finding in result.findings
            if finding.finding_id in final_finding_ids
            and finding.severity == "HARD"
        )
    )


def _render_patch(
    source: Path,
    target: Path,
    page: Any,
    patch: Any,
    interpreter: PagePatchInterpreter,
    *,
    diagnostic: bool,
) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        application = interpreter.apply(
            document,
            page.context,
            page.facts,
            patch,
            ROUTE,
            diagnostic=diagnostic,
        )
        document.save(target, garbage=4, deflate=True)
    return {
        "diagnostic": diagnostic,
        "fits": application.fits,
        "layout_remainders": list(application.layout_remainders),
        "operation_ids": list(application.operation_ids),
    }


def _materialization(
    source: Path,
    output: Path,
    source_language: str,
    bundle: TranslationBundle | None,
    output_facts: Any,
    source_protected_signature: str,
) -> dict[str, object]:
    with pymupdf.open(output) as document:
        output_text = document[0].get_text("text")
    target_script_count = (
        len(HAN.findall(output_text))
        if source_language == "en"
        else len(LATIN_WORD.findall(output_text))
    )
    normalized_output = "".join(output_text.split()).casefold()
    translated_presence = []
    if bundle is not None:
        translated_presence = [
            {
                "unit_id": item.unit_id,
                "present": "".join(item.translated_text.split()).casefold() in normalized_output,
            }
            for item in bundle.units
        ]
    return {
        "output_sha256": _sha256_file(output),
        "output_text_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "protected_signature_preserved": (
            _protected_signature(output_facts) == source_protected_signature
        ),
        "target_script_count": target_script_count,
        "translated_presence": translated_presence,
        "translated_presence_count": sum(bool(item["present"]) for item in translated_presence),
    }


def _case_artifacts(
    case_root: Path,
    source: Path,
    output: Path,
) -> dict[str, Path]:
    source_png = case_root / "output/source.png"
    output_png = case_root / "output/transflow.png"
    comparison_pdf = case_root / "output/comparison.pdf"
    comparison_png = case_root / "output/comparison.png"
    _render_page(source, 1, source_png)
    _render_page(output, 1, output_png)
    _compose_comparison(
        (("SOURCE", source), ("TRANSFLOW", output)),
        comparison_pdf,
        comparison_png,
    )
    return {
        "source_pdf": source,
        "output_pdf": output,
        "source_png": source_png,
        "output_png": output_png,
        "comparison_pdf": comparison_pdf,
        "comparison_png": comparison_png,
    }


def _run_case(
    *,
    record: dict[str, Any],
    index: int,
    run_id: str,
    run_root: Path,
    provider: str,
    base_policy: Any,
    font_path: Path,
    interpreter: PagePatchInterpreter,
) -> dict[str, object]:
    sample_id = str(record["sample_id"])
    case_root = run_root / "cases" / f"{index:02d}-{sample_id}"
    input_pdf = case_root / "input/source.pdf"
    output_pdf = case_root / "output/transflow.pdf"
    input_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DIAGRAM_ROOT / str(record["source_ref"]), input_pdf)
    source_language = str(record["source_language"])
    target_language = str(record["target_language"])
    policy = replace(
        base_policy,
        source_language=source_language,
        target_language=target_language,
    )
    request = _request(
        input_pdf,
        run_id=run_id,
        sample_id=sample_id,
        source_language=source_language,
        target_language=target_language,
        config_hash=content_sha256(policy),
    )
    page = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]
    private_template = build_diagram_template(page.facts, input_pdf)
    toolbox = DiagramToolbox(policy, font_path, input_pdf)
    public_template = toolbox.prepare(page.context, page.facts)
    batch = toolbox.build_translation_request(public_template)
    if batch is None:
        shutil.copy2(input_pdf, output_pdf)
        artifacts = _case_artifacts(case_root, input_pdf, output_pdf)
        return {
            "sample_id": sample_id,
            "status": "PASS",
            "artifact_mode": "BYTE_PASSTHROUGH",
            "product_acceptance": True,
            "template": json_ready(private_template),
            "artifacts": {key: _relative(path, run_root) for key, path in artifacts.items()},
        }

    if provider == "recorded":
        port: Any = _RecordedTranslationPort(
            _recorded_translations(sample_id),
            target_language,
        )
    else:
        port = _RecordingTranslationPort(
            MigrationQwenTranslationAdapter(
                system_prompt=diagram_translation_system_prompt(),
            )
        )
    result = ToolboxPageCoordinator(port).execute(
        ToolboxPageWork(
            page.context,
            page.facts,
            toolbox,
            target_language=target_language,
        )
    )
    bundle = result.translation_bundle or port.initial_bundle or port.last_bundle
    patch = result.patch
    artifact_mode = "PRODUCT_ACCEPTED"
    product_acceptance = (
        patch is not None
        and result.verdict.disposition is DecisionDisposition.ACCEPT
        and result.completeness_decision is not None
        and result.completeness_decision.status is CompletenessStatus.PASS
    )
    if not product_acceptance:
        patch = result.proposed_patch
        artifact_mode = "REJECTED_PRODUCT_CANDIDATE"
    diagnostic_records: tuple[dict[str, object], ...] = ()
    if patch is None and bundle is not None:
        patch, diagnostic_records = toolbox.build_diagnostic_patch(
            public_template,
            batch,
            bundle,
        )
        artifact_mode = "TRANSLATED_DIAGNOSTIC"

    application: dict[str, object]
    if patch is not None:
        try:
            application = _render_patch(
                input_pdf,
                output_pdf,
                page,
                patch,
                interpreter,
                diagnostic=not product_acceptance,
            )
        except Exception as error:
            shutil.copy2(input_pdf, output_pdf)
            application = {
                "diagnostic": True,
                "fits": False,
                "failure": f"{type(error).__name__}:{error}",
            }
            artifact_mode = "SOURCE_FALLBACK_AFTER_RENDER_FAILURE"
            product_acceptance = False
    else:
        shutil.copy2(input_pdf, output_pdf)
        application = {
            "diagnostic": True,
            "fits": True,
            "operation_ids": [],
        }
        artifact_mode = "SOURCE_FALLBACK"
        product_acceptance = False

    output_hash = _sha256_file(output_pdf)
    output_facts = PageFactsExtractor().extract_page(
        output_pdf,
        output_hash,
        1,
    )
    materialization = _materialization(
        input_pdf,
        output_pdf,
        source_language,
        bundle,
        output_facts,
        _protected_signature(page.facts),
    )
    gate_rejection_codes = _terminal_hard_finding_codes(result)
    if not bool(application.get("fits", False)):
        gate_rejection_codes.append("TM4_DIAGRAM_PATCH_NOT_FIT")
    if not bool(materialization["protected_signature_preserved"]):
        gate_rejection_codes.append("TM4_DIAGRAM_PROTECTED_GEOMETRY_CHANGED")
    if int(materialization["target_script_count"]) < 1:
        gate_rejection_codes.append("TM4_DIAGRAM_TRANSLATION_NOT_MATERIALIZED")
    product_acceptance = product_acceptance and not gate_rejection_codes
    artifacts = _case_artifacts(case_root, input_pdf, output_pdf)
    rule_trace = toolbox.rule_trace(f"plan-{public_template.template_id}")
    process = {
        "schema_version": "transflow.tm4-diagram-pool-case/v1",
        "sample_id": sample_id,
        "direction": f"{source_language}->{target_language}",
        "status": "PASS" if product_acceptance else "FAIL",
        "product_acceptance": product_acceptance,
        "artifact_mode": artifact_mode,
        "provider_mode": provider,
        "provider_call_count": port.call_count,
        "source_hash": _sha256_file(input_pdf),
        "template": {
            "container_count": len(private_template.containers),
            "node_count": len(private_template.nodes),
            "connector_count": len(private_template.connectors),
            "protected_object_count": len(private_template.protected_object_ids),
            "topology_sha256": private_template.topology_sha256,
            "structure_sha256": private_template.structure_sha256,
        },
        "translation": {
            "batch_hash": content_sha256(batch),
            "bundle_hash": content_sha256(bundle) if bundle is not None else None,
            "unit_count": len(batch.units),
        },
        "completeness": {
            "status": (
                result.completeness_decision.status.value
                if result.completeness_decision is not None
                else None
            ),
        },
        "outcome": {
            "decision": result.verdict.disposition.value,
            "fallback": result.outcome.fallback.value,
            "finding_codes": list(result.outcome.finding_codes),
            "quality": result.outcome.quality.value,
            "trace": list(result.trace.stages),
        },
        "historical_finding_codes": list(result.outcome.finding_codes),
        "gate_rejection_codes": list(dict.fromkeys(gate_rejection_codes)),
        "application": application,
        "materialization": materialization,
        "diagnostic_records": list(diagnostic_records),
        "layout_rule_trace": list(rule_trace),
        "artifacts": {key: _relative(path, run_root) for key, path in artifacts.items()},
    }
    _write_json(
        case_root / "process/template.json",
        private_template,
        run_root,
    )
    _write_json(
        case_root / "process/translation_batch.json",
        batch,
        run_root,
    )
    if bundle is not None:
        _write_json(
            case_root / "process/translation_bundle.json",
            bundle,
            run_root,
        )
    _write_json(
        case_root / "process/case_manifest.json",
        process,
        run_root,
    )
    return process


def run(
    run_id: str,
    *,
    provider: str,
    only: tuple[str, ...] = (),
) -> Path:
    if provider == "qwen" and not migration_translation_environment_ready():
        raise MigrationContractError(
            "REAL_QWEN_ENVIRONMENT_NOT_CONFIGURED",
            "TM4 diagram pool environment is incomplete",
        )
    run_root = RUNS_ROOT / run_id
    if run_root.exists():
        raise MigrationContractError(
            "TM4_RUN_ID_ALREADY_EXISTS",
            run_id,
        )
    run_root.mkdir(parents=True)
    records = tuple(
        record for record in _load_cases() if not only or str(record["sample_id"]) in only
    )
    if only and len(records) != len(set(only)):
        raise MigrationContractError(
            "TM4_CASE_FILTER_INVALID",
            ",".join(only),
        )
    base_policy = load_p8_toolbox_policy(P8_POLICY)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    font_path = fonts.resolve(FONT_ID).path
    interpreter = PagePatchInterpreter(fonts)
    _write_json(
        run_root / "input/request.json",
        {
            "schema_version": "transflow.tm4-diagram-pool-request/v1",
            "run_id": run_id,
            "provider_mode": provider,
            "case_count": len(records),
            "sample_ids": [str(item["sample_id"]) for item in records],
            "credential_persistence": False,
        },
        run_root,
    )
    results: list[dict[str, object]] = []
    for index, record in enumerate(records, start=1):
        try:
            process = _run_case(
                record=record,
                index=index,
                run_id=run_id,
                run_root=run_root,
                provider=provider,
                base_policy=base_policy,
                font_path=font_path,
                interpreter=interpreter,
            )
        except Exception as error:
            sample_id = str(record["sample_id"])
            case_root = run_root / "cases" / f"{index:02d}-{sample_id}"
            source = case_root / "input/source.pdf"
            output = case_root / "output/transflow.pdf"
            if source.is_file() and not output.is_file():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)
                _case_artifacts(case_root, source, output)
            process = {
                "schema_version": "transflow.tm4-diagram-pool-case/v1",
                "sample_id": sample_id,
                "status": "FAIL",
                "product_acceptance": False,
                "artifact_mode": "SOURCE_FALLBACK_AFTER_CASE_EXCEPTION",
                "gate_rejection_codes": [f"{type(error).__name__}:{error}"],
            }
            _write_json(
                case_root / "process/case_manifest.json",
                process,
                run_root,
            )
        results.append(process)
        print(
            "TM4_DIAGRAM_CASE"
            f" sample={process['sample_id']}"
            f" status={process['status']}"
            f" artifact={process['artifact_mode']}"
        )
    passed = sum(item["status"] == "PASS" for item in results)
    summary = {
        "schema_version": "transflow.tm4-diagram-pool-summary/v1",
        "run_id": run_id,
        "provider_mode": provider,
        "case_count": len(results),
        "pass_count": passed,
        "fail_count": len(results) - passed,
        "all_cases_have_pdf": all(
            (
                run_root / "cases" / f"{index:02d}-{record['sample_id']}" / "output/transflow.pdf"
            ).is_file()
            for index, record in enumerate(records, start=1)
        ),
        "default_catalog_mutated": False,
        "promotion_eligibility": "PASS_DISABLED_WITH_FALLBACK",
        "results": [
            {
                "sample_id": item["sample_id"],
                "status": item["status"],
                "artifact_mode": item["artifact_mode"],
                "gate_rejection_codes": item.get(
                    "gate_rejection_codes",
                    [],
                ),
            }
            for item in results
        ],
    }
    _write_json(run_root / "output/summary.json", summary, run_root)
    _write_json(
        run_root / "run_manifest.json",
        {
            "schema_version": "transflow.tm4-diagram-pool-run/v1",
            "stage": "TM4",
            "route": ROUTE,
            "run_id": run_id,
            "status": "PASS" if passed == len(results) else "FAIL",
            "last_successful_state": "POOL_ARTIFACTS_MATERIALIZED",
            "next_state": "OWNER_VISUAL_REVIEW",
            "input_ref": "input/request.json",
            "output_ref": "output/summary.json",
        },
        run_root,
    )
    return run_root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id",
        default=(
            "03-body-diagram-recorded-structure-regression-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("recorded", "qwen"),
        default="recorded",
    )
    parser.add_argument("--only", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = run(
        args.run_id,
        provider=args.provider,
        only=tuple(args.only),
    )
    print(f"TM4_DIAGRAM_POOL_RUN={_relative(path, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
