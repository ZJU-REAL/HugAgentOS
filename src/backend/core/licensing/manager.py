"""License 门面 —— 社区版 stub。

社区版没有 license 概念：全部商业能力位恒 False、不限席位。
本文件不含任何验签逻辑实现体（验签属商业版闭源代码）。
"""

from __future__ import annotations

from typing import Optional

from .features import Feature, FeatureNotLicensed  # noqa: F401  (re-export 兼容)


class LicenseManager:
    def reload(self) -> None:
        return None

    def mode(self) -> str:
        return "ce"

    def has(self, feature: Feature) -> bool:
        return False

    def features_map(self) -> dict[str, bool]:
        return {f.value: False for f in Feature}

    def require(self, feature: Feature) -> None:
        raise FeatureNotLicensed(feature)

    def seats_allow(self, active_users: int) -> bool:
        return True

    def info(self) -> Optional[dict]:
        return None

    def status(self) -> dict:
        return {
            "edition": "ce",
            "mode": "ce",
            "required": False,
            "license": None,
            "features": self.features_map(),
        }


license_manager = LicenseManager()
