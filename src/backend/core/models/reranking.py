"""Call the configured reranking service."""
from __future__ import annotations
import os
import requests

def resolve_reranker_config() -> tuple[str, str, str]:
    """Resolve reranker config from DB, with env fallback."""
    try:
        from core.models.config import ModelConfigService

        cfg = ModelConfigService.get_instance().resolve("reranker")
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.model_name, cfg.api_key
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.models.reranking:resolve_reranker_config', fallback_error)
    return (
        os.getenv("RERANKER_URL", "").rstrip("/"),
        os.getenv("RERANKER_MODEL", ""),
        os.getenv("RERANKER_API_KEY", ""),
    )


def is_reranker_configured() -> bool:
    """Check if a reranker model endpoint is configured."""
    reranker_url, reranker_model, _ = resolve_reranker_config()
    return bool(reranker_url and reranker_model)


def rerank(
    query: str,
    documents: list[str],
    top_n: int | None = None,
    *,
    timeout: float = 30,
) -> list[dict]:
    """Call the configured reranker endpoint (OpenAI-compatible /rerank).

    Returns a list of {"index": int, "relevance_score": float} sorted by score descending.
    """
    reranker_url, reranker_model, reranker_key = resolve_reranker_config()

    if not reranker_url or not reranker_model:
        raise RuntimeError("Reranker is not configured")

    url = f"{reranker_url}/rerank"
    from core.harness.registry.model_credentials import prepare_request_headers
    headers = prepare_request_headers(reranker_key, reranker_url)
    payload: dict = {
        "model": reranker_model,
        "query": query,
        "documents": documents,
    }
    if top_n is not None:
        payload["top_n"] = top_n

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    # Response format: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
    results = data.get("results", [])
    return sorted(results, key=lambda x: x.get("relevance_score", 0), reverse=True)
