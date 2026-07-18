"""Core package.

Keep package import side-effect free:
- Do not import submodules here.
- Import from concrete modules, e.g. `from core.chat.context import build_runtime_context`.
"""

__all__ = []
