"""Stage only the agent's selected skill packages into an API sandbox."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from core.llm.agent_api_runtime import AgentApiExecutionScope


def _skill_files(skill_dirs: dict[str, str]):
    files = []
    total = 0
    for skill_id, directory in skill_dirs.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", skill_id) or skill_id in {".", ".."}:
            raise ValueError("API 技能标识无效")
        root = Path(directory).resolve(strict=True)
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("API 技能包不能包含软链接")
            if not path.is_file() or any(part in {".git", "__pycache__"} for part in path.relative_to(root).parts):
                continue
            size = path.stat().st_size
            total += size
            if size > 16 * 1024 * 1024 or total > 64 * 1024 * 1024 or len(files) >= 2048:
                raise ValueError("API 技能包超过暂存大小限制")
            files.append((f"/workspace/skills/{skill_id}/{path.relative_to(root).as_posix()}", path.read_bytes()))
    return files


class ApiSkillStager:
    def __init__(self, skill_dirs: dict[str, str] | None):
        self.skill_dirs = skill_dirs or {}
        self.ready = False
        self.lock = asyncio.Lock()

    async def stage(self, provider, scope: AgentApiExecutionScope):
        async with self.lock:
            if self.ready:
                return
            # Validate the entire closure before any files are sent.
            files = await asyncio.to_thread(_skill_files, self.skill_dirs)
            for path, content in files:
                await provider.put_file(
                    scope.sandbox_session_id, path, content, user_id=scope.sandbox_user_id,
                )
            self.ready = True
