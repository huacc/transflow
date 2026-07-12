from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CLASSIFICATION_TREE_FINGERPRINT, CLASSIFICATION_TREE_VERSION, CONTRACT_VERSION, RUNTIME_VERSION
from .contracts import (
    ArtifactRef,
    PageFacts,
    PageTemplate,
    PageTranslationRequest,
    RunManifest,
    SampleManifest,
    write_json,
)
from .sample_snapshot import sha256_file
from .state_machine import PageState, PageStateMachine
from .translation import ProviderError, TranslationProvider


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    terminal_state: str
    process_verdict: str
    product_verdict: str
    error_code: str | None


def run_translation_slice(
    *,
    project_root: Path,
    sample: SampleManifest,
    page_facts: PageFacts,
    page_template: PageTemplate,
    request: PageTranslationRequest,
    provider: TranslationProvider,
    prompt_sha256: str,
    run_id: str | None = None,
) -> RunResult:
    run_id = run_id or _run_id(provider.provider_name)
    run_dir = project_root / "artifacts" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    state = PageStateMachine()
    artifacts: list[ArtifactRef] = []

    def emit(relative: str, value: Any) -> None:
        path = run_dir / relative
        if path.exists():
            raise RuntimeError(f"artifact_already_committed:{relative}")
        write_json(path, value)
        artifacts.append(ArtifactRef(relative, sha256_file(path)))

    error_code: str | None = None
    try:
        if sample.sample_id != page_facts.page_id or page_facts.page_id != page_template.page_id or page_template.page_id != request.page_id:
            raise ValueError("page_id_mismatch")
        if sample.leaf_key != page_template.toolbox_key:
            raise ValueError("toolbox_key_mismatch")
        if sample.snapshot_sha256 != page_facts.source_pdf_sha256:
            raise ValueError("page_facts_source_hash_mismatch")
        if [item.container_id for item in page_template.containers] != [item.container_id for item in request.units]:
            raise ValueError("template_request_container_mismatch")

        emit("inputs/sample_manifest.json", sample)
        emit("inputs/page_facts.json", page_facts)
        state.transition(PageState.FACTS_READY, "页事实合同已加载", "inputs/page_facts.json")
        emit("inputs/page_template.json", page_template)
        state.transition(PageState.TEMPLATE_READY, "页面模板合同已加载", "inputs/page_template.json")
        emit("inputs/page_translation_request.json", request)

        bundle = provider.translate(request)
        bundle.validate_against(request)
        emit("outputs/page_translation_bundle.json", bundle)
        state.transition(PageState.TRANSLATION_READY, "页级译文返回并通过 ID 合同校验", "outputs/page_translation_bundle.json")
        process_verdict = "PASS"
        product_verdict = "NOT_REACHED"
    except ProviderError as exc:
        error_code = exc.code
        emit("errors/failure.json", {"error_code": error_code, "state_before_failure": state.current.value})
        state.fail_capability("P1 纵切失败", "errors/failure.json")
        process_verdict = "PASS"
        product_verdict = "NOT_REACHED"
    except Exception as exc:
        error_code = type(exc).__name__
        emit("errors/failure.json", {"error_code": error_code, "state_before_failure": state.current.value})
        state.fail_process("P1 合同或运行流程失败", "errors/failure.json")
        process_verdict = "FAIL"
        product_verdict = "NOT_REACHED"

    emit("state_trace.json", state.events)
    versions = {
        "classification_tree_version": CLASSIFICATION_TREE_VERSION,
        "classification_tree_fingerprint_sha256": CLASSIFICATION_TREE_FINGERPRINT,
        "contract_version": CONTRACT_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "prompt_sha256": prompt_sha256,
        "provider": provider.provider_name,
        "model": provider.model_name,
        "model_parameters": {"temperature": 0, "top_p": 1, "stream": False},
    }
    manifest = RunManifest(
        run_id=run_id,
        sample_id=sample.sample_id,
        terminal_state=state.current.value,
        process_verdict=process_verdict,
        product_verdict=product_verdict,
        versions=versions,
        artifacts=tuple(artifacts),
        error_code=error_code,
    )
    write_json(run_dir / "run_manifest.json", manifest)
    index = [
        ArtifactRef(path.relative_to(run_dir).as_posix(), sha256_file(path))
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "artifact_index.json"
    ]
    write_json(run_dir / "artifact_index.json", index)
    return RunResult(run_id, run_dir, state.current.value, process_verdict, product_verdict, error_code)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_id(provider_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"p1-{provider_name}-{stamp}"
