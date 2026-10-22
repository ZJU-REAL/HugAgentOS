
# 宜搭自定义页面信息密度设计规范

## 严格禁止 (NEVER DO)

- 不要在移动端页面提供密度切换 UI，移动端固定使用 `spacious`
- 不要在固定展示的报表/大屏页面提供密度切换，直接用 `spacious`
- 不要在表单填写页提供密度切换，固定使用 `comfortable`
- 不要硬编码像素值而不使用 `DENSITY_CONFIG` 变量，否则密度切换失效

## 严格要求 (MUST DO)

- 每次生成自定义页面时都必须考虑密度，无需用户明确提及
- 列表/表格类页面必须默认提供密度切换 UI
- 在 `didMount` 中必须检测移动端并自动降级为 `spacious`
- 所有间距、字体、行高必须从 `DENSITY_CONFIG[density]` 读取，不得硬编码
- **本技能不读写 memory**：密度配置作为页面代码的一部分写入宜搭平台，不依赖跨会话的 memory 状态

## 写操作安全守卫

本技能生成的代码最终会写入宜搭平台，必须遵循以下安全机制：

| 操作阶段 | 安全守卫 | 说明 |
|---------|---------|------|
| **生成前** | 确认页面类型 | 必须先确认是列表/表格/单卡片/表单，再选择合适密度 |
| **生成中** | 代码预览 | 生成代码后必须先展示给用户预览，等待用户确认后再执行发布 |
| **发布前** | 用户确认 | 执行 `yida-publish-page` 前必须明确告知用户"即将发布到宜搭平台"，获得用户同意 |
| **发布后** | 结果验证 | 发布后必须提供页面访问链接，引导用户验证效果 |

**强制规则**：
- 禁止在用户未确认的情况下直接发布页面
- 禁止跳过代码预览步骤
- 生成的代码必须完整展示 `DENSITY_CONFIG` 配置和关键渲染逻辑，不得省略

## 触发条件

**正向触发**（生成自定义页面时自动应用，无需用户提及）：
- 生成任何列表/表格类自定义页面时
- 用户说"紧凑/密集/更多信息" → 使用 `compact` 模式
- 用户说"宽松/舒适/大字体" → 使用 `spacious` 模式

**不适用场景（不要触发）**：
- 单卡片、表单提交页 → 无需密度设计，固定使用 `comfortable`
- 原生报表页面 → 密度由报表组件自身控制
- 移动端页面 → 固定 `spacious`，不提供切换 UI

## 场景与密度选择

AI 在生成自定义页面时，**无需用户明确提及密度**，根据以下规则自动选择：

| 场景 | 推荐密度 | 典型示例 | 是否提供切换 UI |
| --- | --- | --- | --- |
| 数据量大、专业用户、需一屏展示更多信息 | **紧凑（compact）** | 运营后台、数据报表、审批列表 | ✅ 提供 |
| 常规业务场景、平衡信息量与可读性 | **舒适（comfortable）** | 任务管理、日常审批 | ✅ 提供 |
| 表单填写页 | **舒适（comfortable）** | 信息录入、申请提交 | ❌ 固定，不提供 |
| 重点突出、新手友好、强调视觉舒适度 | **宽松（spacious）** | 展示大屏、引导页 | ❌ 固定，不提供 |
| 移动端页面 | **宽松（spacious）** | 移动端所有页面 | ❌ 固定，不提供 |

**用户明确指定时**：
- 用户说"紧凑/密集/更多信息" → 使用 `compact`
- 用户说"宽松/舒适/大字体" → 使用 `spacious`
- 用户未指定 → 根据上表场景自动选择

## 响应式与切换 UI 规则

| 设备/场景 | 默认密度 | 是否提供切换 UI |
| --- | --- | --- |
| PC 端列表/表格页 | comfortable | ✅ 提供，让用户自选 |
| 移动端 | spacious | ❌ 不提供，固定 spacious |
| 大屏展示/报表 | spacious | ❌ 不提供，固定 spacious |
| 表单填写页 | comfortable | ❌ 不提供，固定 comfortable |

在 `didMount` 中自动检测移动端并降级：

```javascript
export function didMount() {
  if (this.utils.isMobile()) {
    _customState.density = 'spacious';
  }
}
```

## 异常与错误处理

| 场景 | 处理方式 |
|---------|----------|
| 密度切换后样式未更新 | 确认所有间距/字体从 `DENSITY_CONFIG[density]` 读取，不得硬编码像素值 |
| 移动端显示密度切换 UI | 在 `didMount` 中检测移动端并自动降级为 `spacious`，隐藏切换 UI |
| 报表/大屏页面提供密度切换 | 固定展示的报表/大屏不提供密度切换，直接使用 `spacious` |
| 密度配置缺失 | 使用 `comfortable` 模式作为默认值，不得报错中断 |
| 移动端检测失败 | 默认使用 `spacious` 模式，确保移动端可用性 |
| 样式变量未定义 | 停止执行，提示用户检查 `DENSITY_CONFIG` 配置是否完整 |
| 页面类型判断错误 | 询问用户确认页面类型（列表/表格/单卡片/表单），再选择合适密度 |
| 未知错误 | 停止执行，完整展示错误信息，建议用户反馈问题 |

---

## 三种密度的样式规范

```javascript
var DENSITY_CONFIG = {
  compact: {
    cardPadding: '8px 12px',
    cardMarginBottom: '8px',
    fontSize: '12px',
    lineHeight: '1.4',
    tableRowHeight: '32px',
    buttonHeight: '24px',
    buttonPadding: '0 8px',
    inputHeight: '24px',
    iconSize: '14px',
    sectionGap: '8px',
  },
  comfortable: {
    cardPadding: '16px 20px',
    cardMarginBottom: '16px',
    fontSize: '14px',
    lineHeight: '1.6',
    tableRowHeight: '48px',
    buttonHeight: '32px',
    buttonPadding: '0 16px',
    inputHeight: '32px',
    iconSize: '16px',
    sectionGap: '16px',
  },
  spacious: {
    cardPadding: '24px 28px',
    cardMarginBottom: '24px',
    fontSize: '16px',
    lineHeight: '1.8',
    tableRowHeight: '64px',
    buttonHeight: '40px',
    buttonPadding: '0 24px',
    inputHeight: '40px',
    iconSize: '20px',
    sectionGap: '24px',
  },
};
```

---

## 完整示例：带密度切换的页面

> 完整示例代码：执行 `openyida sample yida-density density-switch-page` 输出到 `.cache/samples/density-switch-page.js`，再用 `read_file` 读取。包含 `DENSITY_CONFIG`、状态管理、密度切换 UI、数据加载和渲染逻辑的完整实现。
