
# yida-nav-shell — 页面内自绘导航壳

## 定位（先读这段）

自定义页**隐藏应用导航**（`isRenderNav=false`）后，宜搭框架的左侧/顶部导航消失，页面即整个视口。此时页面要**自己画一套导航**接管应用级导航——这就是「导航壳」。本技能是导航壳的**形态选型 + 骨架 + 代码示例**目录。

> **一句话**：`yida-page-uiux` 定视觉方向（Step 0 判定导航是否隐藏）→ 本技能选**导航壳形态**并给骨架 → `yida-custom-page`(native) / `yida-canvas-custom-page`(Canvas) 落地 JSX。

## 边界：和 yida-nav-group 划清（重要）

| 诉求 | 归谁 |
|---|---|
| 页面里用 JSX 画侧边/顶部/浮动导航，隐藏应用导航后自建导航 | **本技能 yida-nav-shell** |
| 管理宜搭应用**左侧菜单树 / 导航分组**：新建/重命名/删除分组、移动排序表单页面、隐藏导航项 | **yida-nav-group**（平台导航，CLI 操作 navUuid） |

用户说「加个导航」时先分清是哪一种：要**平台菜单项**→ nav-group；要**页面内导航组件**（尤其页面已隐藏应用导航）→ 本技能。

## 前置：先确认导航确实隐藏

导航壳的前提是应用导航已隐藏。是否隐藏、以及隐藏时的主色/视觉策略，由 `yida-page-uiux` 的 [Step 0 导航形态判定](../yida-page-uiux/workflow/step-0-nav-shape.md) 决定：

- 发布时 `openyida update-form-config <appType> <formUuid> false "<标题>"`（`isRenderNav=false`），或访问带 `?isRenderNav=false`。
- 导航**未**隐藏（默认）时无需自建导航壳，应用导航负责跨页跳转——此时一般不用本技能。

## 严格要求（导航壳必做）

1. **先隐藏宜搭原导航**：只要页面内自绘应用级导航壳，就必须在发布后执行 `openyida update-form-config <appType> <shellFormUuid> false "<页面标题>"`，或创建时使用能隐藏导航的模式；不能让宜搭原左侧导航和页面内导航同时出现。
2. **导航项必须携带 URL 参数**：导航配置不要只存 `formUuid`。每个跨页导航项必须能生成完整 URL，并合并公共参数（至少 `isRenderNav=false`；跨组织场景还要保留 `corpid`；业务深链保留 `tab`、`view`、`dateRange` 等参数）。
3. **跨自定义页默认走 `custom/{formUuid}?isRenderNav=false`**：隐藏导航壳跳到另一个自定义展示页时，优先拼 `{base_url}/{appType}/custom/{formUuid}?isRenderNav=false`。不要只用 `this.utils.router.push(formUuid, {}, false)`，它容易丢失 `isRenderNav=false`，导致目标页重新出现宜搭原导航。
4. **表单/数据管理入口分清打开方式**：需要在导航壳内容区展示表单列表时，用 iframe 地址 `workbench/{formUuid}?iframe=true`；必须整页跳转到表单管理时，也要通过统一 URL 构造函数合并当前业务参数，并明确接受目标页可能回到宜搭工作台框架。
5. **发布后验证最终 URL**：发布完成后至少打开一个带 `?isRenderNav=false` 的导航项 URL，确认目标页没有宜搭原导航，且 URL 参数没有被跳转逻辑吞掉。

## 选型速查

| 形态 | 何时用 | 顶级项数量 | 移动端收敛 |
|---|---|---|---|
| **左侧边栏** | 模块多、需常驻导航的后台/门户（最通用 B 端形态） | 5–12，支持分组/二级 | 抽屉（汉堡唤出） |
| **顶部导航** | 模块少、内容要占满宽度的看板/门户 | 2–6 | 汉堡菜单 |
| **顶部 + 侧边混合** | 两级结构复杂应用（顶部分域，侧边分子模块） | 顶 2–5 × 侧 3–10 | 顶部汉堡 + 侧边抽屉 |
| **浮动导航（悬浮胶囊/Dock）** | 沉浸/大屏/展示页，chrome 要极简，导航不常驻 | 3–6 | 底部胶囊/收起 |
| **标签页** | 一个模块内切同级视图（常叠加在上面几种之上） | 2–8 | 横向滚动标签 |

