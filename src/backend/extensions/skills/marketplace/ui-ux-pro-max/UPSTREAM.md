# UI/UX Pro Max 上游记录

- 上游仓库：<https://github.com/nextlevelbuilder/ui-ux-pro-max-skill>（`.claude/skills/ui-ux-pro-max/`）
- 上游作者：Next Level Builder
- 固定 commit：`713eef51b04efc98d6fa565323b495f7c9c89fd4`
- commit 时间：2026-11-02T23:10:22+07:00
- 导入日期：2026-11-03
- 许可证：MIT，见 `LICENSE`

## 本地适配

- 只导入主技能 `ui-ux-pro-max`；同仓库的 banner-design、brand、design、design-system、slides、ui-styling 子技能以及 CLI、文档站未导入，`scripts/tests` 未导入。
- `SKILL.md` 中 11 处 `python "${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/scripts/…"` 改为 `python3 "{baseDir}/scripts/…"`。
- `SKILL.md` 开头加 HugAgentOS 运行说明（`--persist` 输出位置、中文字体栈），description 末尾补充中文触发词。

除以上适配外，数据、参考资料和脚本均来自上述固定 commit。
