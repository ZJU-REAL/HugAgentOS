"""视觉桥（Vision Bridge）：让纯文本主模型「看得见」图片。

图片 → 多模态模型 → 结构化文字证据 → 注入主模型上下文。主模型全程只吃文本。

对外入口：

- :func:`core.vision.service.is_available` —— 当前是否配了可用的视觉模型
- :func:`core.vision.service.get_vision_bridge` —— 识别单张/多张图
- :func:`core.vision.render.render_evidence` —— 证据 → 可注入文本（带不可信输入围栏）
- :func:`core.vision.service.model_supports_vision` —— 某个模型配置是否原生多模态
"""

from core.vision.render import render_evidence, render_many, render_unavailable
from core.vision.schema import VisionEvidence, parse_evidence
from core.vision.service import (
    VISION_ROLE_KEY,
    VisionBridge,
    VisionResult,
    get_vision_bridge,
    is_available,
    model_supports_vision,
    resolve_vision_config,
)

__all__ = [
    "VISION_ROLE_KEY",
    "VisionBridge",
    "VisionEvidence",
    "VisionResult",
    "get_vision_bridge",
    "is_available",
    "model_supports_vision",
    "parse_evidence",
    "render_evidence",
    "render_many",
    "render_unavailable",
    "resolve_vision_config",
]
