# ECharts 页面代码模板

> 本文档包含 ECharts 自定义页面的必备代码结构、数据请求函数模板、初始化流程等。
> 由 `yida-chart` 主技能拆分而来，编写 ECharts 页面代码时必须读取本文档。

---

## 必备函数清单

| 函数 | 类型 | 作用 | 缺少后果 |
|------|------|------|----------|
| `_customState` | `var` 变量 | 存储所有业务状态（loading、数据等） | 无法管理页面状态 |
| `getCustomState` | `export function` | 获取状态 | 编译警告 |
| `setCustomState` | `export function` | 设置状态并触发重渲染 | `setCustomState is not a function` |
| `forceUpdate` | `export function` | 通过 `this.setState({ timestamp })` 触发 React 重渲染 | `forceUpdate is not a function` |
| `didMount` | `export function` | 页面加载完成时初始化 | 页面无法初始化 |
| `didUnmount` | `export function` | 页面卸载时清理资源 | 内存泄漏 |
| `renderJsx` | `export function` | 页面渲染入口 | 页面空白 |

## 必备代码模板

```javascript
// ── 状态管理（必须包含） ──────────────────────────────

var _customState = {
  loading: true,
  // ... 其他业务状态
};

export function getCustomState(key) {
  if (key) return _customState[key];
  return _.clone(_customState);
}

export function setCustomState(newState) {
  _.assign(_customState, newState);
  this.forceUpdate();
}

export function forceUpdate() {
  this.setState({ timestamp: new Date().getTime() });
}
```

## renderJsx 的 timestamp 隐藏 div（必须包含）

> ⚠️ **`renderJsx` 的每个 `return` 分支都必须包含 `<div style={{ display: 'none' }}>{this.state.timestamp}</div>`**，否则 `forceUpdate` 调用 `this.setState({ timestamp })` 后，React 无法检测到输出变化，页面将无法更新。

```javascript
export function renderJsx() {
  if (_customState.loading) {
    return (
      <div>
        <div style={{ display: 'none' }}>{this.state.timestamp}</div>
        <div>加载中...</div>
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>
      {/* 页面内容 */}
    </div>
  );
}
```

## CDN 地址规则（必须遵守）

| 规则 | 说明 |
|------|------|
| **必须使用阿里 CDN** | `https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js` |
| **禁止使用 cdnjs.cloudflare.com** | 宜搭环境（aliwork.com）对 cloudflare CDN 有安全策略限制，会加载失败 |
| **必须锁定版本 5.6.0** | 禁止使用 `latest` 或其他未验证版本 |

> 💡 **lodash 无需加载**：宜搭页面运行时已全局加载 lodash 4.6.1（`window._`），可直接使用 `_.groupBy`、`_.sortBy`、`_.get`、`_.cloneDeep` 等处理图表数据，无需 `loadScript`。

## 函数声明规则

| 场景 | 正确写法 | 错误写法 | 原因 |
|------|---------|---------|------|
| 需要 `this` 的组件方法 | `export function loadAllData() {}` | `var loadAllData = function() {}` | 需要宜搭运行时绑定 `this` |
| 不需要 `this` 的纯工具函数 | `var _fetchData = function() {}` | `export function fetchData() {}` | `export function` 不在白名单中会被 UglifyJS 消除 |
| 模块级常量/变量 | `var APP_TYPE = 'xxx'` | `const APP_TYPE = 'xxx'` | 兼容性 |

## prdId（topicId）动态获取（必须遵守）

> ⚠️ **`prdId` 不能硬编码**，必须在运行时通过 `getFormNavigationListByOrder` 接口动态获取。

