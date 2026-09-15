"""Image-aware estimates shared by all model providers and SDK compression."""

from agentscope.message import DataBlock, Msg, ToolResultBlock

from core.llm.context_ir import IMAGE_TOKEN_RESERVE


class ImageTokenCountingMixin:
    """Estimate text normally and reserve image tokens independently of base64."""

    async def count_tokens(self, messages: list[Msg], tools: list[dict] | None) -> int:
        images = 0

        def without_images(blocks):
            nonlocal images
            kept = []
            for block in blocks:
                if isinstance(block, DataBlock) and block.source.media_type.startswith("image/"):
                    images += 1
                elif isinstance(block, ToolResultBlock) and isinstance(block.output, list):
                    kept.append(block.model_copy(update={"output": without_images(block.output)}))
                else:
                    kept.append(block)
            return kept

        # These copies are only for estimation; the provider receives the
        # original structured messages with every image byte intact. Messages
        # that hold no media are passed through rather than copied — that is
        # every message on the text-only paths the SDK calls this from.
        text_messages = []
        for message in messages:
            blocks = message.get_content_blocks()
            kept = without_images(blocks)
            text_messages.append(
                message if kept == blocks else message.model_copy(update={"content": kept})
            )
        return (
            await super().count_tokens(messages=text_messages, tools=tools)
            + images * IMAGE_TOKEN_RESERVE
        )
