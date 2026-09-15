"""工具返回的图片原样进模型，永不落盘、永不降级；送达形式按线路协议分两种。

AgentScope 的 ``FormatterBase.convert_tool_result_to_string`` 对工具结果里的
``DataBlock`` 有三条出路：协议支持就提升成一条独立的 user 消息；``URLSource``
退化成一行 URL 文案；``Base64Source`` 则写进宿主机 ``tempfile``（``delete=False``）
再把路径塞给模型。后两条在本项目里都是错的：

- 那个临时文件落在 **backend 容器**，而智能体的 shell 跑在**沙箱容器**里，模型
  照着路径去读永远读不到——一条指向不存在位置的死链，还每调用一次泄漏一份文件。
- 图被换成文案就是静默降级：调用方已经按「这个模型能看图」注册了工具
  （见 :func:`core.vision.resolve_vision_mode`），到这一层再悄悄抽掉像素，
  没有任何日志或报错，排障时完全看不见。

:class:`NoDiskToolMediaMixin` 堵掉落盘和静默降级，两条线都用。**图放在哪一条消息上
则按协议走各自的原生机制**：

- **Responses**：折回 ``function_call_output`` 自己的 ``output``（
  :class:`ResponsesToolMediaMixin`）。该协议的函数输出本就能载图，codex 也是这么做的
  （``FunctionCallOutputContentItem::InputImage``，见
  ``codex-rs/core/src/tools/handlers/view_image.rs``），图与 ``call_id`` 直接绑定，
  多图并发天然不串。
- **Chat**：沿用 AgentScope 的做法——紧随 tool 回执之后另起一条 user 消息载图。
  Chat Completions 的 ``role="tool"`` 按 OpenAI 规范只接受文本部件（SDK 的
  ``ChatCompletionToolMessageParam`` 就是这么声明的）。这条消息只是给模型的载体，
  由 formatter 在出线时生成，不进对话历史，界面上看不到它。

⚠️ **已知风险，改这里之前先看**：2026-09 曾在 **Chat 线**上做过对照实测（同一组请求、
只有图的位置不同、交叉重复 6 轮）：``DeepSeek-V4-Flash-Vision``（vLLM）在图折进 tool
回执时 6/6 读出图中口令，移到 user 消息则 0/6，且模型不报错、会编造内容；
``qwen3.6-plus`` 两种位置都 6/6。也就是说 Chat 线上有网关只认「图跟着函数输出走」。
当前按协议原生机制划分，Chat 线因此回到 user 消息；若该网关仍在用且读图失效，
先重跑这组对照再决定，不要凭规范推断。

能力判定仍然只有一处：模型能不能看图由 ``resolve_vision_mode`` 在注册工具时决定。
真有协议压根收不下的媒体走到这里，说明上游判定和线路能力不一致——记 ``error`` 级日志，
并把**一条明确的失败回执**交给模型，让它知道「这里本该有张图、但它没到」。不抛错、
不中断这一轮：请求整体失败会把智能体正在跑的任务直接打断，而这次失败是可以如实说明、
让它自己决定怎么绕的。区别于旧行为的关键在于「明说」——绝不写个文件把路径塞给它、
假装图还在。
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, List

from agentscope.message import DataBlock, Msg, TextBlock

if TYPE_CHECKING:  # 类型上它确实是个 formatter；运行时刻意不继承，避免带进字段默认值
    from agentscope.formatter import FormatterBase as _MixinBase
else:
    from pydantic import BaseModel as _MixinBase

logger = logging.getLogger(__name__)


def _undeliverable_notice(media_type: str, patterns: list[str]) -> str:
    """替代那张图交给模型的说明。它是一次失败回执，不是图片内容。

    必须让模型明确知道「这里本该有张图、但它没到」，否则它会拿工具结果里剩下的
    元数据（文件名、尺寸）硬猜图里画了什么——那比直接告诉它失败更糟。
    """
    return (
        f"<media-undeliverable>本次工具结果里有一份 {media_type} 媒体未能送达："
        f"当前线路只接受 {'、'.join(patterns) if patterns else '纯文本'}。"
        "这是一次失败回执，不是媒体内容——不要据此推断该媒体里有什么，"
        "也不要假装已经看过它。请改用其它方式获取所需信息，或如实说明这次没能看到。"
        "</media-undeliverable>"
    )


class NoDiskToolMediaMixin(_MixinBase):
    """工具结果里的媒体一律随消息内联送走，绝不写进宿主机文件系统。"""

    def convert_tool_result_to_string(
        self,
        output: str | List[TextBlock | DataBlock],
    ) -> tuple[str, list[TextBlock | DataBlock]]:
        if isinstance(output, str):
            return output, []

        textual: list[str] = []
        media: list[TextBlock | DataBlock] = []
        for block in output:
            if isinstance(block, TextBlock):
                textual.append(block.text)
            elif isinstance(block, DataBlock):
                if self._is_sendable(block):
                    media.append(block)
                else:
                    textual.append(
                        _undeliverable_notice(
                            block.source.media_type, self.supported_input_media_types
                        )
                    )
        return "\n".join(textual), media

    def _is_sendable(self, block: DataBlock) -> bool:
        """线路能不能收下这份媒体。收不下要吵，但不能掀桌。

        走到这里还收不下，说明上游的能力判定（``core.vision.resolve_vision_mode``）
        与线路实际能力不一致——那是配置或代码的 bug，得有人去修，所以记 ``error``
        级日志。但它不该连累这一轮对话：整个请求抛错会把智能体正在跑的任务直接打断，
        而这次失败本身是可以如实告诉模型、让它自己决定怎么绕的。所以降级只有这一种
        形态——**明说失败**，而不是写个文件把路径塞给它、假装图还在。
        """
        media_type = block.source.media_type
        patterns = self.supported_input_media_types
        if any(fnmatch(media_type, pattern) for pattern in patterns):
            return True
        logger.error(
            "[tool-media] %s 收不下 %s（线路支持 %s）；已如实回执给模型。"
            "工具本不该在这个模型上返回该媒体，请检查能力判定。",
            type(self).__name__,
            media_type,
            patterns or "纯文本",
        )
        return False


class ResponsesToolMediaMixin(NoDiskToolMediaMixin):
    """把媒体折回它所属的那条 ``function_call_output``（Responses 协议）。

    父类 formatter 会把媒体提升成紧跟其后的一条 user 消息。那条消息与产出它的调用
    之间只剩位置关系，多图并发时要靠额外的编号文案才能对上号；Responses 的函数输出
    本身就能载内容部件，折回去即可与 ``call_id`` 直接绑定。
    """

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        # 逐条消息格式化后再折叠：父类对每条消息的处理彼此独立，逐条与整批结果一致，
        # 而逐条能保证「函数输出后面那条纯媒体 user 项」只可能是本条消息的提升产物，
        # 不会误收下一轮真实的用户消息。
        items: list[dict[str, Any]] = []
        for msg in msgs:
            # Concrete SDK formatter follows this mixin in each bound class MRO.
            formatted = await super().format([msg])  # type: ignore[safe-super]
            items.extend(_fold_media_into_function_output(formatted))
        return items


def _fold_media_into_function_output(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    folded: list[dict[str, Any]] = []
    for item in items:
        target = folded[-1] if folded else None
        if (
            target is not None
            and target.get("type") == "function_call_output"
            and _is_media_only(item)
        ):
            target["output"] = _as_output_parts(target.get("output")) + list(item["content"])
            continue
        folded.append(item)
    return folded


def _is_media_only(item: dict[str, Any]) -> bool:
    """这条 user 项是不是上一条函数输出提升出来的纯媒体项。

    ``convert_tool_result_to_string`` 只交出 ``DataBlock``，不再带任何说明文案，
    所以提升出来的项必然是「清一色非文本的内容部件」——据此判定，不去认父类内部
    给这条消息起的名字。
    """
    content = item.get("content")
    if item.get("role") != "user" or not isinstance(content, list) or not content:
        return False
    return all(isinstance(part, dict) and part.get("type") != "input_text" for part in content)


def _as_output_parts(output: Any) -> list[dict[str, Any]]:
    if isinstance(output, list):
        return list(output)
    if output:
        return [{"type": "input_text", "text": output}]
    return []
