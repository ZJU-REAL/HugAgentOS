"""lark API configuration."""
import os
LARK_API_BASE = os.getenv("LARK_API_BASE", "https://open.feishu.cn").rstrip("/")
