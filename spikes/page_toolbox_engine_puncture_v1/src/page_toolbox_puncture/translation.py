from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .contracts import PageTranslationBundle, PageTranslationRequest, TranslationResult, TranslationUnit


class ProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TranslationProvider(Protocol):
    provider_name: str
    model_name: str

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle: ...


class FixedTranslationProvider:
    provider_name = "fixed"
    model_name = "p1-fixed-fixture"

    def __init__(self, translations: dict[str, str]) -> None:
        self._translations = dict(translations)

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle:
        results = tuple(
            TranslationResult(unit.container_id, self._translations[unit.container_id])
            for unit in request.units
        )
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=results,
        )
        bundle.validate_against(request)
        return bundle


@dataclass(frozen=True)
class QwenConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 180.0

    @classmethod
    def from_environment(cls) -> "QwenConfig":
        api_key = os.environ.get("PAGE_TOOLBOX_QWEN_API_KEY", "").strip()
        if not api_key:
            raise ProviderError("MISSING_PAGE_TOOLBOX_QWEN_API_KEY")
        return cls(
            base_url=os.environ.get("PAGE_TOOLBOX_QWEN_BASE_URL", "http://112.30.139.26:19400/v1").rstrip("/"),
            model=os.environ.get("PAGE_TOOLBOX_QWEN_MODEL", "Qwen/Qwen3.6-35B-A3B"),
            api_key=api_key,
            timeout_seconds=float(os.environ.get("PAGE_TOOLBOX_QWEN_TIMEOUT_SECONDS", "180")),
        )


class QwenPageTranslationProvider:
    provider_name = "qwen"

    def __init__(self, config: QwenConfig, prompt_text: str) -> None:
        self.config = config
        self.model_name = config.model
        self.prompt_text = prompt_text

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle:
        translations: list[TranslationResult] = []
        request_ids: list[str] = []
        response_hashes: list[str] = []
        total_latency_ms = 0
        pending = list(_translation_chunks(request.units))
        while pending:
            units = pending.pop(0)
            try:
                rows, provider_request_id, latency_ms, response_sha256 = self._translate_chunk(request, units)
            except ProviderError as exc:
                recoverable_chunk_error = exc.code in {
                    "DUPLICATE_TRANSLATION_CONTAINER_ID",
                    "TRANSLATION_CONTAINER_ID_SET_MISMATCH",
                    "INVALID_TRANSLATION_RESPONSE",
                    "NON_TEXT_TRANSLATION_RESPONSE",
                    "NON_OBJECT_TRANSLATION_RESPONSE",
                    "QWEN_CLIENT_JSONDecodeError",
                }
                if recoverable_chunk_error and len(units) > 1:
                    midpoint = len(units) // 2
                    pending[0:0] = [units[:midpoint], units[midpoint:]]
                    continue
                raise
            translations.extend(rows)
            total_latency_ms += latency_ms
            response_hashes.append(response_sha256)
            if provider_request_id:
                request_ids.append(provider_request_id)

        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(translations),
            provider_request_id=",".join(request_ids) or None,
            latency_ms=total_latency_ms,
            response_sha256=hashlib.sha256("".join(response_hashes).encode("ascii")).hexdigest(),
        )
        bundle.validate_against(request)
        return bundle

    def _translate_chunk(
        self,
        request: PageTranslationRequest,
        units: tuple[TranslationUnit, ...],
    ) -> tuple[tuple[TranslationResult, ...], str | None, int, str]:
        expected_ids = [unit.container_id for unit in units]
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.prompt_text},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request_id": request.request_id,
                            "page_id": request.page_id,
                            "source_language": request.source_language,
                            "target_language": request.target_language,
                            "units": [
                                {
                                    "container_id": unit.container_id,
                                    "source_text": unit.source_text,
                                    "required_literals": list(unit.required_literals),
                                }
                                for unit in units
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "top_p": 1,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "page_translation",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "translations": {
                                "type": "array",
                                "minItems": len(expected_ids),
                                "maxItems": len(expected_ids),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "container_id": {"type": "string", "enum": expected_ids},
                                        "translated_text": {"type": "string", "minLength": 1},
                                    },
                                    "required": ["container_id", "translated_text"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["translations"],
                        "additionalProperties": False,
                    },
                },
            },
            "max_tokens": 16384,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            latency_ms = round((time.perf_counter() - started) * 1000)
            response_sha256 = hashlib.sha256(response.content).hexdigest()
            response.raise_for_status()
            body = response.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = _parse_json_object(content)
            raw_rows = parsed.get("translations")
            if not isinstance(raw_rows, list):
                raise ProviderError("INVALID_TRANSLATION_RESPONSE")
            rows = _normalize_translation_order(raw_rows, expected_ids)
            provider_request_id = response.headers.get("x-request-id") or body.get("id")
            return rows, str(provider_request_id) if provider_request_id else None, latency_ms, response_sha256
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("QWEN_TIMEOUT") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"QWEN_HTTP_{exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"QWEN_CLIENT_{type(exc).__name__}") from exc


def _translation_chunks(units: tuple[TranslationUnit, ...], *, max_units: int = 12, max_chars: int = 6000) -> tuple[tuple[TranslationUnit, ...], ...]:
    chunks: list[tuple[TranslationUnit, ...]] = []
    current: list[TranslationUnit] = []
    current_chars = 0
    for unit in units:
        unit_chars = len(str(getattr(unit, "source_text", "")))
        if current and (len(current) >= max_units or current_chars + unit_chars > max_chars):
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_chars
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _parse_json_object(content: object) -> dict[str, object]:
    if not isinstance(content, str):
        raise ProviderError("NON_TEXT_TRANSLATION_RESPONSE")
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ProviderError("NON_OBJECT_TRANSLATION_RESPONSE")
    return value


def _normalize_translation_order(rows: list[object], expected_ids: list[str]) -> tuple[TranslationResult, ...]:
    parsed = [
        TranslationResult(str(row.get("container_id", "")), str(row.get("translated_text", "")))
        for row in rows
        if isinstance(row, dict)
    ]
    actual_ids = [item.container_id for item in parsed]
    if len(actual_ids) != len(set(actual_ids)):
        raise ProviderError("DUPLICATE_TRANSLATION_CONTAINER_ID")
    if set(actual_ids) != set(expected_ids):
        raise ProviderError("TRANSLATION_CONTAINER_ID_SET_MISMATCH")
    by_id = {item.container_id: item for item in parsed}
    return tuple(by_id[container_id] for container_id in expected_ids)
