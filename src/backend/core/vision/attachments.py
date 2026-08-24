"""把本轮上传的图片附件转写成一段可注入的文本。

抽出来是因为有两个调用方，且**它们想要的时机不同**：

- ``orchestration/streaming.py`` 在进入模型流之前先调它，好在识图这几秒里给前端发
  「图像理解中」的状态事件——识图是纯网络等待，不告诉用户的话，界面上只有一个笼统的
  「深度拥抱中」在走秒。
- ``core/llm/middlewares.py`` 的 FileContextMiddleware 在装配上下文时兜底调它（非流式
  入口、渠道机器人等没走流式那条路的场景）。

所以这里只负责「图 → 文本」，不碰 agent 上下文，也不管状态事件——注入和发事件各自留在
调用方。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class AttachmentVisionResult:
    """一轮附件识图的产物。"""

    # 可直接作为一条 user 消息注入的文本；空串表示无需注入
    text: str = ""
    # 实际参与识图的图片名（供状态事件展示）
    names: list[str] = field(default_factory=list)
    # 识图用的模型名；走降级提示时为空
    model: str = ""
    # 是否真的完成了识图（False = 未配置视觉模型 / 全部失败，text 里是降级提示）
    ok: bool = False
    duration_seconds: float = 0.0

    @property
    def count(self) -> int:
        return len(self.names)


def image_attachments(uploaded_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出图片类附件。"""
    from core.llm.hooks import _is_image

    return [f for f in (uploaded_files or []) if _is_image(f)]


async def transcribe_attachments(
    image_files: list[dict[str, Any]],
    *,
    user_id: Optional[str] = None,
) -> Optional[AttachmentVisionResult]:
    """把图片附件转写成注入文本。

    返回 ``None`` 表示无事可做（没有图片、或一张都取不回字节），调用方什么都不用注入。
    返回结果里 ``ok=False`` 时 ``text`` 是给模型看的降级说明，仍然要注入——否则模型会
    当作用户根本没发图，答得驴唇不对马嘴。
    """
    if not image_files:
        return None

    from core.vision import get_vision_bridge, render_many, render_unavailable
    from core.vision.service import is_available

    names = [f.get("name") or "图片" for f in image_files]
    if not is_available():
        return AttachmentVisionResult(text=render_unavailable(names), names=names)

    from core.llm.hooks import _download_artifact_bytes

    payloads: list[tuple[bytes, str]] = []
    kept_names: list[str] = []
    for f in image_files:
        raw = _download_artifact_bytes(
            f.get("file_id") or "",
            f.get("name") or "image",
            "Vision bridge",
            user_id=user_id,
        )
        if raw:
            payloads.append((raw, (f.get("mime_type") or "").lower()))
            kept_names.append(f.get("name") or "图片")
    if not payloads:
        return None

    started = time.monotonic()
    results = await get_vision_bridge().describe_many(payloads)
    elapsed = time.monotonic() - started

    pairs = [
        (name, result.evidence)
        for name, result in zip(kept_names, results)
        if result is not None
    ]
    if not pairs:
        # 桥配了但每张都失败——要明说，不能让模型以为没收到图
        logger.warning("[vision] all %d attachment reads failed", len(payloads))
        return AttachmentVisionResult(
            text=render_unavailable(kept_names, reason="视觉模型调用失败"),
            names=kept_names,
            duration_seconds=elapsed,
        )

    model_name = next((r.model for r in results if r is not None), "")
    return AttachmentVisionResult(
        text=render_many(pairs, model=model_name),
        names=kept_names,
        model=model_name,
        ok=True,
        duration_seconds=elapsed,
    )
