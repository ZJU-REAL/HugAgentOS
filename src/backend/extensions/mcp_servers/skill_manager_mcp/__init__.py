"""技能管理 MCP —— 让智能体在对话里搜索/安装/创建/管理/申请上架技能。

只放"沙箱够不着的" 动词（读技能市场 / 写后端 DB）。技能的"创作/下载解包"由本插件
打包的 skill-creator 技能在沙箱内完成，产物经共享产物库（artifact store）交给
``register_skill`` 落库——见 internal design docs。
"""
