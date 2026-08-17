/**
 * 管理类插件的写操作 → 需要重拉的那个 store。
 *
 * 拆成独立纯模块有两个原因：一是它是数据不是行为，二是 chatStream.ts 依赖 antd/stores，
 * 没法在 node 测试里直接 import，而这张表恰恰是最需要被钉住的东西——
 * 它已经错过两次：edit_skill 长期漏在表外，以及 agent/plugin 的写操作刷了 catalog
 * 这个根本不包含它们的 store。
 *
 * 三份列表来自三个不同接口，刷错了不会报错，只会静默无效：
 *   - 私有技能   → useCatalogStore  ← GET /v1/catalog
 *   - 子智能体   → useAgentStore    ← GET /v1/agents
 *   - 已装插件   → usePluginStore   ← GET /v1/plugins/installed
 *
 * 注意：插件带来的技能/工具**不出现在** /v1/catalog（插件是整体绑定的单元），
 * 所以装卸插件对 catalog 响应毫无影响，只有插件 store 会变。
 */
export type RefreshTarget = 'catalog' | 'agents' | 'plugins';

export const MUTATING_TOOL_REFRESH: Record<string, RefreshTarget> = {
  // skill-manager → 用户的私有技能库
  register_skill: 'catalog',
  install_from_marketplace: 'catalog',
  delete_skill: 'catalog',
  edit_skill: 'catalog',
  // agent-manager → 用户的子智能体
  create_agent: 'agents',
  edit_agent: 'agents',
  delete_agent: 'agents',
  install_market_agent: 'agents',
  // plugin-manager → 用户已安装的插件
  install_plugin: 'plugins',
  uninstall_plugin: 'plugins',
  import_plugin: 'plugins',
  set_plugin_enabled: 'plugins',
};

// 只读动词（search_ / list_ / get_ 开头）与"申请上架"不改任何列表，故返回 undefined。
// 注意别在块注释里写 `search_*` 加斜杠的形式——那会提前闭合注释。
export function refreshTargetForTool(bareToolName: string): RefreshTarget | undefined {
  return MUTATING_TOOL_REFRESH[bareToolName];
}
