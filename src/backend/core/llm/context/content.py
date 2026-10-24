"""Normalise, measure and truncate context content."""
from __future__ import annotations
from core.models.constants import IMAGE_TOKEN_RESERVE
from typing import Any, Mapping
from core.infra.immutable import thaw_json
from core.harness.execution_manifest import canonical_json
from core.llm.context import ir_constants

def normalize_context_content(content: Any) -> Any:
    """Convert SDK block objects into stable, framework-neutral JSON values."""
    if hasattr(content, "model_dump"):
        return normalize_context_content(content.model_dump(mode="json"))
    if isinstance(content, Mapping):
        return {
            str(key): normalize_context_content(value)
            for key, value in content.items()
        }
    if isinstance(content, (list, tuple)):
        return [normalize_context_content(value) for value in content]
    return content


def tool_result_images(content: Any) -> list[Any]:
    """Image payloads are atomic media, not part of a tool's text allowance."""
    if not isinstance(content, Mapping) or content.get("type") != "tool_result":
        return []
    output = content.get("output")
    if not isinstance(output, (list, tuple)):
        return []
    return [
        block
        for block in output
        if isinstance(block, Mapping)
        and block.get("type") == "data"
        and isinstance(block.get("source"), Mapping)
        and str(block["source"].get("media_type") or "").startswith("image/")
    ]


def image_token_reserve(content: Any) -> int:
    """Reserve media occupancy without measuring image transport bytes."""
    if isinstance(content, (list, tuple)):
        return sum(image_token_reserve(block) for block in content)
    if isinstance(content, Mapping):
        source = content.get("source")
        if (
            content.get("type") == "data"
            and isinstance(source, Mapping)
            and str(source.get("media_type") or "").startswith("image/")
        ):
            return IMAGE_TOKEN_RESERVE
        if content.get("type") == "tool_result":
            return image_token_reserve(content.get("output", content.get("content")))
    return 0


