"""实现仅供 P9 真实样本迁移测试使用的千问翻译适配器。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

from transflow.application.translation_completeness import extract_required_literals
from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)

LOGGER = logging.getLogger("transflow.tests.migration.p9_qwen_translation_adapter")
BASE_URL_ENV = "TRANSFLOW_MIGRATION_QWEN_BASE_URL"
API_KEY_ENV = "TRANSFLOW_MIGRATION_QWEN_API_KEY"
MODEL_ENV = "TRANSFLOW_MIGRATION_QWEN_MODEL"
DEFAULT_SYSTEM_PROMPT = (
    "你是财务年报专业翻译。把每个 source_text 从英文翻译为简体中文；"
    "数字、代码、邮箱、网址和已经是中文的内容保持原意，不总结、不合并、不拆分。"
    "只要 source_text 含普通英文词或句子，"
    "translated_text 就必须包含对应的简体中文，"
    "不得仅原样返回英文；纯代码、网址、邮箱或数字单元除外。"
    "每个 unit 的 required_literals 必须在 translated_text 中逐字保留，"
    "不得翻译、改写、增删或改变大小写。"
    "必须只返回满足响应 Schema 的 JSON。"
)


def _required_environment(name: str) -> str:
    """读取必填环境变量，异常和日志只暴露变量名。"""

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing_environment_variable:{name}")
    return value


def migration_translation_environment_ready() -> bool:
    """仅判断迁移测试环境是否齐全，不读取或输出秘密内容。"""

    return all(os.environ.get(name, "").strip() for name in (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV))


def _parse_json_content(content: object) -> dict[str, Any] | None:
    """解析普通文本或 Markdown JSON 围栏中的单一对象。"""

    if not isinstance(content, str):
        return None
    candidate = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _response_schema(unit_ids: tuple[str, ...]) -> dict[str, Any]:
    """为当前分片生成 unit_id 封闭且无额外字段的响应 Schema。"""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "p9_translation_batch",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "translations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "unit_id": {"type": "string", "enum": list(unit_ids)},
                                "translated_text": {"type": "string"},
                            },
                            "required": ["unit_id", "translated_text"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["translations"],
                "additionalProperties": False,
            },
        },
    }


class MigrationQwenTranslationAdapter:
    """通过环境变量调用真实千问，并严格实现 TranslationPort 合同。"""

    def __init__(
        self,
        timeout_seconds: float = 180.0,
        chunk_size: int = 48,
        *,
        system_prompt: str | None = None,
    ) -> None:
        """读取非持久化连接参数，并限制单次请求的翻译单元数量。"""

        if timeout_seconds <= 0 or chunk_size < 1:
            raise ValueError("迁移翻译超时和分片大小必须为正数")
        self._base_url = _required_environment(BASE_URL_ENV).rstrip("/")
        self._api_key = _required_environment(API_KEY_ENV)
        self._model = _required_environment(MODEL_ENV)
        self._timeout_seconds = timeout_seconds
        self._chunk_size = chunk_size
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        if not self._system_prompt.strip():
            raise ValueError("迁移翻译 system_prompt 不得为空")
        self._lock = threading.Lock()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        """返回真实 HTTP 调用次数，不包含请求、响应或秘密正文。"""

        with self._lock:
            return self._call_count

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """按稳定分片调用真实模型，并恢复为原始 unit 顺序。"""

        LOGGER.info(
            "调用 P9 真实迁移翻译，意图=翻译分类样本 batch_id=%s unit_count=%s",
            batch.batch_id,
            len(batch.units),
        )
        translated: dict[str, str] = {}
        for start in range(0, len(batch.units), self._chunk_size):
            chunk = batch.units[start : start + self._chunk_size]
            translated.update(self._translate_chunk_resilient(batch, chunk))
        units = tuple(
            TranslatedUnit(unit.unit_id, translated[unit.unit_id]) for unit in batch.units
        )
        return TranslationBundle.from_batch(batch, units)

    def _translate_chunk_resilient(
        self,
        batch: TranslationBatch,
        units: tuple[TranslationUnit, ...],
    ) -> dict[str, str]:
        """结构响应无效时二分当前分片；单 unit 仍无效则诚实失败。"""

        try:
            return self._translate_chunk(batch, units)
        except PortCallError as error:
            if error.code is not ErrorCode.AI_RESPONSE_INVALID or len(units) == 1:
                raise
            middle = len(units) // 2
            LOGGER.warning(
                "千问分片结构无效，意图=只缩小当前分片 unit_count=%s",
                len(units),
            )
            return {
                **self._translate_chunk_resilient(batch, units[:middle]),
                **self._translate_chunk_resilient(batch, units[middle:]),
            }

    def _translate_chunk(
        self,
        batch: TranslationBatch,
        units: tuple[TranslationUnit, ...],
    ) -> dict[str, str]:
        """执行一次真实 HTTP 请求并严格校验本分片身份集合。"""

        unit_ids = tuple(unit.unit_id for unit in units)
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_language": batch.source_language,
                            "target_language": batch.target_language,
                            "units": [
                                {
                                    "required_literals": list(
                                        extract_required_literals(unit.source_text)
                                    ),
                                    "source_text": unit.source_text,
                                    "unit_id": unit.unit_id,
                                }
                                for unit in units
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            "response_format": _response_schema(unit_ids),
            "stream": False,
            "temperature": 0,
            "top_p": 1,
        }
        LOGGER.info(
            "调用真实千问 HTTP，意图=取得 P9 迁移译文 unit_count=%s",
            len(units),
        )
        started = time.perf_counter()
        try:
            with self._lock:
                self._call_count += 1
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            parsed = _parse_json_content(body["choices"][0]["message"]["content"])
        except httpx.TimeoutException as error:
            raise PortCallError(ErrorCode.AI_TIMEOUT, True, "P9 真实迁移翻译超时") from error
        except httpx.HTTPStatusError as error:
            code = (
                ErrorCode.AI_RATE_LIMITED
                if error.response.status_code == 429
                else ErrorCode.AI_SERVER_ERROR
            )
            raise PortCallError(
                code, error.response.status_code >= 500, "P9 真实迁移翻译 HTTP 失败"
            ) from error
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise PortCallError(
                ErrorCode.AI_RESPONSE_INVALID, False, type(error).__name__
            ) from error
        LOGGER.info(
            "真实千问返回，意图=只记录迁移调用延迟 latency_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        required_by_id = {
            unit.unit_id: extract_required_literals(unit.source_text) for unit in units
        }
        return self._normalize(parsed, unit_ids, required_by_id)

    @staticmethod
    def _normalize(
        value: dict[str, Any] | None,
        unit_ids: tuple[str, ...],
        required_by_id: dict[str, tuple[str, ...]],
    ) -> dict[str, str]:
        """拒绝身份漂移，并机械恢复模型遗漏的必保留字面量。"""

        if not isinstance(value, dict) or set(value) != {"translations"}:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "P9 翻译响应字段非法")
        raw = value["translations"]
        if not isinstance(raw, list):
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "P9 翻译响应列表非法")
        translated: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"unit_id", "translated_text"}:
                raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "P9 翻译单元字段非法")
            unit_id = item["unit_id"]
            text = item["translated_text"]
            if (
                not isinstance(unit_id, str)
                or unit_id in translated
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "P9 翻译单元内容非法")
            normalized = text.strip()
            missing_literals = tuple(
                literal
                for literal in required_by_id.get(unit_id, ())
                if literal not in normalized
            )
            if missing_literals:
                LOGGER.info(
                    "调用必保留字面量恢复，意图=让真实模型结果满足门禁 unit_id=%s literal_count=%s",
                    unit_id,
                    len(missing_literals),
                )
                normalized = " ".join((normalized, *missing_literals))
            translated[unit_id] = normalized
        if set(translated) != set(unit_ids):
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "P9 翻译 unit_id 集合漂移")
        return translated


def main() -> int:
    """只报告迁移环境是否齐全，不输出端点、模型或密钥。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"P9_MIGRATION_QWEN_ENV ready={migration_translation_environment_ready()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
