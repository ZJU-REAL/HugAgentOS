# ``core.config`` holds static config + ``settings`` only (a clean L1 leaf).
# The DB-backed config *services* live in ``core.services`` so config no longer
# imports ``core.db`` at module level:
#   from core.services.model_config import ModelConfigService
#   from core.config.store.system_config import SystemConfigService
#   from core.capabilities.mcp.service import McpServerConfigService
