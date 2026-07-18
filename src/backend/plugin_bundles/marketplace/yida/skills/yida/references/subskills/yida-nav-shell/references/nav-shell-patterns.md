# B 端导航壳形态目录

当页面隐藏了应用导航（`isRenderNav=false`，见 [yida-page-uiux Step 0 导航形态判定](../../yida-page-uiux/workflow/step-0-nav-shape.md)），页面要**自带导航壳**接管应用级导航。本文件是 B 端常见导航形态的选型 + 骨架 + 代码示例目录。挑一种主形态，可与标签页叠加做二级导航。

> 这里给**方向 + 骨架 + 可直接改的代码示例**。示例以 native（`_customState` + inline style）为主，因为最自包含、两条链路都能读；每种形态附一句 **Canvas（antd）等价件**。落地时：native 交 `yida-custom-page`，Canvas 交 `yida-canvas-custom-page`。

## 选型速查

| 形态 | 何时用 | 顶级项数量 | 移动端收敛 |
|---|---|---|---|
| **左侧边栏** | 模块多、需常驻导航的后台/门户（最通用 B 端形态） | 5–12，支持分组/二级 | 抽屉（汉堡唤出） |
| **顶部导航** | 模块少、内容要占满宽度的看板/门户 | 2–6 | 汉堡菜单 |
| **顶部 + 侧边混合** | 两级结构复杂应用（顶部分域，侧边分子模块） | 顶 2–5 × 侧 3–10 | 顶部汉堡 + 侧边抽屉 |
| **浮动导航（悬浮胶囊/Dock）** | 沉浸/大屏/展示页，chrome 要极简，导航不常驻 | 3–6 | 底部胶囊/收起 |
| **标签页** | 一个模块内切同级视图（常叠加在上面几种之上） | 2–8 | 横向滚动标签 |

## 通用纪律（B 端 + 去 AI 味）

- **选中态要一眼可辨**：左侧边用「左侧 3px 主色条 + 浅色底 + 字重加粗」；顶部用「底部 2px 主色下划线 + 主色文字」。别只靠淡淡变色。
- **图标只作功能用途**：导航项用「功能性内联 SVG + 文字」，同页一套图标风格；**禁 emoji**、禁每项前配装饰图标（见 [yida-page-uiux 图标纪律](../../yida-page-uiux/workflow/step-5-icon-and-assets.md)）。
- **不做营销脸**：没有巨 Logo Hero、没有渐变横幅。顶部条左侧放「应用名/模块名 + 面包屑」，右侧放「用户/操作」，克制。
- **密度可偏高**：B 端导航允许信息密集，但要有主次；分组用小标题或分隔线，不要一长串平铺。
- **主色策略**：导航隐藏时主色相可自立（见 yida-page-uiux Step 0）；仍要么走品牌 `var(--color-brand1-*)`、要么用自定主色一以贯之，语义色固定。
- **先关原导航**：使用本导航壳时，宿主页发布后必须配置 `isRenderNav=false`，不要让宜搭原导航和自绘导航同时出现。
- **URL 参数不丢失**：跨页导航项要保存 `params` 并统一构造 URL；自定义页目标至少带 `isRenderNav=false`，需要跨组织或深链时合并 `corpid`、`tab`、`view` 等白名单参数。

## 多视图切换机制（导航壳的核心）

导航壳 = 一个自定义页内切多个视图。两条链路的做法：

- **native**：状态存 `_customState.activeView`，点击 `this.setCustomState({ activeView: key })`，`renderJsx` 里按 `activeView` 分支渲染内容区。
- **Canvas**：`var v = React.useState('home')`，或用 URL hash（`window.location.hash` + `hashchange` 监听，`useEffect` 注册并 cleanup）做可分享/可后退的视图切换。
- **跨页跳转**（跳到别的自定义页/表单）：用 [field-and-url-reference.md](../../../references/field-and-url-reference.md) 的模板拼 URL，目标自定义页必须带 `?isRenderNav=false` 保持沉浸；不要假设应用导航还在。不要只调用 `router.push(formUuid, {}, false)`，它无法表达完整的隐藏导航 URL 和业务参数。

### 跨页导航 URL 模板

```jsx
var BASE_URL = 'https://www.aliwork.com';
var APP_TYPE = 'APP_XXX';
var COMMON_NAV_PARAMS = { isRenderNav: 'false' };

var NAV = [
  { key: 'overview', label: '概览', type: 'custom', formUuid: 'FORM-OVERVIEW', params: { tab: 'overview' } },
  { key: 'orders', label: '订单', type: 'workbench', formUuid: 'FORM-ORDERS', params: { iframe: 'true' } }
];

export function buildQuery(params) {
  var pairs = [];
  Object.keys(params || {}).forEach(function(key) {
    if (params[key] !== undefined && params[key] !== null && params[key] !== '') {
      pairs.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(params[key])));
    }
  });
  return pairs.length ? '?' + pairs.join('&') : '';
}

export function buildNavUrl(item) {
  var path = item.type === 'workbench'
    ? '/' + APP_TYPE + '/workbench/' + item.formUuid
    : '/' + APP_TYPE + '/custom/' + item.formUuid;
  var params = item.type === 'workbench'
    ? Object.assign({}, item.params || {})
    : Object.assign({}, COMMON_NAV_PARAMS, item.params || {});
  return BASE_URL + path + this.buildQuery(params);
}

export function openNavItem(item) {
  window.open(this.buildNavUrl(item), '_top');
}
```

