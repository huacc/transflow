from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import httpx

from .config import GENERATION_PARAMS, ProviderConfig
from .models import ProviderResult


def business_payload(model: str, messages: list[dict[str, Any]], extra: dict[str, Any]) -> dict[str, Any]:
    return {"model": model, "messages": messages, **GENERATION_PARAMS, **extra}


def parse_json_content(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def call_chat(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    response_format: dict[str, Any],
    timeout_seconds: float = 180,
) -> ProviderResult:
    payload = business_payload(config.model, messages, {"response_format": response_format})
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                f"{config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key()}", "Content-Type": "application/json"},
                json=payload,
            )
        latency = round((time.perf_counter() - started) * 1000)
        raw_hash = hashlib.sha256(response.content).hexdigest()
        response.raise_for_status()
        body = response.json()
        choice = body.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        parsed = parse_json_content(content)
        return ProviderResult(
            response.status_code,
            latency,
            response.headers.get("x-request-id") or body.get("id"),
            body.get("model"),
            choice.get("finish_reason"),
            content if isinstance(content, str) else None,
            parsed,
            raw_hash,
            None if parsed is not None else "INVALID_JSON",
        )
    except httpx.TimeoutException:
        return ProviderResult(None, round((time.perf_counter() - started) * 1000), None, None, None, None, None, None, "TIMEOUT")
    except httpx.HTTPStatusError as exc:
        code = "RATE_LIMIT" if exc.response.status_code == 429 else f"HTTP_{exc.response.status_code}"
        return ProviderResult(exc.response.status_code, round((time.perf_counter() - started) * 1000), exc.response.headers.get("x-request-id"), None, None, None, None, hashlib.sha256(exc.response.content).hexdigest(), code)
    except (httpx.HTTPError, ValueError) as exc:
        return ProviderResult(None, round((time.perf_counter() - started) * 1000), None, None, None, None, None, None, f"CLIENT_ERROR:{type(exc).__name__}")
