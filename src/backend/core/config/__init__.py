# ``core.config`` holds static config + ``settings`` only and never imports
# ``core.db`` at module level. DB-backed config services live elsewhere:
#   from core.models.config import ModelConfigService
#   from core.config.store.system_config import SystemConfigService
#   from core.capabilities.mcp.service import McpServerConfigService