---

## 形态 1：左侧边栏（最通用）

```
┌──────┬─────────────────────────────┐
│ 应用名 │  顶部条：面包屑        用户   │
│ ─────│─────────────────────────────│
│▎概览  │                             │  ▎ = 选中项左侧主色条
│  订单 │        内容区（activeView）   │
│  客户 │                             │
│  报表 │                             │
│ ─────│                             │
│ ‹收起 │                             │
└──────┴─────────────────────────────┘
```

**何时用**：顶级模块 5–12，需要常驻导航。**Canvas 等价**：antd `Layout` + `Layout.Sider` + `Menu`（`mode="inline"`）。

```jsx
// native：_customState.activeView / collapsed；styles 见下，主色可用品牌变量或自定主色
var NAV = [
  { key: 'overview', label: '概览', icon: ICONS.dashboard },
  { key: 'orders',   label: '订单', icon: ICONS.list },
  { key: 'customers',label: '客户', icon: ICONS.user },
  { key: 'reports',  label: '报表', icon: ICONS.chart },
];

export function renderSidebar() {
  var self = this;
  var state = this.getCustomState();
  var collapsed = state.collapsed;
  var isMobile = this.utils.isMobile();

  return (
    <nav style={{
      width: collapsed ? 56 : 216,
      flexShrink: 0,
      height: '100%',
      background: '#fff',
      borderRight: '1px solid #e5e6eb',
      display: 'flex',
      flexDirection: 'column',
      transition: 'width .2s',
    }}>
      <div style={{ height: 52, display: 'flex', alignItems: 'center', padding: '0 16px', fontWeight: 700 }}>
        {collapsed ? '' : '进销存中台'}
      </div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {NAV.map(function (item) {
          var active = state.activeView === item.key;
          return (
            <div key={item.key}
              onClick={function () { self.setCustomState({ activeView: item.key }); }}
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                height: 40, padding: '0 16px', cursor: 'pointer',
                borderLeft: active ? '3px solid var(--color-brand1-6)' : '3px solid transparent',
                background: active ? 'var(--color-brand1-1)' : 'transparent',
                color: active ? 'var(--color-brand1-6)' : '#4e5969',
                fontWeight: active ? 600 : 400,
              }}>
              {self.renderIcon(item.icon, 18, active ? 'var(--color-brand1-6)' : '#86909c')}
              {collapsed ? null : <span>{item.label}</span>}
            </div>
          );
        })}
      </div>
      <div onClick={function () { self.setCustomState({ collapsed: !collapsed }); }}
        style={{ height: 44, display: 'flex', alignItems: 'center', padding: '0 16px', cursor: 'pointer', color: '#86909c', borderTop: '1px solid #f2f3f5' }}>
        {collapsed ? '›' : '‹ 收起'}
      </div>
    </nav>
  );
}
```

移动端：窄屏时侧边栏改为默认隐藏，顶部条放汉堡按钮，点开滑出为覆盖层抽屉（`position: fixed` + 半透明遮罩），选完自动收起。

## 形态 2：顶部导航（水平）

```
┌─────────────────────────────────────┐
│ 应用名   概览  订单  客户  报表    用户 │  选中项底部 2px 主色下划线
├─────────────────────────────────────┤
│            内容区（activeView）        │
└─────────────────────────────────────┘
```

**何时用**：顶级模块 2–6、内容要占满宽度（看板/门户）。**Canvas 等价**：antd `Menu mode="horizontal"` 或 `Tabs`。

```jsx
export function renderTopnav() {
  var self = this;
  var state = this.getCustomState();
  return (
    <header style={{
      height: 52, display: 'flex', alignItems: 'center',
      padding: '0 20px', gap: 4,
      background: '#fff', borderBottom: '1px solid #e5e6eb',
    }}>
      <span style={{ fontWeight: 700, marginRight: 24 }}>营销中台</span>
      {NAV.map(function (item) {
        var active = state.activeView === item.key;
        return (
          <div key={item.key}
            onClick={function () { self.setCustomState({ activeView: item.key }); }}
            style={{
              height: 52, display: 'flex', alignItems: 'center', padding: '0 14px', cursor: 'pointer',
              color: active ? 'var(--color-brand1-6)' : '#4e5969',
              fontWeight: active ? 600 : 400,
              borderBottom: active ? '2px solid var(--color-brand1-6)' : '2px solid transparent',
            }}>
            {item.label}
          </div>
        );
      })}
      <div style={{ marginLeft: 'auto', color: '#86909c' }}>管理员</div>
    </header>
  );
}
```

