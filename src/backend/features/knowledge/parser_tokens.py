"""parser tokens responsibilities."""
from __future__ import annotations


def get_tiktoken_enc():
    """Return cached tiktoken cl100k_base encoder."""
    global tiktoken_enc
    if tiktoken_enc is None:
        try:
            import tiktoken
            tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            pass
    return tiktoken_enc


def count_tokens(text: str) -> int:
    """Approximate token count using tiktoken cl100k_base."""
    enc = get_tiktoken_enc()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback: rough Chinese/English estimate
    return max(1, len(text) // 2)


tiktoken_enc = None