```javascript
var _prdId = null;

var _fetchPrdId = function () {
  var appType = window.pageConfig && window.pageConfig.appType;
  var csrfToken = window.g_config && window.g_config._csrf_token;
  var baseUrl = window.location.origin;
  var url = baseUrl + '/dingtalk/web/' + appType
    + '/query/formnav/getFormNavigationListByOrder.json'
    + '?_api=Nav.queryList&_mock=false&_csrf_token=' + encodeURIComponent(csrfToken);

  return fetch(url, {
    method: 'GET',
    credentials: 'include',
    headers: { 'accept': 'application/json', 'x-requested-with': 'XMLHttpRequest' },
  })
    .then(function (resp) { return resp.json(); })
    .then(function (res) {
      if (res.success && Array.isArray(res.content)) {
        var targetNav = res.content.find(function (item) { return item.formUuid === REPORT_FORM_UUID; });
        if (targetNav && targetNav.topicId) {
          _prdId = targetNav.topicId;
          return _prdId;
        }
        var reportNav = res.content.find(function (item) { return item.formType === 'report' && item.topicId; });
        if (reportNav) {
          _prdId = reportNav.topicId;
          return _prdId;
        }
        throw new Error('未找到报表的 topicId');
      }
      throw new Error(res.errorMsg || '获取导航菜单失败');
    });
};
```

## 报表数据请求函数模板（必须用 var 声明）

> ⚠️ **必须严格按照以下模板编写**，缺少任何参数或 header 都可能导致接口返回空数据或 403 错误。

```javascript
var _fetchReportData = function (component, filterValueMap) {
  var appType = window.pageConfig && window.pageConfig.appType;
  var csrfToken = window.g_config && window.g_config._csrf_token;
  var baseUrl = window.location.origin;

  var queryContext = {
    aliasList: [],
    filterValueMap: filterValueMap || {},
    dim2table: true,
    orderByList: [],
    needTotalCount: false,
    variableParams: {},
    paging: { start: 0, limit: 100 },
  };

  var params = {
    timezone: 'GMT+8',
    _tb_token_: csrfToken, _csrf_token: csrfToken, _csrf: csrfToken,
    prdId: _prdId,
    pageId: REPORT_FORM_UUID,
    pageName: 'report',
    cid: component.cid,
    cname: component.cname || '',
    componentClassName: component.className,
    queryContext: JSON.stringify(queryContext),
    dataSetKey: component.dataSetKey,
    enabledCache: 'true',
    queryTimestamp: String(new Date().getTime()),
    appendTraceId: 'true',
  };

  var apiUrl = baseUrl + '/alibaba/web/' + appType
    + '/visual/visualizationDataRpc/getDataAsync.json'
    + '?_api=EDataService.getDataAsync&_mock=false&_stamp=' + new Date().getTime();

  return fetch(apiUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'accept': 'application/json, text/json',
      'x-requested-with': 'XMLHttpRequest',
    },
    credentials: 'include',
    body: Object.keys(params).map(function (key) {
      return encodeURIComponent(key) + '=' + encodeURIComponent(params[key]);
    }).join('&'),
  })
    .then(function (r) { return r.json(); })
    .then(function (result) {
      if (result.success) return result.content;
      throw new Error(result.errorMsg || '报表数据获取失败');
    });
};
```

### 请求参数对照表

| 参数 | 是否必须 | 说明 |
|------|---------|------|
| **URL `_api`** | ✅ | 固定为 `EDataService.getDataAsync` |
| **URL `_mock`** | ✅ | 固定为 `false` |
| **URL `_stamp`** | ✅ | 当前时间戳，防缓存 |
| **Header `accept`** | ✅ | `application/json, text/json` |
| **Header `x-requested-with`** | ✅ | `XMLHttpRequest` |
| **Body `enabledCache`** | ✅ | `'true'` |
| **Body `queryTimestamp`** | ✅ | 当前时间戳字符串 |
| **Body `appendTraceId`** | ✅ | `'true'` |
| **Body `queryContext.aliasList`** | ✅ | 空数组 `[]` |
| **Body `queryContext.orderByList`** | ✅ | 空数组 `[]` |
| **Body `queryContext.needTotalCount`** | ✅ | `false` |
| **Body `queryContext.variableParams`** | ✅ | 空对象 `{}` |
| **Body `queryContext.paging`** | ✅ | `{ start: 0, limit: 100 }` |

## didMount 初始化流程模板