移动端：把中间菜单收进右侧汉堡下拉，只留应用名 + 汉堡。

## 形态 3：顶部 + 侧边混合

```
┌─────────────────────────────────────┐
│ 应用名   销售域 │ 采购域 │ 财务域    用户 │  顶部=一级域
├──────┬──────────────────────────────┤
│▎订单  │                             │  侧边=当前域的子模块
│  客户 │        内容区                 │
│  合同 │                             │
└──────┴──────────────────────────────┘
```

**何时用**：两级结构、模块多的复杂应用（顶部切「域」，侧边切该域「子模块」）。顶部选中切换时，侧边 `NAV` 换成该域的子项，`activeView` 重置到该域第一项。**Canvas 等价**：antd `Layout`（Header + Sider + Content）。

## 形态 4：浮动导航（悬浮胶囊 / Dock）

```
        ┌───────────────────────┐
        │      内容/大屏区        │
        │                       │
        │   ╭─────────────────╮ │  悬浮胶囊：固定底部居中，
        │   │ 概览 订单 客户 报表│ │  半透明底 + 模糊，不占布局
        │   ╰─────────────────╯ │
        └───────────────────────┘
```

**何时用**：沉浸/大屏/展示页，chrome 要极简、导航不常驻。**Canvas 等价**：自定义悬浮容器或 antd `FloatButton.Group`。

```jsx
export function renderFloatDock() {
  var self = this;
  var state = this.getCustomState();
  return (
    <div style={{
      position: 'fixed', left: '50%', bottom: 24, transform: 'translateX(-50%)',
      display: 'flex', gap: 4, padding: 6, zIndex: 50,
      background: 'rgba(255,255,255,.82)', backdropFilter: 'blur(12px)',
      border: '1px solid #e5e6eb', borderRadius: 999,
      boxShadow: '0 6px 24px rgba(0,0,0,.12)',
    }}>
      {NAV.map(function (item) {
        var active = state.activeView === item.key;
        return (
          <div key={item.key}
            onClick={function () { self.setCustomState({ activeView: item.key }); }}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              height: 36, padding: '0 14px', cursor: 'pointer', borderRadius: 999,
              background: active ? 'var(--color-brand1-6)' : 'transparent',
              color: active ? '#fff' : '#4e5969',
              fontWeight: active ? 600 : 400,
            }}>
            {item.label}
          </div>
        );
      })}
    </div>
  );
}
```

注意：浮动导航**叠在内容之上**（`position: fixed` + `zIndex`），内容区底部留出安全间距别被遮住。移动端收窄胶囊或改底部标签栏。

## 形态 5：标签页（叠加二级导航）

一个模块内切同级视图，通常叠在侧边/顶部之下。**Canvas 等价**：antd `Tabs`。

```jsx
export function renderTabs() {
  var self = this;
  var state = this.getCustomState();
  var TABS = [{ key: 'pending', label: '待处理' }, { key: 'done', label: '已完成' }];
  return (
    <div style={{ display: 'flex', gap: 24, borderBottom: '1px solid #e5e6eb', padding: '0 4px' }}>
      {TABS.map(function (t) {
        var active = state.tab === t.key;
        return (
          <div key={t.key}
            onClick={function () { self.setCustomState({ tab: t.key }); }}
            style={{
              height: 40, display: 'flex', alignItems: 'center', cursor: 'pointer',
              color: active ? 'var(--color-brand1-6)' : '#4e5969',
              fontWeight: active ? 600 : 400,
              borderBottom: active ? '2px solid var(--color-brand1-6)' : '2px solid transparent',
            }}>
            {t.label}
          </div>
        );
      })}
    </div>
  );
}
```

---

## 自查清单（导航壳）

- 选中态一眼可辨（主色条/下划线 + 字重），不是只靠淡变色。
- 导航项 = 功能性 SVG + 文字，同页一套图标风格，无 emoji、无每项装饰图标。
- 顶部/侧边有应用名或面包屑，用户知道「在哪、能去哪」，不是孤零零一个返回按钮。
- 内容区按 `activeView` 切换（native `_customState` / Canvas `useState`/hash），切换有状态、可回来。
- 宿主页已执行 `openyida update-form-config <appType> <formUuid> false "<标题>"`，宜搭原导航不再出现。
- 跨页跳转用 URL 模板拼；目标自定义页带 `?isRenderNav=false`，导航项的 `params` 没丢。
- 移动端：侧边→抽屉、顶部→汉堡、浮动→底部胶囊，走 `isMobile` 响应式。
- 浮动导航留出内容安全间距，不遮关键信息。
