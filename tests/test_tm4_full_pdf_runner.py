from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts.toolbox_leaf_migration_diagram_run import (
    ACCEPTED_TEXT_ROUTES,
    _RecordingTranslationPort,
    build_accepted_leaf_catalog_overlay,
    leaf_policy_for_languages,
    translation_prompt_for_route,
)
from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
)
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "resources/catalogs/page_toolbox_catalog_v4.json"


def test_tm4_full_pdf_overlay_enables_only_accepted_leaf_routes() -> None:
    before = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    overlay = build_accepted_leaf_catalog_overlay(before)

    assert json.loads(CATALOG_PATH.read_text(encoding="utf-8")) == before
    before_by_route = {item["route"]: item for item in before["entries"]}
    after_by_route = {item["route"]: item for item in overlay["entries"]}
    changed = {
        route
        for route in before_by_route
        if before_by_route[route] != after_by_route[route]
    }
    assert changed == {"body.chart", "body.diagram"}
    assert all(after_by_route[route]["enabled"] for route in ACCEPTED_TEXT_ROUTES)


def test_tm4_full_pdf_routes_use_leaf_owned_prompts() -> None:
    assert {
        route for route in ACCEPTED_TEXT_ROUTES if translation_prompt_for_route(route)
    } == ACCEPTED_TEXT_ROUTES
    assert translation_prompt_for_route("body.table") is None


def test_tm4_full_pdf_leaf_policy_uses_document_language_direction() -> None:
    base = load_p8_toolbox_policy(
        REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
    )

    policy = leaf_policy_for_languages(base, "zh-CN", "en")

    assert policy.source_language == "zh-CN"
    assert policy.target_language == "en"
    assert policy.font_id == base.font_id


def test_qwen_connection_failure_is_retryable_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_BASE_URL", "http://qwen.invalid/v1")
    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_API_KEY", "test-key")
    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_MODEL", "test-model")

    def fail_connect(
        _client: httpx.Client,
        url: str,
        **_kwargs: object,
    ) -> httpx.Response:
        raise httpx.ConnectError(
            "connection unavailable",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fail_connect)
    batch = TranslationBatch(
        "connection-failure",
        "en",
        "zh-CN",
        (TranslationUnit("u1", 1, 0, "Hello", "r1"),),
    )
    adapter = MigrationQwenTranslationAdapter(chunk_size=1)

    with pytest.raises(PortCallError) as captured:
        adapter.translate(batch)

    assert captured.value.code is ErrorCode.AI_SERVER_ERROR
    assert captured.value.retryable is True


def test_recording_port_retries_only_transient_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch = TranslationBatch(
        "transient-retry",
        "en",
        "zh-CN",
        (TranslationUnit("u1", 1, 0, "Hello", "r1"),),
    )
    bundle = TranslationBundle.from_batch(
        batch,
        (TranslatedUnit("u1", "你好"),),
    )

    class TransientDelegate:
        def __init__(self) -> None:
            self.calls = 0

        def translate(self, _batch: TranslationBatch) -> TranslationBundle:
            self.calls += 1
            if self.calls < 3:
                raise PortCallError(
                    ErrorCode.AI_SERVER_ERROR,
                    True,
                    "ConnectError",
                )
            return bundle

    delegate = TransientDelegate()
    monkeypatch.setattr(
        "scripts.toolbox_leaf_migration_diagram_run.store_translation_bundle",
        lambda *_args, **_kwargs: SimpleNamespace(
            bundle_hash="bundle-hash",
            path=tmp_path / "bundle.json",
        ),
    )
    port = _RecordingTranslationPort(
        delegate,  # type: ignore[arg-type]
        tmp_path,
        retry_delays_seconds=(0.0, 0.0),
    )

    assert port.translate(batch) == bundle
    assert delegate.calls == 3


def test_tm4_full_pdf_runner_has_no_spike_runtime_dependency() -> None:
    source = (
        REPO_ROOT / "scripts/toolbox_leaf_migration_diagram_run.py"
    ).read_text(encoding="utf-8")

    assert "from spikes" not in source
    assert "import spikes" not in source
    assert "page_toolbox_engine_puncture_v1" not in source
