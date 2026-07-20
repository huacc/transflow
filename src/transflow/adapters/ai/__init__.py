"""公开 P3 测试翻译与生产 HTTP AI Adapter。"""

from transflow.adapters.ai.fixed import DeterministicTranslationAdapter, FixedTranslationAdapter
from transflow.adapters.ai.http import HttpAiCapabilityAdapter

__all__ = [
    "DeterministicTranslationAdapter",
    "FixedTranslationAdapter",
    "HttpAiCapabilityAdapter",
]
