"""按 P1 冻结 manifest 解析并校验受控字体资产。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from transflow.domain.errors import ErrorCode, PortCallError
from transflow.pdf_kernel.models import FontProbe

LOGGER = logging.getLogger("transflow.pdf_kernel.fonts")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _sha256_file(path: Path) -> str:
    """流式计算字体资产 SHA-256，用于每次解析前的完整性校验。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ControlledFontAsset:
    """表示 manifest 中已登记并通过内容哈希校验的一项字体。"""

    font_id: str
    path: Path
    sha256: str


class ControlledFontRegistry:
    """只允许显式 manifest 字体，不搜索或回退到宿主机系统字体。"""

    def __init__(self, manifest_path: Path, repository_root: Path) -> None:
        """读取 manifest，并把全部字体路径约束在注入的仓库根下。"""

        self._manifest_path = manifest_path.resolve()
        self._repository_root = repository_root.resolve()
        payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "transflow.font-manifest/v1":
            raise PortCallError(ErrorCode.FONT_INTEGRITY_FAILED, False, "字体 manifest 版本无效")
        self._assets: dict[str, ControlledFontAsset] = {}
        for item in payload["assets"]:
            asset_path = (self._repository_root / str(item["path"])).resolve()
            try:
                asset_path.relative_to(self._repository_root)
            except ValueError as error:
                raise PortCallError(
                    ErrorCode.FONT_INTEGRITY_FAILED,
                    False,
                    "字体路径越出仓库根",
                ) from error
            self._assets[str(item["id"])] = ControlledFontAsset(
                font_id=str(item["id"]),
                path=asset_path,
                sha256=str(item["sha256"]),
            )
        self._system_probe_count = 0

    @property
    def manifest_hash(self) -> str:
        """返回字体 manifest 的真实内容哈希，供 Kernel/Checkpoint 指纹使用。"""

        return _sha256_file(self._manifest_path)

    @property
    def system_probe_count(self) -> int:
        """返回宿主机系统字体探测次数；受控实现始终保持为零。"""

        return self._system_probe_count

    def resolve(self, font_id: str) -> ControlledFontAsset:
        """解析已登记字体，并在每次使用前核对真实文件和 SHA-256。"""

        LOGGER.info("调用受控字体解析，意图=拒绝未登记或漂移字体 font_id=%s", font_id)
        asset = self._assets.get(font_id)
        if asset is None:
            raise PortCallError(ErrorCode.FONT_NOT_REGISTERED, False, "字体未登记")
        if not asset.path.is_file() or _sha256_file(asset.path) != asset.sha256:
            raise PortCallError(ErrorCode.FONT_INTEGRITY_FAILED, False, "字体文件或哈希无效")
        try:
            pymupdf.Font(fontfile=str(asset.path))
        except Exception as error:
            raise PortCallError(
                ErrorCode.FONT_INTEGRITY_FAILED,
                False,
                f"字体无法加载:{type(error).__name__}",
            ) from error
        return asset

    def probe(self, font_id: str, text: str) -> FontProbe:
        """探测已登记字体的真实文件完整性、可加载性和全部非空白字形。"""

        LOGGER.info("调用字体字形探测，意图=验证译文字形覆盖 font_id=%s", font_id)
        asset = self._assets.get(font_id)
        unique_characters = tuple(
            dict.fromkeys(character for character in text if not character.isspace())
        )
        all_codepoints = tuple(f"U+{ord(character):04X}" for character in unique_characters)
        if asset is None:
            return FontProbe(font_id, False, False, False, None, all_codepoints)
        integrity_passed = asset.path.is_file() and _sha256_file(asset.path) == asset.sha256
        if not integrity_passed:
            return FontProbe(font_id, True, False, False, None, all_codepoints)
        try:
            font = pymupdf.Font(fontfile=str(asset.path))
            missing = tuple(
                f"U+{ord(character):04X}"
                for character in unique_characters
                if not font.has_glyph(ord(character))
            )
            return FontProbe(
                font_id,
                True,
                True,
                True,
                int(font.glyph_count),
                missing,
            )
        except Exception:
            return FontProbe(font_id, True, True, False, None, all_codepoints)

    def validate_all(self) -> tuple[FontProbe, ...]:
        """按 manifest 声明的 required_codepoints 验证全部受控字体资产。"""

        payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        probes: list[FontProbe] = []
        for item in payload["assets"]:
            characters = "".join(chr(int(value[2:], 16)) for value in item["required_codepoints"])
            probes.append(self.probe(str(item["id"]), characters))
        return tuple(probes)


def main() -> int:
    """记录字体注册表只接受显式 manifest 与仓库根。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ControlledFontRegistry 示例，意图=禁止隐式系统字体探测")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
