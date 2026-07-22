"""Community edition has no external knowledge-provider probe."""


async def test_external_knowledge(base_url: str, api_key: str) -> dict:
    return {"success": False, "latency_ms": 0, "error": "unsupported"}


__all__ = ["test_external_knowledge"]
