"""skills state responsibilities."""
from __future__ import annotations
import threading
from collections import OrderedDict
from typing import Dict, Tuple
from features.desktop.capabilities.resolver import Resolution

SKIP_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}


INVENTORY_NAME = ".inventory.json"


hash_lock = threading.Lock()


hash_cache: Dict[str, Tuple[Tuple, str]] = {}


content_hash_cache: OrderedDict[tuple, str] = OrderedDict()


resolution_lock = threading.Lock()


last_resolution: Dict[str, Resolution] = {}


resolution_cache: Dict[str, Tuple[Tuple, Resolution]] = {}


view_generation = 0


shadow_user_ids: Dict[str, str] = {}
