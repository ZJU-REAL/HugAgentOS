# internet_search MCP Server

Standalone **stdio MCP server** exposing HugAgentOS tool:

- Tool: `internet_search(query: str, max_results: int = 5, topic: str = "general", include_raw_content: bool = False) -> Any`

## Run

```bash
python3 -m pip install mcp

python3 -m mcp_servers.internet_search_mcp.server

# Or (recommended)
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server
```

## Local self-test

```bash
python3 -m mcp_servers.internet_search_mcp._selftest

# Or (recommended)
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp._selftest
```

## Notes

- StdIO transport: underlying tool prints are captured and forwarded to stderr.
- Requires `TAVILY_API_KEY` at runtime for real searches.