每种形态的 ASCII 骨架、选中态纪律、移动端收敛、native + Canvas 代码示例、自查清单见 [references/nav-shell-patterns.md](references/nav-shell-patterns.md)。

## 多视图切换机制（导航壳的核心）

导航壳 = 一个自定义页内切多个视图。两条链路：

- **native**：状态存 `_customState.activeView`，点击 `this.setCustomState({ activeView: key })`，`renderJsx` 里按 `activeView` 分支渲染内容区。
- **Canvas**：`React.useState`，或用 URL hash（`window.location.hash` + `hashchange`，`useEffect` 注册并 cleanup）做可分享/可后退的视图切换。
- **跨页跳转**：跳到别的自定义页/表单时，用 [field-and-url-reference.md](../../references/field-and-url-reference.md) 的模板拼 URL，并显式合并导航项参数。隐藏导航壳跳自定义页时，目标必须带 `?isRenderNav=false` 保持沉浸；别假设应用导航还在。

### URL 构造模板（native）

```jsx
var BASE_URL = 'https://www.aliwork.com';
var APP_TYPE = 'APP_XXX';
var COMMON_NAV_PARAMS = {
  isRenderNav: 'false'
};

var NAV_ITEMS = [
  { key: 'home', label: '首页', type: 'custom', formUuid: 'FORM-HOME', params: { tab: 'home' } },
  { key: 'dashboard', label: '经营看板', type: 'custom', formUuid: 'FORM-DASHBOARD', params: { tab: 'dashboard' } },
  { key: 'orders', label: '订单管理', type: 'workbench', formUuid: 'FORM-ORDERS', params: { iframe: 'true' } }
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

export function mergeParams(baseParams, extraParams) {
  return Object.assign({}, baseParams || {}, extraParams || {});
}

export function buildNavUrl(item) {
  var path = item.type === 'workbench'
    ? '/' + APP_TYPE + '/workbench/' + item.formUuid
    : '/' + APP_TYPE + '/custom/' + item.formUuid;
  var baseParams = item.type === 'workbench' ? {} : COMMON_NAV_PARAMS;
  return BASE_URL + path + this.buildQuery(this.mergeParams(baseParams, item.params));
}

export function openNavItem(item) {
  var url = this.buildNavUrl(item);
  window.open(url, '_top');
}
```

> 如果需要保留当前 URL 上的 `corpid`、`locale` 或业务参数，先从 `this.state.urlParams` 读出白名单字段，合并进 `COMMON_NAV_PARAMS`；不要把不明参数全量透传。

## 纪律（B 端 + 去 AI 味）

- **选中态一眼可辨**：侧边用「左侧 3px 主色条 + 浅色底 + 字重加粗」；顶部用「底部 2px 主色下划线 + 主色文字」，别只靠淡变色。
- **图标只作功能用途**：功能性内联 SVG + 文字，同页一套风格；**禁 emoji**、禁每项前配装饰图标（详见 [yida-page-uiux 图标纪律](../yida-page-uiux/workflow/step-5-icon-and-assets.md)）。
- **不做营销脸**：无巨 Logo Hero、无渐变横幅；顶部条放「应用名/模块名 + 面包屑 + 用户」，克制。
- **主色策略**：导航隐藏时主色相可自立，但要么走品牌 `var(--color-brand1-*)`、要么用自定主色一以贯之；语义色固定。

## 参考文档

| 文档 | 覆盖范围 | 何时阅读 |
|------|---------|---------|
| [导航壳形态目录](references/nav-shell-patterns.md) | 五形态 ASCII 骨架 + 选中态 + 移动端 + native/Canvas 代码 + 多视图切换 + 自查清单 | 选定形态、要骨架/代码时 |
| [yida-page-uiux Step 0](../yida-page-uiux/workflow/step-0-nav-shape.md) | 导航是否隐藏的判定法 + 隐藏时主色/视觉策略分叉 | 动手前确认导航形态时 |
| [字段与 URL 参考](../../references/field-and-url-reference.md) | 隐藏导航 `isRenderNav=false`、跨页跳转 URL 模板 | 拼跨页跳转 URL 时 |
