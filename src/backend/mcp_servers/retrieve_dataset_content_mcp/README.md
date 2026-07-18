# retrieve_dataset_content MCP Server

Standalone **stdio MCP server** exposing HugAgentOS tool:

- Tool: `retrieve_dataset_content(dataset_id: str, query: str, top_k: int = 10, score_threshold: float = 0.4, search_method: str = "hybrid_search", reranking_enable: bool = False, weights: float = 0.6) -> list`

## Run

```bash
python3 -m pip install mcp

python3 -m mcp_servers.retrieve_dataset_content_mcp.server

# Or (recommended)
PYTHONPATH=src/backend python -m mcp_servers.retrieve_dataset_content_mcp.server
```

## Local self-test

```bash
python3 -m mcp_servers.retrieve_dataset_content_mcp._selftest

# Or (recommended)
PYTHONPATH=src/backend python -m mcp_servers.retrieve_dataset_content_mcp._selftest
```

## Notes

- StdIO transport: underlying tool prints are captured and forwarded to stderr.
- The underlying `search.retrieve_dataset_content` calls Dify knowledge base API and may require `DIFY_URL`/`DIFY_API_KEY`.
- `dataset_id` must be a real ID from your current Dify instance (you can get it from `/v1/catalog` -> `kb[].id`).
- The tool `description` is dynamically injected in `server.py` with a runtime KB list (`dataset_id | name | description`), so the model should pick `dataset_id` from that list.
- Retrieval output is token-truncated with upper bound `50,000` (override by env `RETRIEVE_DATASET_TOKEN_LIMIT`).
