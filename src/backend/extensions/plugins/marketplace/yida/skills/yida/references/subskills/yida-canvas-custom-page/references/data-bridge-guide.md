# Code Canvas 数据桥指南（自写 HTTP 读写宜搭数据）

Code Canvas 运行时**没有** `this.utils.yida.*` / `dataSourceMap` / `this.$(fieldId)` 实例数据桥（核实自 `factory.tsx`：物料只透传 `code / runtimeCode / importedModules / pageType`，`YidaComp` 是普通函数组件，wrapper 只注入 `window`）。因此 `YidaComp` 要读写宜搭数据，只能**自己补一座 HTTP 桥**。本文件给出干净、可复用、合规的写法。

## 三条数据路径，先选对

| 路径 | 是否可在浏览器（Canvas）直接调 | 说明 |
| --- | --- | --- |
| 宜搭开放 API（OpenAPI，`appKey`/`appSecret` 签名） | **不可** | 需服务端签名；在浏览器里必然泄露 secret。只能由后端 / 连接器代理调，Canvas 不直连。 |
| 平台已配置**连接器**（HTTP 连接器暴露的同源代理端点） | **推荐** | 同源 `fetch(url, { credentials: 'include' })` 带 cookie 即可，鉴权与密钥留在平台侧，符合数据源治理。 |
| 内部表单数据端点（同源、依赖登录 cookie + CSRF） | 可，但要谨慎 | 与 native `this.utils.yida.searchFormDatas` 命中的是同类端点；需自行带 CSRF token，端点随环境可能变化，不要硬编码跨域绝对地址。 |

选路原则：**优先走连接器代理**（编码规则 #6「不要绕过数据源治理」）。真需要直连内部端点时，只用**同源相对路径** + `credentials: 'include'`，绝不在源码里硬编码 Cookie / CSRF / appSecret。

## 可复用读数据 Hook

```jsx
import React, { useCallback, useEffect, useRef, useState } from 'react';

// 通用：同源、带 cookie、可取消、带 loading/error 的 fetch hook
function useYidaFetch(buildRequest, deps) {
  var stateHook = React.useState({ loading: false, data: null, error: null });
  var state = stateHook[0];
  var setState = stateHook[1];
  var abortRef = React.useRef(null);

  var run = React.useCallback(function () {
    if (abortRef.current) { abortRef.current.abort(); }
    var controller = new AbortController();
    abortRef.current = controller;
    setState({ loading: true, data: null, error: null });

    var req = buildRequest(); // { url, method, body }
    return fetch(req.url, {
      method: req.method || 'GET',
      credentials: 'include',          // 同源带登录 cookie，不手动塞 Cookie 头
      headers: { 'Content-Type': 'application/json' },
      body: req.body ? JSON.stringify(req.body) : undefined,
      signal: controller.signal,
    })
      .then(function (resp) {
        if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
        return resp.json();
      })
      .then(function (json) {
        // 宜搭返回体通常形如 { success, result / data, errorMsg }，按真实结构解析
        if (json && json.success === false) {
          throw new Error(json.errorMsg || 'request failed');
        }
        setState({ loading: false, data: json.result || json.data || json, error: null });
      })
      .catch(function (err) {
        if (err.name === 'AbortError') { return; }
        setState({ loading: false, data: null, error: err.message });
      });
  }, deps || []);

  React.useEffect(function () {
    run();
    return function () { if (abortRef.current) { abortRef.current.abort(); } };
  }, deps || []);

  return { loading: state.loading, data: state.data, error: state.error, refetch: run };
}
```

要点：

- `credentials: 'include'` 让浏览器带上同源登录态，**不要**手动拼 `Cookie` / `x-csrf-token` 硬编码值；如需 CSRF，从页面已有上下文动态读取，别写死。
- 用 `AbortController` 在卸载 / 依赖变化时取消，避免 setState-after-unmount（对应编码规则 #5 副作用清理）。
- 解析响应按**真实返回结构**处理（宜搭常见 `{ success, result, errorMsg }`），不要假设字段名。

## 在组件里用

```jsx
function YidaComp(props) {
  var appType = props.appType || '<APP_TYPE>';        // 来自 props 或页面约定
  var formUuid = props.formUuid || '<FORM_UUID>';

  var q = useYidaFetch(function () {
    return {
      url: '/your-connector-proxy/searchFormDatas',    // 连接器同源代理端点（示意）
      method: 'POST',
      body: { appType: appType, formUuid: formUuid, pageSize: 20, pageNumber: 1 },
    };
  }, [appType, formUuid]);

  if (q.loading) { return <div>加载中…</div>; }
  if (q.error) { return <div style={{ color: 'red' }}>加载失败：{q.error}</div>; }

  var rows = (q.data && q.data.data) || [];
  return (
    <ul>
      {rows.map(function (row) { return <li key={row.formInstanceId}>{row.title}</li>; })}
    </ul>
  );
}

export default YidaComp;
```

`url`、`body` 字段按你实际接的连接器 / 端点契约填；上面是结构示意，不是可直接跑的真实端点。

## 写数据（新增 / 更新 / 删除）额外红线

- **确认再写**：删除、批量更新等不可逆操作，先让用户在 UI 里显式确认，不在 `useEffect` 里静默触发。
- **幂等**：提交按钮加 loading 锁与去重键，避免重复写入。
- **权限**：写操作是否允许由平台权限决定，浏览器侧不要伪造身份；失败按后端返回的 `errorMsg` 提示，不吞错。
- **不硬编码密钥**：任何 `appSecret` / 签名逻辑都必须留在服务端 / 连接器，Canvas 源码里只出现同源相对路径与业务参数。

## 与其他技能的边界

- 需要**创建 / 管理 HTTP 连接器** → `yida-connector`。
- 需要**通过数据源调用连接器**的页面级配置 → `yida-data-source-connectors`。
- 页面**强依赖实例数据桥**（表单内字段双向绑定、提交流程深度耦合）→ 回退 `yida-custom-page`（native 免费提供该桥）。
