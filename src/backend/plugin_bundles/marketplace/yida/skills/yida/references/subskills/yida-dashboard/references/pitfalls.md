# 看板避坑清单

> 本文档汇总开发过程中实际踩过、并已修复的坑。每一条都有**现象 → 根因 → 正确做法**。
>
> 建议时机：
> - 发布前最后一次通读
> - 运行时报错排查时按编号对照
> - 代码 review 时作为 checklist

---

## 1. `onMouseEnter / onMouseLeave` 在宜搭不触发

**现象**：KPI 卡 hover 弹菜单在本地预览 OK，发布到宜搭后毫无反应。

**根因**：宜搭自定义页面使用阿里内部 Recore 渲染引擎，**不支持** React 的 `onMouseEnter / onMouseLeave` 合成事件。

**正确做法**：
- 优先用 **CSS `:hover` 伪类** —— 把 action menu 藏在子元素，用 `:hover .actionMenu { display: block }`。注意宜搭内联样式不支持 `:hover`，需要用内嵌 `<style>` 标签或给元素加 class + 页面级 CSS。
- 或者用 **点击切换**：`onClick={() => self.setCustomState({ activeKpi: k.key })}`，配合 `onClick` 空白处关闭。

```javascript
// 方案一：内嵌 CSS
<style dangerouslySetInnerHTML={{ __html: `
  .kpi-card .action-menu { display: none; }
  .kpi-card:hover .action-menu { display: block; }
`}} />

// 方案二：点击切换
<div onClick={function(){ self.setCustomState({ activeKpi: k.key }); }}>
  ...
  {state.activeKpi === k.key && <div>{actionMenu}</div>}
</div>
```

---

## 2. `searchUserList` 返回路径猜错 → 搜人永远显示"无结果"

**现象**：派单弹窗输入人名，接口 200 成功但列表空。

**根因**：宜搭/钉钉 `searchUserList.json` 接口真实响应路径是 `data.data.content`（不是 `data.content`，更不是 `data.userList`）。

**正确做法**：兼容多种可能路径的鲁棒解析函数：

```javascript
var extractUserList = function(res) {
  if (!res) return [];
  var data = res.data || res;
  // 优先：data.data.content（实际最常见）
  if (data.data && Array.isArray(data.data.content)) return data.data.content;
  // 兜底：data.data.content.userList
  if (data.data && data.data.content && Array.isArray(data.data.content.userList)) {
    return data.data.content.userList;
  }
  // 兜底：data.data 自身是数组
  if (data.data && Array.isArray(data.data)) return data.data;
  // 兜底：直接含 content
  if (Array.isArray(data.content)) return data.content;
  if (data.content && Array.isArray(data.content.userList)) return data.content.userList;
  return [];
};
```

---

## 3. 把工作通知当成钉钉待办

**现象**：派单提交后负责人只收到一条钉钉工作通知，钉钉客户端"待办"列表里没有任务。

**根因**：`saveFormData → 集成自动化 → 消息通知节点` 创建的是工作通知，不是真实待办。真实待办必须让集成自动化调用「待办2.0 / 创建待办任务」连接器（`G-CONN-1016B8AEBED50B01B8D00009` + `G-ACT-1016B8B1911A0B01B8D0000I`）。

**正确做法**（CLI 全程可控）：

```bash
# 用 SKILL.md Step 2 里的一键命令创建并发布集成自动化，关键参数：
openyida integration create <appType> <触发表formUuid> "<看板名>-派单" \
  --events create \
  --connector-id G-CONN-1016B8AEBED50B01B8D00009 \
  --action-id G-ACT-1016B8B1911A0B01B8D0000I \
  --connector-name "待办2.0" \
  --connector-assignment unionId:processVar:<executor字段ID> \
  --connector-assignment subject:processVar:<subject字段ID> \
  --connector-assignment creatorId:processVar:form_inst_modifier \
  --connector-assignment description:processVar:<description字段ID> \
  --connector-assignment dueTime:processVar:<dueTime字段ID> \
  --connector-assignment priority:processVar:<priorityNum字段ID> \
  --connector-assignment executorIds:processVar:<executor字段ID> \
  --publish
```

前端只负责向派单触发表写一条数据：

