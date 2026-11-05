# Diagram Design 上游记录

- 上游仓库：<https://github.com/cathrynlavery/diagram-design>
- 上游作者：Cathryn Lavery
- 固定 commit：`3eb03b8d2984e47ddd9b083db9abdcdaa3c69635`
- commit 时间：2026-11-03T02:44:49Z
- 导入日期：2026-11-03
- 许可证：MIT，见 `LICENSE`；内置图标的第三方许可（Tabler Icons MIT、Simple Icons CC0 等）见 `THIRD_PARTY_LICENSES.md`

## 本地适配

- 只导入上游 `skills/diagram-design/` 技能目录；仓库根目录的插件清单、文档站、维护脚本和 `verify-*.py` 校验脚本未导入。
- `references/style-guide.md` 的 `Font source` 默认值从 `web` 改为 `system`：国内网络和内网访问不到 Google Fonts。
- `references/style-guide.md` 在简体中文一段补充了包含沙箱已装字体（文泉驿正黑、方正黑体、方正小标宋）的字体栈，保证沙箱里导出 PNG 时中文有字形。
- `SKILL.md` 在开头加了 HugAgentOS 运行说明（`{baseDir}` 路径、只读安装目录、首次设置门的处理、中文与交付约定），脚本调用改为 `{baseDir}/scripts/`，并在 description 末尾补充中文触发词。

除以上适配外，模板、参考资料、示例和脚本均来自上述固定 commit。