```javascript
export function didMount() {
  var self = this;
  var loadEcharts = new Promise(function (resolve, reject) {
    if (window.echarts) { resolve(); return; }
    var script = document.createElement('script');
    script.src = ECHARTS_CDN;
    script.onload = function () { resolve(); };
    script.onerror = function () { reject(new Error('ECharts 加载失败')); };
    document.head.appendChild(script);
  });

  Promise.all([loadEcharts, _fetchPrdId()])
    .then(function () { return self.loadAllData(); })
    .catch(function (err) {
      console.error('[看板] 初始化失败:', err);
      self.utils.toast({ title: '初始化失败: ' + err.message, type: 'error' });
      _customState.loading = false;
      self.forceUpdate();
    });

  window.addEventListener('resize', function () {
    // 调用所有 chart 实例的 resize()
  });
}
```

## 报表接口返回数据的 meta 字段解析规则

> ⚠️ **`getDataAsync.json` 返回的 `content.meta` 数组有固定的顺序约定**，必须按以下规则解析，禁止猜测。

### meta 数组顺序约定

| 索引 | 含义 | 说明 |
|------|------|------|
| `meta[0]` | **维度字段**（dimension） | 分组依据，如行业、来源、日期等 |
| `meta[1]` | **度量字段**（measure） | 聚合值，如计数、求和等 |

> 对于**指标卡**（`YoushuSimpleIndicatorCard`），`meta` 只有一个元素，即度量字段。

### 标准解析模板

```javascript
// 饼图/柱状图/折线图的数据解析（维度 + 度量）
var data = content.data || [];
var meta = content.meta || [];
var dimAlias = meta[0] && meta[0].alias;
var measureAlias = meta[1] && meta[1].alias;

var chartData = data.map(function (row) {
  return {
    name: row[dimAlias] || '未知',
    value: Number(row[measureAlias]) || 0,
  };
});

// 指标卡的数据解析（仅度量）
var indicatorMeta = content.meta || [];
var measureField = indicatorMeta[0] && indicatorMeta[0].alias;
var totalValue = content.data[0] && measureField ? Number(content.data[0][measureField]) || 0 : 0;
```

> ⚠️ **禁止**使用 `meta.find(m => m.alias.indexOf('field_') === 0)` 来区分维度和度量，因为维度和度量的 alias 都以 `field_` 开头，这种写法不具备可靠性。

## ⚠️ 图表渲染时序（必须遵守）

> 这是一个**极易出错的陷阱**：异步加载数据后立即调用 `renderXxxChart()` 初始化 ECharts 图表，但此时 DOM 容器可能还不存在。

### 问题场景

```
1. didMount → 设置 loading=true → 页面渲染 loading 状态（无图表容器 DOM）
2. 异步请求数据 → 数据返回 → 调用 renderPieChart()
   → document.getElementById('pie-chart') 返回 null ❌
3. 所有数据加载完成 → loading=false → forceUpdate() → 图表容器 DOM 出现
   → 但 renderPieChart() 已经调用过了，不会再调用 ❌
```

### 正确做法：统一延迟渲染

```javascript
var checkDone = function () {
  loadCount++;
  if (loadCount >= totalLoads) {
    _customState.loading = false;
    self.forceUpdate();
    // ⚠️ 必须延迟，等待 React 将图表容器渲染到 DOM 后再初始化 ECharts
    setTimeout(function () {
      self.renderPieChart();
      self.renderBarChart();
      self.renderLineChart();
    }, 100);
  }
};
```

## 图表初始化与销毁

```javascript
export function createChart(domId) {
  var container = document.getElementById(domId);
  if (!container) { console.warn('图表容器不存在: ' + domId); return null; }
  var existingInstance = window.echarts.getInstanceByDom(container);
  if (existingInstance) { existingInstance.dispose(); }
  var chart = window.echarts.init(container);
  var rawSetOption = chart.setOption.bind(chart);
  var firstOptionApplied = false;
  chart.setOption = function(option, notMerge) {
    rawSetOption(option, notMerge);
    if (!firstOptionApplied) {
      firstOptionApplied = true;
      setTimeout(function() {
        if (window.echarts.getInstanceByDom(container) === chart) {
          chart.resize();
          rawSetOption(option, true);
        }
      }, 120);
    }
  };
  return chart;
}

export function didUnmount() {
  var chartIds = this.getCustomState('chartIds') || [];
  chartIds.forEach(function(domId) {
    var container = document.getElementById(domId);
    if (container) {
      var instance = window.echarts.getInstanceByDom(container);
      if (instance) { instance.dispose(); }
    }
  });
  if (this._resizeHandler) { window.removeEventListener('resize', this._resizeHandler); }
}
```