```javascript
// ✅ 对：写入派单触发表，insert 事件触发集成自动化调用钉钉待办连接器
self.utils.yida.saveFormData({
  formUuid: FORM_CONFIG.todoTrigger.formUuid,
  appType: appType,
  formDataJson: JSON.stringify(formDataObj)
});

// ❌ 错：集成自动化只发工作通知
// ❌ 错：集成自动化没发布（status != 'y'）
```

历史方案曾建议把待办连接器绑成页面数据源 `createDingTodo` 再前端调用 `this.dataSourceMap.createDingTodo.load(payload)`，此方案已废弃（AI 无法 CLI 闭环页面数据源绑定）。

---

## 4. 派单触发表 EmployeeField 没用数组 → 集成自动化找不到人

**现象**：派单触发表数据实例创建成功，集成自动化也触发了，但 ConnectorCall 节点报 "unionId 参数异常"，或负责人收不到待办。

**根因**：宜搭 `EmployeeField` 的存储格式是**用户对象数组**（`[{ value: 'userId', label: '姓名', ... }]`），写入时前端必须传 `[String(userId)]` 数组。集成自动化在 ConnectorCall 节点透传 `processVar:<executor字段ID>` 给连接器的 `unionId` 入参时，宜搭后端会自动从 EmployeeField 解析出员工 `unionId`。如果写入时传了单字符串（`'userId'`）或空数组，后端解析不到，连接器入参就错了。

**正确做法**：搜人归一化后拿到 `userId`，写表单时一定要包在数组里：

```javascript
// ✅ 对：EmployeeField 传数组
formDataObj[FORM_CONFIG.todoTrigger.fields.executor] = [String(modal.assigneeUserId)];

// ❌ 错：传字符串，宜搭后端会解析失败
formDataObj[FORM_CONFIG.todoTrigger.fields.executor] = String(modal.assigneeUserId);

// ❌ 错：空数组 / undefined，ConnectorCall unionId 为空
formDataObj[FORM_CONFIG.todoTrigger.fields.executor] = [];
```

搜人归一化函数只需要保留一份 `userId`（用于 EmployeeField）即可：

```javascript
var normalizeUserCandidate = function(user) {
  if (!user) return null;
  var employeeUserId = user.odUserId
            || user.userId
            || user.workNo
            || user.workCode
            || user.emplId
            || user.id
            || '';
  if (!employeeUserId) return null;
  return {
    userId: String(employeeUserId),
    unionId: user.unionId ? String(user.unionId) : '',  // 备用，当前链路不需要
    odUserId: user.odUserId ? String(user.odUserId) : '',
    name: user.name || user.nickname || user.displayName || employeeUserId
  };
};
```

> 历史版本曾要求同时保留 `todoUserId` 和 `userId`（旧的 `this.dataSourceMap.createDingTodo` 链路需要），当前集成自动化链路下只需要 `userId` 一个。

---

## 5. 前端直接 `fetch` 钉钉 OpenAPI / saveFormData

**现象**：派单提交时报 CORS、401/403，或页面源码里出现 accessToken/appSecret。

**根因**：宜搭自定义页面运行在浏览器，不能直接携带钉钉开放平台密钥；表单保存接口也不应直接 `fetch('/xxx/saveFormData.json')`。

**正确做法**：
- 钉钉待办：通过**派单触发表 + 集成自动化 + 待办2.0 连接器**闭环，前端只写派单触发表
- 审计台账：通过 `self.utils.yida.saveFormData(...)` 写入
- 页面源码里不出现 accessToken/appSecret

```javascript
// ✅ 对：写派单触发表，由集成自动化在后端调用钉钉待办连接器
self.utils.yida.saveFormData({
  formUuid: FORM_CONFIG.todoTrigger.formUuid,
  appType: appType,
  formDataJson: JSON.stringify(formDataObj)
});

// ❌ 错：直接 fetch 钉钉 OpenAPI 或宜搭保存接口
fetch('https://api.dingtalk.com/v1.0/todo/...');
fetch('/dingtalk/web/' + appType + '/.../saveFormData.json');
```

---

## 6. cdnjs.cloudflare.com 在宜搭加载失败

**现象**：本地跑 OK 的 ECharts / html2canvas，发布到宜搭后控制台 `net::ERR_BLOCKED_BY_CLIENT` 或脚本加载超时。

**根因**：宜搭环境（aliwork.com）对 cloudflare CDN 有安全策略限制。

**正确做法**：统一用阿里 CDN，锁定版本：

