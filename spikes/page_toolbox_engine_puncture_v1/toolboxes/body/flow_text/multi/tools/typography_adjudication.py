"""千问一次只裁决字号与行距密度，不允许直接修改坐标或选择任意工具。"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Protocol

import httpx

from page_toolbox_puncture.translation import ProviderError, QwenConfig


class TypographyAdjudicator(Protocol):
    def adjudicate(
        self,
        *,
        source_png: Path,
        candidate_png: Path,
        evidence: dict[str, object],
    ) -> dict[str, object]: ...


class QwenTypographyAdjudicator:
    def __init__(self, config: QwenConfig, prompt_text: str) -> None:
        self.config = config
        self.prompt_text = prompt_text

    def adjudicate(self, *, source_png: Path, candidate_png: Path, evidence: dict[str, object]) -> dict[str, object]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.prompt_text},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(evidence, ensure_ascii=False)},
                        {"type": "text", "text": "原文页面："},
                        {"type": "image_url", "image_url": {"url": _data_url(source_png)}},
                        {"type": "text", "text": "候选页面："},
                        {"type": "image_url", "image_url": {"url": _data_url(candidate_png)}},
                    ],
                },
            ],
            "temperature": 0,
            "top_p": 1,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "typography_density_adjudication",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "verdict": {
                                "type": "string",
                                "enum": ["acceptable", "too_small", "too_tight", "too_small_and_tight"],
                            },
                            "reason": {"type": "string", "minLength": 1},
                        },
                        "required": ["verdict", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "max_tokens": 512,
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
            parsed = json.loads(content)
            verdict = str(parsed.get("verdict", ""))
            reason = str(parsed.get("reason", "")).strip()
            if verdict not in {"acceptable", "too_small", "too_tight", "too_small_and_tight"} or not reason:
                raise ProviderError("INVALID_TYPOGRAPHY_DENSITY_RESPONSE")
            return {
                "schema_version": "p5-typography-density-qwen/v1",
                "judge": "qwen",
                "model": self.config.model,
                "verdict": verdict,
                "reason": reason,
                "provider_request_id": response.headers.get("x-request-id") or body.get("id"),
                "latency_ms": latency_ms,
                "response_sha256": response_sha256,
            }
        except ProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"QWEN_TYPOGRAPHY_HTTP_{exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"QWEN_TYPOGRAPHY_{type(exc).__name__}") from exc


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
