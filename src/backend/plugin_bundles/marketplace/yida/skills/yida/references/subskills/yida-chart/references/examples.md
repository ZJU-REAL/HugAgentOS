# yida-chart 使用示例

## 示例 1：基于已有报表 URL 创建 ECharts 可视化大屏

### 输入

用户提供已有宜搭原生报表 URL：
```
https://www.aliwork.com/APP_KNILKT41DC5XXR5D4QEC/admin/REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5
```

### 执行步骤

```bash
# Step 1：检测环境和登录态
openyida env

# Step 2：获取报表 Schema；需要文件时用结构化文件写入工具保存 stdout
openyida get-schema APP_KNILKT41DC5XXR5D4QEC REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5 --json

# Step 3：创建自定义展示页面
openyida create-page APP_KNILKT41DC5XXR5D4QEC "任务数据看板" --mode dashboard

# Step 4：编写 ECharts 页面代码（参考 yida-custom-page 规范）
# 输出到 project/pages/src/task-dashboard.js

# Step 5：发布页面
openyida publish project/pages/src/task-dashboard.js APP_KNILKT41DC5XXR5D4QEC FORM-XXXXXXXX --health-check
```

### 输出

```json
{
  "success": true,
  "pageUrl": "https://www.aliwork.com/APP_KNILKT41DC5XXR5D4QEC/custom/FORM-XXXXXXXX",
  "formUuid": "FORM-XXXXXXXX"
}
```

---

## 示例 2：ECharts 页面核心代码结构

### 输入

用户要求：基于报表数据创建包含柱状图 + 折线图 + 数据明细表的看板页面。

### 关键代码片段

```javascript
// 模块级变量（不能用 export function，否则被编译器消除）
var _prdId = null;
var _customState = { loading: true, timestamp: 0 };

// 动态获取 prdId（必须从 getFormNavigationListByOrder 接口获取，禁止硬编码）
var _fetchPrdId = function() {
  var appType = window.pageConfig && window.pageConfig.appType;
  var csrfToken = window.g_config && window.g_config._csrf_token;
  var url = window.location.origin + '/dingtalk/web/' + appType
    + '/query/formnav/getFormNavigationListByOrder.json'
    + '?_api=Nav.queryList&_mock=false&_csrf_token=' + encodeURIComponent(csrfToken);
  return fetch(url, { method: 'GET', credentials: 'include' })
    .then(function(resp) { return resp.json(); })
    .then(function(res) {
      if (res.success && Array.isArray(res.content)) {
        var reportNav = res.content.find(function(item) {
          return item.formUuid === 'REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5';
        });
        if (reportNav && reportNav.topicId) {
          _prdId = reportNav.topicId;
          return _prdId;
        }
      }
      throw new Error('未找到报表 topicId');
    });
};

// 获取报表聚合数据
var _fetchReportData = function(cid, componentClassName, dataSetKey, filterValueMap) {
  var appType = window.pageConfig && window.pageConfig.appType;
  var csrfToken = window.g_config && window.g_config._csrf_token;
  var body = new URLSearchParams({
    _csrf_token: csrfToken,
    prdId: _prdId,
    pageId: 'REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5',
    pageName: 'report',
    cid: cid,
    componentClassName: componentClassName,
    queryContext: JSON.stringify({ filterValueMap: filterValueMap || {}, dim2table: true }),
    dataSetKey: dataSetKey,
  });
  return fetch('/alibaba/web/' + appType + '/visual/visualizationDataRpc/getDataAsync.json', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
    credentials: 'include',
  }).then(function(r) { return r.json(); })
    .then(function(result) {
      if (result.success) return result.content;
      throw new Error(result.errorMsg);
    });
};

// 生命周期：页面加载后初始化
export function didMount() {
  var self = this;
  _fetchPrdId().then(function() {
    return _fetchReportData('YoushuBar_abc123', 'YoushuBar', 'chartData', {});
  }).then(function(content) {
    var data = content.data || [];
    var meta = content.meta || [];
    _customState.chartData = data;
    _customState.loading = false;
    self.forceUpdate();
    // 延迟初始化 ECharts（等待 DOM 更新）
    setTimeout(function() { self.initBarChart(); }, 100);
  }).catch(function(err) {
    self.utils.toast({ title: err.message, type: 'error' });
    _customState.loading = false;
    self.forceUpdate();
  });
}

// 初始化柱状图
export function initBarChart() {
  var container = document.getElementById('bar-chart-container');
  if (!container || typeof echarts === 'undefined') return;
  var chart = echarts.init(container);
  var data = _customState.chartData || [];
  var option = {
    xAxis: { type: 'category', data: data.map(function(d) { return d.dim; }) },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: data.map(function(d) { return d.measure; }) }],
  };
  chart.setOption(option, true);
  setTimeout(function() {
    chart.resize();
    chart.setOption(option, true);
  }, 120);
  window.addEventListener('resize', function() { chart.resize(); });
}

// 渲染函数
export function renderJsx() {
  var isMobile = this.utils.isMobile();
  return (
    <div style={{ padding: '16px', background: '#f5f5f5', minHeight: '100vh' }}>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>
      {_customState.loading ? (
        <div style={{ textAlign: 'center', padding: '40px' }}>加载中...</div>
      ) : (
        <div id="bar-chart-container" style={{ width: '100%', height: '400px', background: '#fff', borderRadius: '8px' }} />
      )}
    </div>
  );
}

export function getCustomState(key) { if (key) return _customState[key]; return _.clone(_customState); }
export function setCustomState(newState) { _.assign(_customState, newState); this.forceUpdate(); }
export function forceUpdate() { this.setState({ timestamp: new Date().getTime() }); }
```

### 注意事项

- `prdId` 必须通过 `getFormNavigationListByOrder` 动态获取，**禁止硬编码**
- `pageName` 固定为 `'report'`，不能用 `'custom'`
- ECharts 必须使用阿里 CDN：`https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js`
- 数据请求函数必须用 `var` 声明为模块级变量，不能用 `export function`
