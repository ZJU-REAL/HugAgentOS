# internet_search MCP Server

Standalone **stdio MCP server** exposing the internet-search tool:

- Tool: `internet_search(query: str, max_results: int = 5, topic: str = "general", search_depth: str = "advanced", include_raw_content: bool = False, cn_only: bool = True) -> Any`

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
- Set `INTERNET_SEARCH_ENGINE` to `tavily`, `baidu`, or `langsearch`.
- Configure only the matching `TAVILY_API_KEY`, `BAIDU_API_KEY`, or
  `LANGSEARCH_API_KEY` for the selected engine.
- `topic`, `search_depth`, and `include_raw_content` are Tavily-specific.
  LangSearch maps its generated summary to the shared result `content` field.
