# Guizang PPT Skill 上游记录

- 上游仓库：<https://github.com/op7418/guizang-ppt-skill>
- 上游作者：歸藏（GitHub: `op7418`）
- 固定 commit：`c91369c449d34755d320a8b81d0734000d99d1ab`
- commit 时间：2026-08-07T03:58:06Z
- 导入日期：2026-08-24
- 许可证：GNU Affero General Public License v3.0；完整文本见 `LICENSE`

## 本地适配

- 删除上游仓库维护文件、宣传截图和贡献文档，只保留运行时必需的模板、参考资料、背景资产和校验脚本。
- 将上游 `<SKILL_ROOT>` 路径替换为 HugAgentOS 支持的 `{baseDir}` 占位符。
- 禁用技能运行时的 Git 自更新；marketplace 安装目录是固定版本的只读材料。
- 生成 deck 时复制本地 `assets/motion.min.js`，避免模板在断网时遗漏 Motion One 的本地回退资源。
- 将默认输出位置改为 `/workspace/outputs/`，并要求整目录 ZIP 交付，防止相对图片与本地脚本丢失。
- 图像生成步骤改为能力检测：仅在当前智能体实际注册了对应工具时启用。

除以上 HugAgentOS 运行适配外，模板、参考资料、背景图片和校验脚本均来自上述固定 commit。