| 库 | URL |
|----|-----|
| ECharts 5.6.0 | `https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js` |
| html2canvas 1.4.1 | `https://g.alicdn.com/code/lib/html2canvas/1.4.1/html2canvas.min.js` |

---

## 7. html2canvas 把"截图"按钮拍进去了

**现象**：用户分享到群里，截图右上角赫然一个"截图"按钮。

**正确做法**：截图按钮要同时满足两件事：有真实 `onClick`，并带 `sl-no-capture` 让 html2canvas 排除。如果只是视觉标签或占位，不要用 `<button>`，改用 `span/div`。

```javascript
// 按钮加 class，且必须有真实点击事件
<button
  className="sl-no-capture"
  onClick={function(e) {
    e.stopPropagation();
    self.captureCard('chart-region', '区域营收');
  }}
>
  截图
</button>

// 截图时排除
window.html2canvas(el, {
  backgroundColor: THEME.bg,
  scale: 2,
  useCORS: true,
  ignoreElements: function(node) {
    return !!(node && node.className
           && String(node.className).indexOf('sl-no-capture') >= 0);
  }
}).then(function(canvas) { /* ... */ });
```

class 名建议用项目前缀（`sl-` / `sc-` 等），不要直接 `.no-capture`，避免和业务 class 冲突。

---

## 8. 大文件 create_file 被截断

**现象**：一口气 create_file 写 2000+ 行，文件尾部被截断、JSX 括号不闭合、页面白屏。

**正确做法**：
- **> 1000 行**必须分批：第一次 `create_file`（覆盖），后续 `append=true` + 提供 `continuation_context`
- 或使用 `large-file-write` 技能
- 分批时按**语义边界**切分（常量块 → 函数块 → renderJsx 块），不要在一个函数中间切
- Windows 下不要用 `Get-Content -Raw | ConvertTo-Json` 把大 `.oyd.jsx` 转成 patch 文件；这会制造多份转义副本并可能耗尽内存

---

## 9. `setState` / `forceUpdate` 漏写 timestamp 隐藏 div

**现象**：调 `setCustomState` 后数据更新了，但 UI 不刷新；图表永远停在初始状态。

**根因**：`forceUpdate` 依赖 `this.setState({ timestamp })` 触发 React diff。如果 `renderJsx` 的任一 `return` 分支不渲染 timestamp，React 判定输出无变化 → 不 re-render。

**正确做法**：**每个 return 分支**（loading、空态、正常）都加：

```jsx
<div style={{ display: 'none' }}>{this.state && this.state.timestamp}</div>
```

---

## 10. 纯工具函数用 `export function` 被 UglifyJS 消除

**现象**：本地跑 OK 的 `_fetchData`，发布后报 `this._fetchData is not a function`。

**根因**：宜搭 Babel 编译器只把白名单内的 `export function` 转成组件方法；自定义名字不在白名单会被 UglifyJS 当作无用代码删除。

**正确做法**：
- **需要 `this`** 的组件方法 → 用 `export function` 且名字在白名单（`loadAllData / renderPieChart / onFilterChange` 等常见名字白名单通过）
- **纯工具函数**（不需要 `this`）→ 用 `var` 声明模块级变量：

```javascript
// ✅ 纯工具函数用 var
var _fetchData = function(params) { /* ... */ };
var _parseRow = function(row) { /* ... */ };

// ✅ 组件方法用 export function（能访问 this）
export function loadAllData() {
  var self = this;
  _fetchData(...).then(function(data) {
    self.setCustomState({ data: data });
  });
}
```

---

## 11. 宜搭自定义页面必须用 `var` 和 `function`，禁用 ES6+ 关键字

**现象**：`const` / `let` / 箭头函数 / `class` 在本地 Babel 转译 OK，发布后某些组合会报奇怪的语法错误或运行时 `is not defined`。

**正确做法**：看板源码顶层严格使用 **ES5 兼容**：
- ❌ `const THEME = {...}` → ✅ `var THEME = {...}`
- ❌ `const formatDate = (ts) => {...}` → ✅ `var formatDate = function(ts) {...}`
- ❌ `class ChartManager { ... }` → ✅ 用对象字面量
- ❌ `for (const x of arr)` → ✅ `arr.forEach((x) => {...})`（需要 `this/self` 的回调优先用箭头）
- ❌ 解构 `const {a, b} = obj` → ✅ `var a = obj.a; var b = obj.b;`

