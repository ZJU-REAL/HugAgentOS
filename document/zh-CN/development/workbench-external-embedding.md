# 外部页面嵌入

管理台属于商业版 EE。管理员在外部应用编辑器选择「嵌入工作区」，并明确配置精确 origin、就绪确认方式、第三方自身存储及表单权限。默认外链仍在浏览器打开。

配置示例：

```json
{
  "declaration": {
    "schemaVersion": "1.0.0",
    "id": "reports",
    "title": "Reports",
    "target": {"kind": "external", "mode": "embedded", "url": "https://reports.example.org/home"}
  },
  "enabled": true,
  "embedding": {
    "origin": "https://reports.example.org",
    "ready": "integrated",
    "originStorage": true,
    "allowForms": false
  }
}
```

更新时另外提交当前 revision。origin 必须等于目标 URL 的规范 origin，不允许路径、通配符或宿主主机名；不同端口或协议仍共享主机 Cookie，不能作为隔离。配置中的 embedding 是管理员记录，不是插件可请求的权限。普通浏览器模式不能附带 embedding。

普通页面使用 ready=unconfirmed，默认 originStorage=false。iframe 保持 opaque origin，仅允许脚本；8 秒后显示「未确认就绪」。加载事件不代表业务已就绪，也不能证明嵌入被拒绝。

集成页面必须明确信任一个独立 origin，并启用 originStorage。宿主 HTML 必须通过 HTTP 响应头提供 `Content-Security-Policy: frame-ancestors 'self'` 或更严格的 'none'。缺少该部署防护时承载器拒绝命名 origin 存储及集成握手，提供浏览器入口。不同运行环境必须落实相同防护，不能关闭检查来获得兼容。

中间承载页只运行固定脚本，使用随机 CSP nonce 和 frame-src 精确 origin。外部页无法导航到其他 origin 或通过重定向进入宿主。宿主 HTML 的祖先策略阻止第三方嵌套宿主页面。外部页的 sandbox 只在管理员批准时增加 allow-same-origin 或 allow-forms；弹窗、下载、顶层导航及设备权限禁用。外部页面不接入插件 SDK、宿主存储、凭据、任意导航或 native invoke。

集成页面收到 `hugagent.external.init` 后，先检查 event.source===parent 和宿主精确 event.origin，再以相同 origin 回传：

```js
window.addEventListener('message', event => {
  if (event.source !== parent || event.origin !== 'https://host.example.org') return;
  const input = event.data;
  if (input?.type !== 'hugagent.external.init' || input.protocolVersion !== '1.0.0') return;
  // 页面完成业务初始化后再发送。
  parent.postMessage({
    type: 'hugagent.external.ready',
    protocolVersion: '1.0.0',
    nonce: input.nonce
  }, event.origin);
});
```

承载器验证 source、精确 origin、协议版本及每次加载重新生成的 nonce。ready 仅证明受配置 origin 的页面参与握手，不保证业务数据正确。旧页面、错误 nonce、其他窗口及非集成页面不能声明已就绪。重试、关闭和身份变化释放旧通道及计时器。

始终提供「在浏览器中打开」。点击时再次执行已认证 launch，保留真实用户点击，禁用 opener 和 Referrer。失败显示反馈。禁止去掉对方 CSP/X-Frame-Options、代理第三方内容或复制宿主凭据。对方自身 Cookie、共同身份及换票配置遵循独立的认证接入规则。

浏览器原理参考：[iframe](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)、[frame-src](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-src)、[frame-ancestors](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors)。
