"""Absolute paths to shipped extensions, independent of the working directory."""

from pathlib import Path

EXTENSIONS_DIR = Path(__file__).resolve().parent
SKILLS_DIR = EXTENSIONS_DIR / "skills"
AGENTS_DIR = EXTENSIONS_DIR / "agents"
PLUGINS_DIR = EXTENSIONS_DIR / "plugins"
MCP_SERVERS_DIR = EXTENSIONS_DIR / "mcp_servers"
DEFAULT_AGENTS_DIR = AGENTS_DIR / "default"