def model_facing_tool_result(content: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a tool_result the provider actually receives."""
    return {
        key: content[key] for key in ir_constants.TOOL_RESULT_MODEL_FIELDS if key in content
    }


def estimate_context_tokens(content: Any) -> int:
    """Deterministic conservative estimate without provider-specific imports."""
    content = normalize_context_content(content)
    if content is None:
        return 0
    if isinstance(content, Mapping) and content.get("type") == "tool_result":
        content = model_facing_tool_result(content)
    if isinstance(content, str):
        return max(1, (len(content.encode("utf-8")) + 3) // 4) if content else 0
    if isinstance(content, Mapping) and content.get("type") == "data":
        # Provider image accounting is not proportional to base64 bytes. Keep a
        # conservative fixed reserve without letting transport encoding evict
        # the actual attachment before the provider can count it precisely.
        return IMAGE_TOKEN_RESERVE
    if tool_result_images(content):
        # Count image reserves recursively instead of tokenizing their base64
        # inside the enclosing tool-result JSON.
        return estimate_context_tokens({**content, "output": []}) + sum(
            estimate_context_tokens(block) for block in content["output"]
        )
    if isinstance(content, list):
        return sum(estimate_context_tokens(item) for item in content)
    return max(1, (len(canonical_json(content).encode("utf-8")) + 3) // 4)


def truncate_text(text: str, max_tokens: int, *, tail_only: bool = False) -> str:
    if not text or max_tokens <= 0:
        return ""
    max_bytes = max(4, max_tokens * 4)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    # Character slicing is deterministic and safe for Unicode. Iterate down if
    # multi-byte text still exceeds the byte reserve.
    max_chars = max(1, max_bytes)
    head = 0
    tail = 0
    marker = ""
    if tail_only:
        tail = max_chars
        result = text[-tail:]
    elif max_chars < 40:
        head = max_chars
        result = text[:head]
    else:
        marker = "\n[… omitted …]\n"
        body = max(2, max_chars - len(marker))
        head = max(1, int(body * 0.6))
        tail = max(1, body - head)
        result = text[:head] + marker + text[-tail:]
    while len(result.encode("utf-8")) > max_bytes and len(result) > 1:
        if tail_only:
            tail = max(1, tail - 1)
            result = text[-tail:]
        elif marker:
            # Preserve both semantic ends; shrink the larger slice first.
            if head >= tail and head > 1:
                head -= 1
            elif tail > 1:
                tail -= 1
            elif head > 1:
                head -= 1
            else:
                break
            result = text[:head] + marker + text[-tail:]
        else:
            head = max(1, head - 1)
            result = text[:head]
    return result


def truncate_content(content: Any, max_tokens: int, policy: str) -> Any:
    """Shorten prunable text only; every structured block keeps its shape.

    Plain text and a tool result's output are the only things a budget may
    shorten. Any other block — thinking, tool call, data, or a shape this
    module does not know — is returned untouched so the caller keeps or drops
    it whole. Rewriting such a block into a JSON string would hand the model
    its own reasoning back as an answer.
    """
    if max_tokens <= 0:
        return ""
    if isinstance(content, str):
        return truncate_text(content, max_tokens, tail_only=policy == ir_constants.POLICY_TAIL)
    if isinstance(content, Mapping):
        mutable = thaw_json(content)
        block_type = str(mutable.get("type") or "")
        if block_type not in ir_constants.TRUNCATABLE_BLOCK_TYPES:
            return mutable
        for key in ("output", "content", "text"):
            value = mutable.get(key)
            if isinstance(value, str):
                overhead = estimate_context_tokens({**mutable, key: ""})
                mutable[key] = truncate_text(
                    value,
                    max(1, max_tokens - overhead),
                    tail_only=policy == ir_constants.POLICY_TAIL,
                )
                return mutable
            if key == "output" and isinstance(value, (list, Mapping)):
                images = tool_result_images(mutable)
                if images:
                    text = "\n".join(
                        (
                            str(block["text"])
                            if isinstance(block, Mapping) and block.get("text") is not None
                            else block if isinstance(block, str) else canonical_json(block)
                        )
                        for block in value
                        if block not in images
                    )
                    output = [{"type": "text", "text": ""}, *images]
                    overhead = estimate_context_tokens({**mutable, key: output})
                    output[0]["text"] = truncate_text(
                        text, max(1, max_tokens - overhead), tail_only=policy == ir_constants.POLICY_TAIL
                    )
                    mutable[key] = output
                    return mutable
                if isinstance(value, list):
                    parts = []
                    for block in value:
                        if isinstance(block, Mapping) and block.get("text") is not None:
                            parts.append(str(block["text"]))
                        elif isinstance(block, str):
                            parts.append(block)
                        else:
                            parts.append(canonical_json(block))
                    flattened = "\n".join(parts)
                else:
                    flattened = canonical_json(value)
                overhead = estimate_context_tokens({**mutable, key: ""})
                mutable[key] = truncate_text(
                    flattened,
                    max(1, max_tokens - overhead),
                    tail_only=policy == ir_constants.POLICY_TAIL,
                )
                return mutable
        return mutable
    return thaw_json(content)


def prune_tool_output(content: Any, original_tokens: int) -> Any:
    """Replace a tool result's output with an explicit placeholder."""
    mutable = thaw_json(content)
    if not isinstance(mutable, dict) or str(mutable.get("type") or "") != "tool_result":
        raise ValueError("only tool_result content can be pruned")
    images = tool_result_images(mutable)
    if images:
        mutable["output"] = [
            {"type": "text", "text": ir_constants.PRUNED_OUTPUT_TEMPLATE.format(tokens=original_tokens)},
            *images,
        ]
        return mutable
    for key in ("output", "content", "text"):
        if key in mutable:
            mutable[key] = ir_constants.PRUNED_OUTPUT_TEMPLATE.format(tokens=original_tokens)
            return mutable
    mutable["output"] = ir_constants.PRUNED_OUTPUT_TEMPLATE.format(tokens=original_tokens)
    return mutable
