"""定义 Transflow 纯领域层稳定错误类型和错误码。"""

from __future__ import annotations

import logging
from enum import StrEnum

LOGGER = logging.getLogger("transflow.domain.errors")


class ErrorCode(StrEnum):
    """列出 P2 合同与状态不变量使用的稳定错误码。"""

    INVALID_CONTRACT = "INVALID_CONTRACT"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    INVALID_TRANSLATION_BUNDLE = "INVALID_TRANSLATION_BUNDLE"
    PATCH_BINDING_MISMATCH = "PATCH_BINDING_MISMATCH"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    DOCUMENT_NOT_FINALIZABLE = "DOCUMENT_NOT_FINALIZABLE"
    CHECKPOINT_VERSION_NOT_MONOTONIC = "CHECKPOINT_VERSION_NOT_MONOTONIC"
    CHECKPOINT_INCOMPATIBLE = "CHECKPOINT_INCOMPATIBLE"
    REPAIR_BUDGET_EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"
    PORT_UNAVAILABLE = "PORT_UNAVAILABLE"
    PORT_CONTRACT_VIOLATION = "PORT_CONTRACT_VIOLATION"
    INPUT_SHAPE_INVALID = "INPUT_SHAPE_INVALID"
    SOURCE_NOT_REGULAR_FILE = "SOURCE_NOT_REGULAR_FILE"
    PATH_OUTSIDE_ALLOWED_ROOT = "PATH_OUTSIDE_ALLOWED_ROOT"
    SOURCE_NOT_READABLE = "SOURCE_NOT_READABLE"
    SOURCE_UNSUPPORTED = "SOURCE_UNSUPPORTED"
    CHECKPOINT_CONFLICT = "CHECKPOINT_CONFLICT"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_INTEGRITY_FAILED = "ARTIFACT_INTEGRITY_FAILED"
    ARTIFACT_IMMUTABLE_CONFLICT = "ARTIFACT_IMMUTABLE_CONFLICT"
    AI_TIMEOUT = "AI_TIMEOUT"
    AI_RATE_LIMITED = "AI_RATE_LIMITED"
    AI_SERVER_ERROR = "AI_SERVER_ERROR"
    AI_AUTH_FAILED = "AI_AUTH_FAILED"
    AI_RESPONSE_INVALID = "AI_RESPONSE_INVALID"
    AI_REQUEST_TOO_LARGE = "AI_REQUEST_TOO_LARGE"
    SOURCE_CHANGED_DURING_RUN = "SOURCE_CHANGED_DURING_RUN"
    PATCH_OPERATION_INVALID = "PATCH_OPERATION_INVALID"
    PATCH_OWNER_VIOLATION = "PATCH_OWNER_VIOLATION"
    PATCH_PROTECTED_OBJECT = "PATCH_PROTECTED_OBJECT"
    FONT_NOT_REGISTERED = "FONT_NOT_REGISTERED"
    FONT_INTEGRITY_FAILED = "FONT_INTEGRITY_FAILED"
    PREVIEW_INVALID = "PREVIEW_INVALID"
    PRESERVATION_FAILED = "PRESERVATION_FAILED"


class DomainContractError(ValueError):
    """表示调用数据违反稳定领域合同。"""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        """保存稳定错误码和无秘密诊断文本。"""

        super().__init__(f"{code.value}:{detail}")
        self.code = code
        self.detail = detail


class PortCallError(RuntimeError):
    """表示 Port 调用以稳定错误码失败，不泄漏 Adapter 实现异常。"""

    def __init__(self, code: ErrorCode, retryable: bool, detail: str) -> None:
        """保存错误码、是否可重试和受控说明。"""

        super().__init__(f"{code.value}:{detail}")
        self.code = code
        self.retryable = retryable
        self.detail = detail


def main() -> int:
    """展示领域错误的稳定错误码接口。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    error = DomainContractError(ErrorCode.INVALID_CONTRACT, "示例合同错误")
    LOGGER.info("调用错误合同示例，意图=展示稳定错误码 code=%s", error.code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
