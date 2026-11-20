"""Call the configured embedding service."""
from __future__ import annotations
import logging
import time

from core.models import embedding_state
from core.models.service_endpoint import endpoint_headers, http_session, resolve_service_config

logger = logging.getLogger(__name__)

def resolve_embed_config() -> tuple[str, str, str]:
    """Resolve embedding config from DB, with env fallback."""
    return resolve_service_config(
        "embedding",
        "MEM0_EMBED_URL",
        "MEM0_EMBED_MODEL",
        "MEM0_EMBED_API_KEY",
        failure_site="core.models.embedding:resolve_embed_config",
    )


def _embed_endpoint() -> tuple[str, dict, str, str]:
    """Return ``(url, headers, model, base_url)`` or raise when not configured."""
    embed_url, embed_model, api_key = resolve_embed_config()
    if not embed_url:
        raise RuntimeError("Embedding model is not configured")
    return f"{embed_url}/embeddings", endpoint_headers(api_key, embed_url), embed_model, embed_url


def embed_request(
    url: str,
    headers: dict,
    inputs: list[str],
    embed_model: str,
    *,
    timeout: float | None = None,
) -> list[list[float]]:
    """Post a single embedding request and return vectors ordered by input index."""
    payload = {"input": inputs, "model": embed_model}
    resp = http_session().post(
        url,
        headers=headers,
        json=payload,
        timeout=timeout or embedding_state.EMBED_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]


def embed_text(text: str, *, timeout: float | None = None) -> list[float]:
    """Call the configured embedding service and return a dense vector."""
    url, headers, embed_model, embed_url = _embed_endpoint()
    vector = embed_request(url, headers, [text], embed_model, timeout=timeout)[0]
    if vector:
        embedding_state.process_state().DETECTED_DIM_CACHE[(embed_url, embed_model)] = len(vector)
    return vector


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts, splitting into sub-batches with one-by-one fallback.

    A large document is split into hundreds or thousands of chunks. Sending the whole batch at once to a slow embedding
    model (e.g. 8B, ~0.45s/item serially) would time out the single request and fail the whole document's vectorization. So we
    split into sub-batches of _EMBED_BATCH_SIZE and request them one sub-batch at a time; if a sub-batch fails, we downgrade to
    embedding one item at a time within that sub-batch, preserving as many other chunks as possible so one timeout doesn't drag
    down the whole document. Only if the one-by-one attempt still fails do we re-raise (treated as a true failure).
    """
    url, headers, embed_model, _ = _embed_endpoint()

    results: list[list[float]] = []
    for start in range(0, len(texts), embedding_state.EMBED_BATCH_SIZE):
        sub = texts[start : start + embedding_state.EMBED_BATCH_SIZE]
        try:
            results.extend(embed_request(url, headers, sub, embed_model))
        except Exception as exc:
            logger.warning(
                "Sub-batch embed failed for [%d:%d] (%s); falling back to one-by-one",
                start,
                start + len(sub),
                exc,
            )
            for one in sub:
                results.extend(embed_request(url, headers, [one], embed_model))
    return results


def _dim_key() -> tuple[str, str]:
    embed_url, embed_model, _ = resolve_embed_config()
    return embed_url, embed_model


def measured_embed_dim() -> int | None:
    """The dimension actually measured for the current model, or None if never measured
    (unlike detect_embed_dim, never probes and never returns the configured fallback)."""
    return embedding_state.process_state().DETECTED_DIM_CACHE.get(_dim_key())


def detect_embed_dim() -> int:
    """Probe the real output dimension of the currently chosen embedding model (one /embeddings call, take the vector length).

    The result is cached by (embed_url, embed_model) to avoid repeated probing on every index; on probe failure (endpoint not
    configured/unreachable) it falls back to MEM0_EMBED_DIMS. This way the table dimension auto-follows after switching the
    embedding model, no manual filling needed.
    """
    key = _dim_key()
    embed_model = key[1]
    state = embedding_state.process_state()
    cached = state.DETECTED_DIM_CACHE.get(key)
    if cached:
        return cached
    failed_at = state.FAILED_DIM_PROBE_AT.get(key)
    if failed_at is not None and time.monotonic() - failed_at < embedding_state.DIM_PROBE_RETRY_SECONDS:
        return embedding_state.EMBED_DIMS
    try:
        dim = len(embed_text("dimension probe"))
        if dim > 0:
            state.DETECTED_DIM_CACHE[key] = dim
            state.FAILED_DIM_PROBE_AT.pop(key, None)
            logger.info("Auto-detected embedding dim=%d (model=%s)", dim, embed_model or "?")
            return dim
    except Exception as exc:
        state.FAILED_DIM_PROBE_AT[key] = time.monotonic()
        logger.warning(
            "Embedding dim auto-detect failed (%s); falling back to MEM0_EMBED_DIMS=%d",
            exc,
            embedding_state.EMBED_DIMS,
        )
    return embedding_state.EMBED_DIMS