**例外**：回调函数内部可以用箭头（Babel 会处理），但**顶层声明**和**对象方法**必须 ES5。

---

## 12. EChart 容器 0 高度 → 图表不显示

**现象**：DOM 在，`echarts.init` 无报错，但画布空。

**根因**：`chartCard` 容器的 height 用了 `auto` 或 `100%`，父容器高度未定义，ECharts 算出 0 高度。

**正确做法**：`s.chartCard` 必须显式设置 height（PC 400px / 移动 280px）。同时：
- `didMount` 里 `echarts.init(container)` 之后延迟 120ms 再 `resize() + setOption(option, true)` 一次，绕过宜搭 CSS layout 未稳定的问题（见 `yida-chart` 的 `createChart()` 封装）

---

## 13. 筛选刷新时 `dispose` 重建 → 图表闪烁

**现象**：切换筛选条件，图表先空白一下再画出来，体验差。

**正确做法**：筛选后 **setOption 原地更新**，不 dispose：

```javascript
if (_chartInstances.pie) {
  _chartInstances.pie.setOption(option, true);  // 第二参数 true = 完全替换
} else {
  _chartInstances.pie = this.createChart('pie-chart');
  _chartInstances.pie.setOption(option, true);
}
```

---

## 14. `DateField` 筛选必须传毫秒时间戳

**现象**：日期筛选传 `'2025-03-01'` 给 `searchFormDatas`，返回空。

**正确做法**：
```javascript
var startTs = new Date('2025-03-01T00:00:00').getTime();
var endTs   = new Date('2025-03-31T23:59:59').getTime();
searchCondition[FIELD.planDate] = JSON.stringify([startTs, endTs]);
```

---

## 15. 看板发布 Version N 后用户看到的还是老版本

**现象**：`openyida publish` 提示成功，Version 号自增，但用户刷新还是老画面。

**根因**：
- CDN 缓存
- 用户浏览器本地缓存（Schema JSON）
- 宜搭导航栏组件有 30 秒内存缓存

**正确做法**：
- 发布带 `--health-check` 校验首屏
- 告诉用户**硬刷新**（macOS Cmd+Shift+R / Windows Ctrl+F5）
- 或等 1 分钟再打开
- 如果是组织内短链，短链指向的是最新版，直接用短链打开更准

---

## 16. `relatedKpi` 字段审计表里没有 → 派单时怎么记录关联指标

**现象**：点击 "RevPAR" KPI 的派单按钮，希望待办和审计记录里能看到"关联指标：RevPAR"，但审计表没有 `relatedKpi` 字段。

**正确做法**：把 `relatedKpi` 拼接到 `description` 字段前面，并把同一份 `fullDesc` 同时传给待办连接器和审计表：

```javascript
export function submitTodo() {
  var state = this.getCustomState();
  var modal = state.todoModal;
  var fullDesc = modal.description || '';
  if (modal.relatedKpi) {
    fullDesc = '【关联指标】' + modal.relatedKpi + '\n' + fullDesc;
  }

  var todoPayload = buildDingTodoPayload(modal, fullDesc, sourceId, detailUrl);
  this.dataSourceMap.createDingTodo.load(todoPayload);

  // 可选：审计表也写 fullDesc
  formDataObj[FIELD.description] = fullDesc;
}
```

不要为了一个展示字段强行改待办连接器入参；审计表应当保持通用。

---

## 17. 待办2.0 连接器 `priority` 透传选项字段字符串 / literal 写死 → UI 选项失效

**现象**：
- **场景 A**：`priority:processVar:<RadioField 或 SelectField 字段 ID>` → 集成自动化执行记录出现「接口参数异常」，钉钉待办没生成。
- **场景 B**：`priority:literal:10` → 集成顺利执行，但看板 UI 上选「中/高/紧急」都没用，钉钉里永远是「较低」。

**根因**：钉钉「待办2.0」连接器 `priority` 入参要求 **Number 类型**（10=较低 / 20=普通 / 30=紧急 / 40=非常紧急）：
- `processVar` 对 `SelectField/RadioField` 只会透传**字符串**（如"中"、"0"），类型不匹配 → 接口参数异常
- `literal:N` 会被 CLI Number 化（见 [connector-presets.js](../../../../lib/integration/connector-presets.js) `buildConnectorRulesFromInputs`），但**整条集成流共用同一个 N**，UI 选项失去意义

**正确做法**：task 表同时放两个字段，集成流透传 NumberField：

