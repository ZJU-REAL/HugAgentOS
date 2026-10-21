"""插件管理 MCP —— 让智能体在对话里搜索/安装/导入/启停/卸载插件。

只放"沙箱够不着的"动词（读插件市场 / 写后端 DB）。插件包的"创作/下载解包"由本插件
打包的 plugin-creator 技能在沙箱内完成，产物经共享产物库（artifact store）交给
``import_plugin`` 落库——与 skill-manager 的 register_skill 是同一条通路。
"""
