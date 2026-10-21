# Code Canvas 编写示例 / 脚手架

本文件是从零写 Code Canvas 页面的 vetted 模板集。所有示例都遵守运行时事实：`YidaComp` 是普通 React18 函数组件，必须**导出或返回** `YidaComp` / `YidaComp.default`；只 `import` 依赖白名单内的包；副作用在 `useEffect` 里注册并返回 cleanup。

> 白名单、windowAlias、CDN 与 antd/dayjs 陷阱见 [dependencies-and-cdn.md](dependencies-and-cdn.md)；读写宜搭数据见 [data-bridge-guide.md](data-bridge-guide.md)。

## 1. 最小可运行组件（入口 + 本地状态）

最小骨架：明确入口导出、用 `useState` 管本地状态，不写任何普通页面契约（无 `renderJsx` / `didMount` / `this.*`）。

```jsx
import React, { useState } from 'react';

function YidaComp(props) {
  var s = React.useState(0);
  var count = s[0];
  var setCount = s[1];

  return (
    <div style={{ padding: 16 }}>
      <p>当前计数：{count}</p>
      <button onClick={function () { setCount(count + 1); }}>加一</button>
    </div>
  );
}

export default YidaComp;
```

## 2. 带副作用清理（定时器 / 事件监听）

`useEffect` 注册的定时器、事件、图表实例，**必须**在返回的 cleanup 里销毁，否则重渲染 / 卸载会泄漏。

```jsx
import React, { useEffect, useState } from 'react';

function YidaComp(props) {
  var t = React.useState(0);
  var seconds = t[0];
  var setSeconds = t[1];

  React.useEffect(function () {
    var timer = setInterval(function () {
      setSeconds(function (prev) { return prev + 1; });
    }, 1000);
    function onResize() { /* 响应窗口变化 */ }
    window.addEventListener('resize', onResize);

    return function cleanup() {
      clearInterval(timer);
      window.removeEventListener('resize', onResize);
    };
  }, []);

  return <div style={{ padding: 16 }}>已运行 {seconds}s</div>;
}

export default YidaComp;
```

## 3. 可视化：recharts 图表

`recharts` 在白名单内（windowAlias `Recharts`）。标准 `import` 即可，CLI 本地编译会把它计入 `importedModules`。图表容器给定高度，避免 0 高度不渲染。图表颜色是 JS 传给库的字符串，**不能硬编码蓝**——用 `readBrandColor` 读平台品牌色，让线条跟随 App 主题（见 [canvas-design-system.md](canvas-design-system.md)）。

```jsx
import React from 'react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';

// 读平台品牌色（跑在真 window，getComputedStyle 可直接解析），缺失时退默认蓝
function readBrandColor(level, fallback) {
  try {
    var v = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-brand1-' + (level || 6)).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

function YidaComp(props) {
  var brand = readBrandColor(6, '#1677ff');
  var data = [
    { name: '1月', value: 120 },
    { name: '2月', value: 200 },
    { name: '3月', value: 150 },
    { name: '4月', value: 320 },
  ];

  return (
    <div style={{ width: '100%', height: 300, padding: 16 }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <XAxis dataKey="name" />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="value" stroke={brand} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default YidaComp;
```

## 4. 数据拉取组件（接数据桥）

结合 [data-bridge-guide.md](data-bridge-guide.md) 的 `useYidaFetch`：同源 `fetch` + `credentials: 'include'` + AbortController 清理。此处只演示消费侧结构。

```jsx
import React, { useEffect, useRef, useState } from 'react';

function YidaComp(props) {
  var st = React.useState({ loading: true, rows: [], error: null });
  var state = st[0];
  var setState = st[1];
  var abortRef = React.useRef(null);

  React.useEffect(function () {
    var controller = new AbortController();
    abortRef.current = controller;

    fetch('/your-connector-proxy/searchFormDatas', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ appType: props.appType, formUuid: props.formUuid, pageSize: 20 }),
      signal: controller.signal,
    })
      .then(function (r) { if (!r.ok) { throw new Error('HTTP ' + r.status); } return r.json(); })
      .then(function (json) {
        var rows = (json && json.result && json.result.data) || [];
        setState({ loading: false, rows: rows, error: null });
      })
      .catch(function (e) {
        if (e.name === 'AbortError') { return; }
        setState({ loading: false, rows: [], error: e.message });
      });

    return function () { controller.abort(); };
  }, [props.appType, props.formUuid]);

  if (state.loading) { return <div>加载中…</div>; }
  if (state.error) { return <div style={{ color: 'red' }}>加载失败：{state.error}</div>; }

  return (
    <ul style={{ padding: 16 }}>
      {state.rows.map(function (row) {
        return <li key={row.formInstanceId}>{row.title}</li>;
      })}
    </ul>
  );
}

export default YidaComp;
```

## 通用自查清单

- 入口：源码有 `export default YidaComp`（或返回组件函数），不是只定义局部组件。
- 依赖：所有 `import` 都在白名单内，能出现在编译结果 `dependencies` 里。
- 副作用：每个 `useEffect` 的定时器 / 监听 / 图表实例都有 cleanup。
- 数据：读写走同源 `fetch` + `credentials: 'include'`，无硬编码 Cookie / CSRF / appSecret。
- 主色：antd 走 `ConfigProvider.colorPrimary`、Tailwind 走 `var(--color-brand1-*)`、图表走 `readBrandColor`，无散落的 `#1677ff` / `bg-blue-500`（见 [canvas-design-system.md](canvas-design-system.md)）。
- 原生字段组件（`EmployeeField` 等）：先按 [employeefield-verification.md](employeefield-verification.md) 最小验证，缺证据就降级。