```bash
# task 表必含：
#   SelectField priority     （展示/审计，选项「紧急/高/中/低」）
#   NumberField priorityNum  （透传用，前端按 10/20/30/40 写入）
openyida integration create ... \
  --connector-assignment priority:processVar:<priorityNumFieldId> \
  ...
```

前端 submitTodo 双写：

```javascript
// ✅ 对：SelectField 留档 + NumberField 透传
var priorityNumMap = { '低': 10, '中': 20, '高': 30, '紧急': 40 };
formDataObj[fields.priority]    = priority;                          // SelectField 审计
formDataObj[fields.priorityNum] = priorityNumMap[priority] || 20;    // NumberField 透传

// ❌ 错：priority:literal:10 → 无论选什么都只是"较低"
// ❌ 错：priority:processVar:<SelectField 字段 ID> → 接口参数异常
```

**为什么 NumberField processVar 能透传**：NumberField 存储本身就是 Number 类型，集成自动化 processVar 不做类型转换，原样透传即可满足连接器 Number 校验。

**排查**：宜搭后台「集成自动化 → 执行记录」能清楚看到每次 ConnectorCall 的入参和连接器返回报文，出问题先查这里。

---

## 18. 看板派单 toast 文案说"已创建待办"→ 用户以为失败

**现象**：点击派单后前端 saveFormData 秒成功 toast，但用户打开钉钉 5 秒后仍没看到待办，以为系统坏了。

**根因**：宜搭集成自动化触发 ConnectorCall 到钉钉待办真正生成，通常有 **5~30 秒延迟**（集成自动化调度周期 + 连接器 RPC + 钉钉待办写入）。前端 `saveFormData` 的返回只能保证"派单触发表写入成功"，不等于"待办已到"。

**正确做法**：
- toast 文案写"派单已发起，负责人将在 30 秒内收到钉钉待办"，不写"已创建待办"
- 文档里告诉用户：如果 1 分钟还没到，让用户查看宜搭后台"集成自动化 → 执行记录"自查
- 不要用"轮询查询"来模拟同步返回（几轮 RPC 后还是无法保证）

```javascript
// ✅ 对：准确表达延迟
self.utils.toast({
  title: '派单已发起，' + modal.assigneeName + ' 将在 30 秒内收到钉钉待办',
  type: 'success'
});

// ❌ 错：误导性文案
self.utils.toast({ title: '已创建待办', type: 'success' });
```

---

## 19. 集成流失败不阻塞表单保存，前端 toast 容易误导

**现象**：`saveFormData` 把 task 记录写进 task 表成功（`res.success === true`），前端弹"派单已发起"，但钉钉端始终没收到——原因是后置的集成流 ConnectorCall 失败（连接器鉴权、参数异常、限流等）。

**根因**：宜搭的集成自动化是"数据变更后异步触发"的，它的失败不会回滚 `saveFormData`，也不会在前端返回值里暴露。

**正确做法**：
- 前端 toast 文案用"派单已发起，X 将在 30 秒内收到钉钉待办"（表达异步延迟），**不要**说"已创建待办"或"已发送"。
- 遇到用户反馈"看板说派单成功，但钉钉没收到"，引导去宜搭后台"集成自动化 → 执行记录"查看对应 processCode 的失败原因。
- 条件允许时，在集成流里加**失败分支**回写 task 表 `status=失败 / errorMsg=<错误信息>`，并在看板上做"派单历史"清单方便排障。字段扩展见 interaction-patterns 第 1 章「task 表生命周期字段」。
- 不要试图在前端做同步轮询等结果，宜搭集成流执行本就是秒级异步，轮询成本高且不稳。

---

## 20. 钉钉待办集成流回滚 runbook（v2 出问题如何 3 分钟切回 v1）

**使用场景**：v2 `processVar:<priorityNum>` 链路上线后若出现连接器异常（钉钉端待办不下发、priority 规范报错等），需要先止血回到 v1 `literal:10` 写死版。

**前置要求**：`openyida` 已登录，`processCode` 记录在 PRD 里（supply-chain / shangri-la 都有 v0/v1/v2 三组）。

**步骤**：

1. **禁用 v2**：
   ```bash
   openyida integration disable <appType> <formUuid> <v2-processCode>
   ```
   （参数缺一不可，否则会报「用法: openyida integration disable <appType> <formUuid> <processCode>」）

