/** 插件 UI 素材库（src/plugin-ui/）与插件画布面板的英文字典。 */
export const PLUGIN_UI_DICT: Record<string, string> = {
  // ── 面板 / 容器 ──
  '插件视图': 'Plugin view',
  '正在获取数据，完成后将在这里自动展开': 'Fetching data. The result will appear here automatically.',
  '该插件视图已不可用（插件可能已停用或卸载）':
    'This plugin view is no longer available (the plugin may be disabled or uninstalled)',
  '该插件声明的展示类型不受支持：{view}': 'Unsupported view type declared by the plugin: {view}',
  '在画布中查看': 'Open in canvas',

  // ── 动作结果 / 分页 ──
  '上一页': 'Previous',
  '下一页': 'Next',

  // ── L2 模块 ──
  '插件模块未能加载，已回退到基础视图': 'The plugin module failed to load; falling back to the basic view',
  '插件模块「{name}」无法加载': 'Plugin module "{name}" could not be loaded',
  '正在加载插件模块…': 'Loading plugin module…',

  // ── 分析型 view ──
  '暂无对比数据': 'No comparison data',
  '暂无可对比的指标': 'No comparable metrics',
  '主体 {n}': 'Subject {n}',
  '指标': 'Metric',
  '暂无分布数据': 'No distribution data',
  '暂无排名数据': 'No ranking data',
  '第 {n} 名': 'No. {n}',
  '暂无评价数据': 'No evaluation data',
  '维度 {n}': 'Dimension {n}',
  '暂无事件': 'No events',
  '事件 {n}': 'Event {n}',
  '暂无趋势数据': 'No trend data',
  '单位：{unit}': 'Unit: {unit}',

  // ── 容器 / 交互型 view ──
  // （展开/收起下级环节、正在加载…、重新加载 等词条已在 panels.ts 中，避免重复登记；
  //   下面三条原本只登记在 EE 字典里，但消费它们的 plugin-ui 视图是 CE/EE 共享代码，
  //   CE 派生会排除 edition-ee/ 字典 → 必须登记在共享字典中）
  '共 {n} 条记录': '{n} records',
  '查看原文': 'View Source',
  '{n} 项': '{n} items',
  '{title} · {n} 个节点': '{title} · {n} nodes',
  '关键指标': 'Key metrics',
  '{name}层级结构': '{name} hierarchy',
  '正在查询…': 'Looking up…',
  '在新窗口查看{name}详情': 'Open {name} details in a new window',
  '暂无条目': 'No entries',
  '暂无链接': 'No link',
  '暂无记录': 'No records',
  '暂无调用链': 'No trace',
  '未命名调用': 'Unnamed span',
  '未返回可展示的层级结构': 'No displayable hierarchy was returned',

  // ── 文档型 view ──
  '暂无指标': 'No metrics',
  '引用锚点 {id}': 'Citation anchor {id}',
};