## 窗口 resize 自适应

```javascript
export function bindChartResize() {
  this._resizeHandler = function() {
    var chartIds = this.getCustomState('chartIds') || [];
    chartIds.forEach(function(domId) {
      var container = document.getElementById(domId);
      if (container) {
        var instance = window.echarts.getInstanceByDom(container);
        if (instance) { instance.resize(); }
      }
    });
  }.bind(this);
  window.addEventListener('resize', this._resizeHandler);
}
```

## 图表 setOption 原地更新（禁止 dispose 重建）

筛选刷新时，**禁止 `dispose()` 后重新 `init()`**，这会导致图表闪烁。应使用 `setOption(option, true)` 原地更新。首次初始化必须使用上方 `createChart()`，它会在首个 `setOption` 后延迟执行 `resize()` + `setOption(option, true)`，规避宜搭 CSS layout 尚未稳定导致 series 不绘制的问题：

```javascript
export function renderPieChart() {
  var container = document.getElementById('pie-chart');
  if (!container || !window.echarts) return;
  var option = { /* ... 构建 option ... */ };
  if (_chartInstances.pie) {
    _chartInstances.pie.setOption(option, true);
  } else {
    _chartInstances.pie = this.createChart('pie-chart');
    if (_chartInstances.pie) {
      _chartInstances.pie.setOption(option, true);
    }
  }
}
```

> ⚠️ `label.formatter` 返回 rich text 模板在宜搭自定义页面环境不稳定，`check-page` 会给出警告。优先使用普通 formatter 字符串，或预先在数据中生成标签文本。

## 筛选触发 → 局部刷新（最佳实践）

| 场景 | 加载方式 | 说明 |
|------|---------|------|
| **首次加载** | `loadAllData`（全屏 loading） | 页面无数据，需要全屏等待 |
| **筛选刷新** | `refreshAllData`（局部刷新） | 页面已有数据，各区域独立更新 |

```javascript
export function applyFilters() {
  _customState.refreshing = true;
  this.forceUpdate();
  this.refreshAllData();
}

export function refreshAllData() {
  var self = this;
  var pendingCount = 4;
  var checkDone = function() {
    pendingCount--;
    if (pendingCount <= 0) {
      _customState.refreshing = false;
      self.forceUpdate();
    }
  };
  // 各区域独立加载、独立更新...
}
```

## 数据明细表格

报表中**必须包含数据明细表格**，使用 `searchFormDatas` 获取（返回 `formInstId`，支持详情跳转）。

### 表单详情页跳转

```javascript
export function getDetailUrl(formInstId) {
  var appType = window.pageConfig && window.pageConfig.appType;
  if (!appType || !formInstId) return '';
  return 'https://www.aliwork.com/' + appType + '/formDetail/' + FORM_UUID + '?formInstId=' + formInstId;
}
```

### 日期格式化

```javascript
export function formatDate(timestamp) {
  if (!timestamp) return '-';
  var date = new Date(Number(timestamp));
  if (isNaN(date.getTime())) return '-';
  var year = date.getFullYear();
  var month = date.getMonth() + 1;
  var day = date.getDate();
  return year + '-' + (month < 10 ? '0' + month : '' + month) + '-' + (day < 10 ? '0' + day : '' + day);
}
```

### 日期筛选格式兼容

```javascript
// ❌ 错误：直接传日期字符串
searchCondition[FIELD.planDate] = JSON.stringify(['2025-03-01', '2025-03-31']);

// ✅ 正确：转换为毫秒时间戳
var startTimestamp = new Date('2025-03-01T00:00:00').getTime();
var endTimestamp = new Date('2025-03-31T23:59:59').getTime();
searchCondition[FIELD.planDate] = JSON.stringify([startTimestamp, endTimestamp]);
```
