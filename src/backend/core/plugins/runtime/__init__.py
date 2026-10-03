"""Progressive plugin loading（渐进式插件加载）.

安装后的插件默认把 N 个技能的 yaml 头 + M 个 MCP 工具的完整 JSON Schema 全量
注入每一轮请求（技能进系统提示词末尾的技能清单，工具进 tools 参数），插件越装
越多，首字延迟（TTFT）随之线性膨胀。本模块把「插件」这个聚合单位带回运行时：

- **装配期**：插件的组件（技能 / MCP server）从本轮的 enabled 集合中剔除，系统
  提示词只保留一行「插件目录」条目（插件名 + 描述），并注册一个 ``load_plugin``
  工具。
- **激活期**：模型判断需要某插件时调用 ``load_plugin``，此时才连接该插件的 MCP
  server（追加进 Toolkit basic 组的 mcps）、注册其技能（追加 LocalSkillLoader），
  下一轮 ReAct 请求里工具 schema 与技能清单即出现（AS2 的
  ``_prepare_model_input`` 每轮重算，无缓存）。
- **粘滞性**：激活以精确 ``install_id`` 写入
  ``ChatSession.extra_data["activated_plugins"]``——同一会话后续轮次在个人开关
  筛选前恢复该插件（其组件回到常规装配位置）。通过 ``/`` 或 ``+`` 显式选择
  插件视同激活；管理员停用、卸载、依赖或归属门控仍在每轮重新校验。

与前缀缓存（prefix caching）的关系：插件目录段内容与顺序对同一用户稳定（按
slug 排序、不随激活状态变化），激活行为本身会在「激活那一轮」与「激活后的下一
轮」（组件回到常规装配位）各击穿一次前缀缓存，之后前缀重新稳定。未被引用的
插件则永远不再为每轮 prefill 付费。

关闭开关：环境变量 ``PLUGIN_PROGRESSIVE_LOADING=false`` 回到全量 eager 装配。

范围：主链路装配（``resolve_progressive_plugins``，可见插件 ∩ enabled 集合，
会话粘滞）+ 子智能体装配（``resolve_bound_progressive_plugins``，按绑定的
install_id 解析；子智能体运行短暂且相互隔离，激活只在本次运行内生效、不落库）。
收窄模式（对话模式）圈定的插件面是管理员的显式圈定，保持 eager；组件含 stdio
transport MCP 的插件不延迟（激活期进程内起子进程的生命周期管理复杂度不值得）。
"""

from core.plugins.runtime.activation import register_load_plugin
from core.plugins.runtime.activation_history import (
    load_activated_plugin_slugs,
    record_plugin_activation,
)
from core.plugins.runtime.desktop_resolution import (
    prepare_desktop_plugin_skill_defaults,
    resolve_desktop_progressive_plugins,
)
from core.plugins.runtime.models import (
    DeferredPlugin,
    ProgressiveResolution,
    StickyPluginCapabilities,
    progressive_plugin_loading_enabled,
)
from core.plugins.runtime.resolution import (
    build_plugin_directory_section,
    resolve_bound_progressive_plugins,
    resolve_progressive_plugins,
)
from core.plugins.runtime.sticky_selection import resolve_sticky_plugin_capabilities

__all__ = [
    "progressive_plugin_loading_enabled",
    "DeferredPlugin",
    "ProgressiveResolution",
    "load_activated_plugin_slugs",
    "record_plugin_activation",
    "StickyPluginCapabilities",
    "resolve_sticky_plugin_capabilities",
    "resolve_progressive_plugins",
    "resolve_bound_progressive_plugins",
    "build_plugin_directory_section",
    "prepare_desktop_plugin_skill_defaults",
    "resolve_desktop_progressive_plugins",
    "register_load_plugin",
]
