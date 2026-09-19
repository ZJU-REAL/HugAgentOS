"""Desktop-only runtime instructions and omitted container sections."""

# Authoritative override appended on the desktop LOCAL backend. "My Space" is a
# cloud concept and does not exist locally; this cancels all the /myspace/ +
# artifact-net-disk guidance from the base prompt so the model works on the
# user's real local files instead.
LOCAL_MODE_OVERRIDE = (
    "## 【本机模式 · 最高优先级，覆盖上文】\n"
    "你现在运行在**用户本机电脑**上（桌面本地模式），沙盒就是用户电脑的**真实文件系统**。\n"
    "**用真实的本机绝对路径直接读写/运行文件**——`Read`/`Write`/`Edit`/`Glob`/`Grep`/`bash` 在本机模式下"
    "**都接受并推荐使用真实路径**。当前本地项目关联的真实文件夹路径已在项目上下文里给出，直接在它下面操作。\n"
    "**本机没有「我的空间」**（那是云端概念）：上文所有关于 `/myspace/`、`pin_to_workspace`、"
    "`list_myspace_files`、`CreateFolder`/`Move`/`Delete` 我的空间、「存到我的空间/留档」的说明，"
    "在本机模式下**一律不适用，请忽略**，也**不要**往 `/myspace/` 写。\n"
    "- 交付产物：直接写进用户的真实文件夹即可，他在本机就能看到；不需要 pin 到我的空间。\n"
    "- 越权目录与危险命令受本机权限策略约束，可能被拦截或需用户确认；改本机文件前系统会自动快照、可回滚。"
)


SKIP_PARTS = {
    "code_exec": {"system/00_sandbox_environment", "system/10_tools_and_capabilities"},
}
