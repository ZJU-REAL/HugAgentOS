"""How authorized space files reach a sandbox.

A transport either mounts the space (the kernel keeps it consistent) or keeps
a synchronized copy inside the sandbox. Sandbox providers only call this
interface; they never branch on the deployment mode themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SyncReport:
    """What a synchronization could not apply as-is, shown after the command."""

    conflicts: list[tuple[str, str]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.conflicts or self.rejected or self.skipped or self.errors)

    def text(self, limit: int = 20) -> str:
        if not self:
            return ""
        lines = ["[空间同步]"]
        for path, copy in self.conflicts[:limit]:
            lines.append(f"冲突：{path} 已被他人修改，保留远端版本；沙盒中的版本另存为 {copy}")
        for path in self.rejected[:limit]:
            lines.append(f"只读：{path} 所在空间不可写，修改未保存，已恢复为远端版本")
        for path in self.skipped[:limit]:
            lines.append(f"跳过：{path} 超出同步大小上限，未放入沙盒")
        for message in self.errors[:limit]:
            lines.append(f"失败：{message}")
        hidden = sum(max(0, len(items) - limit) for items in
                     (self.conflicts, self.rejected, self.skipped, self.errors))
        if hidden:
            lines.append(f"另有 {hidden} 条未列出")
        return "\n".join(lines)


class SpaceTransport:
    """Mount transports keep every hook below a no-op."""

    name = "base"
    # True when the sandbox holds a copy that the hooks below must keep in step.
    synchronizes = False

    def validate(self) -> None:
        """Fail at startup when the deployment cannot serve this transport."""

    def volume(self, grant):
        """The provider volume for one grant, or None when nothing is mounted."""
        raise NotImplementedError

    @staticmethod
    def _volume(grant, **source):
        from opensandbox.models.sandboxes import Volume
        from core.files.spaces.access import fingerprint
        return Volume(name="space-" + fingerprint((grant,))[:24], mount_path=grant.mount_path,
                      read_only=not grant.writable, **source)

    async def ensure(self, sandbox, actor, info, *, domain, api_key) -> None:
        """Verify an existing sandbox still exposes the current grants."""

    async def before_use(self, sandbox, actor, *, path=None) -> SyncReport:
        """Bring remote changes into the sandbox before code reads them.

        ``path`` limits the work to the space holding that sandbox path."""
        return SyncReport()

    async def after_use(self, sandbox, actor, *, path=None) -> SyncReport:
        """Persist sandbox changes after code wrote them."""
        return SyncReport()

    async def flush(self, sandbox, actor, *, force=False) -> SyncReport:
        """Best-effort persistence outside a command (renewal, release)."""
        return SyncReport()