2. **启用 v1**（仅当 v1 尚未删除）：
   ```bash
   openyida integration enable <appType> <formUuid> <v1-processCode>
   ```
   这样 v1 立刻接管派单流。若 v1 已被彻底删除，则只能手工在宜搭后台"集成自动化"里重建。

3. **前端临时降级**（可选）：若 v1 连接器只接受 `literal:10`，那么前端继续双写 SelectField+NumberField 无害，**无需**改前端。反之若彻底回 v0（工作通知版），则需要把 `submitTodo` toast 文案改回"已发送钉钉工作通知"并重新发布页面。

4. **排障参考**：
   - 401/403：连接器 `connection` 过期或权限收回 → `openyida connector list-connections` 看连接状态
   - 400 参数异常 → 去"集成自动化 → 执行记录"查当次入参，重点看 `priority` 是否 Number 类型、`executorIds` 是否数组
   - 5xx → 钉钉服务端瞬断，隔几分钟看是否自愈；长时间 5xx 报到连接器维护方

5. **恢复到 v2**：
   ```bash
   # 先 disable v1
   openyida integration disable <appType> <formUuid> <v1-processCode>
   # 再启用 v2
   openyida integration enable <appType> <formUuid> <v2-processCode>   # 或后台启用
   ```

**命名规范**（避免下次切流误伤）：
- 创建时用 `--name "<看板>-派单钉钉待办-v<N>"`
- 历史版本在宜搭后台备注加 `-deprecated`
- `processCode` 登记在 PRD 的「钉钉待办联动」节，包含 v0/v1/v2 全部三个版本

---

## 发布前一次性过一遍的最小 checklist

- [ ] 所有 `onMouseEnter/Leave` 已替换为 CSS `:hover` 或点击切换
- [ ] `extractUserList` 覆盖 `data.data.content` 路径
- [ ] 派单触发表已用 `yida-create-form-page` 创建，含 subject/executor/description/dueTime/priority（SelectField 展示）+ `priorityNum`（NumberField 透传）6 个字段
- [ ] 集成自动化已用 `openyida integration create ... --events create --connector-id G-CONN-1016B8AEBED50B01B8D00009 --action-id G-ACT-1016B8B1911A0B01B8D0000I ... --publish` 创建并发布，status=`y`
- [ ] 集成自动化 `priority` 入参用 `processVar:<NumberField 字段 ID>`，**不用** `literal:10`（UI 选项会失效），**不用** processVar 透传 SelectField/RadioField（Number 类型校验不过）
- [ ] 前端 submitTodo 含 `priorityNumMap = { '低':10, '中':20, '高':30, '紧急':40 }`，SelectField 和 NumberField **双写**
- [ ] SelectField `priority` 选项包含「紧急/高/中/低」四档（与 priorityNumMap 一致）
- [ ] 集成自动化 `unionId` 和 `executorIds` 都绑派单触发表的 executor 字段（EmployeeField）
- [ ] 前端 `FORM_CONFIG.todoTrigger.formUuid` 已填，与 `integration create` 第二个位置参数一致
- [ ] 派单提交走 `self.utils.yida.saveFormData({ formUuid: todoTrigger.formUuid })`，**不走** `this.dataSourceMap.createDingTodo.load`
- [ ] EmployeeField 写入值是 `[String(userId)]` 数组（不是字符串，不是空数组）
- [ ] 派单 toast 文案是"派单已发起，将在 30 秒内收到钉钉待办"，**不是**"已创建待办"
- [ ] 页面源码没有直接 `fetch` 钉钉 OpenAPI 或保存 accessToken/appSecret
- [ ] ECharts / html2canvas 用 `g.alicdn.com`
- [ ] 截图按钮有真实 `onClick` + `sl-no-capture` class + `ignoreElements` 已配
- [ ] 所有可见 `<button>` 都有真实事件或显式 `disabled`，静态标签/状态胶囊不用 button
- [ ] 超过 1000 行用分批写入
- [ ] 每个 `renderJsx` 的 return 分支都有 `timestamp` 隐藏 div
- [ ] 纯工具函数用 `var`，组件方法用 `export function`
- [ ] 顶层无 `const / let / class / 箭头`
- [ ] `s.chartCard` 有显式 height
- [ ] 筛选刷新用 `setOption(option, true)` 不 dispose
- [ ] 日期筛选传毫秒时间戳
- [ ] 关联指标拼到 description 前面
