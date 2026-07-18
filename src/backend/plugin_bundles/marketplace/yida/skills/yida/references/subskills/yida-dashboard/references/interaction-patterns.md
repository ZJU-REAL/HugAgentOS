# 看板交互闭环代码范式

> 本文档提供 5 大交互闭环的**可直接 copy** 代码片段：
> 1. 钉钉派单闭环（前端 `saveFormData` 到派单触发表 → 集成自动化 insert 事件 → 待办2.0 连接器 → 真实钉钉待办）
> 2. 负责人搜索（searchUser）
> 3. 卡片截图分享（html2canvas）
> 4. AI 快讯 marquee（CSS 滚动）
> 5. 组织内短链 + 隐藏导航

---

## 1. 钉钉派单闭环

### 1.0 正确链路

真实钉钉待办必须满足：提交后出现在钉钉客户端"待办"列表中，负责人可以在待办里处理/完成。**工作通知不是待办**，不能把 `saveFormData → 集成自动化 → 钉钉工作通知` 包装成待办闭环。

推荐链路（**CLI 全程可控**）：

```
看板点击派单
  → 前端 self.utils.yida.saveFormData({ formUuid: 派单触发表, formDataJson: {...} })
  → 派单触发表 insert 事件触发已发布的集成自动化
  → 集成自动化 ConnectorCall 节点调用「待办2.0 / 创建待办任务」
  → 钉钉真实待办生成
```

前置条件（看板发布前必须就绪，均由 SKILL.md Step 2 的命令一键完成）：

| 配置项 | 要求 |
|--------|------|
| 派单触发表（TodoTrigger） | `yida-create-form-page` 创建，至少含 subject/executor/description/dueTime/priority(SelectField)/**priorityNum(NumberField)** 6 个字段 |
| 集成自动化 | `openyida integration create ... --events create --connector-id G-CONN-1016B8AEBED50B01B8D00009 --action-id G-ACT-1016B8B1911A0B01B8D0000I ...`，发布后 status=`y` |
| `priority` 入参 | **必须 `--connector-assignment priority:processVar:<priorityNum NumberField 字段 ID>`**。NumberField 的 processVar 会以 Number 类型原样透传（10=较低 / 20=普通 / 30=紧急 / 40=非常紧急）。**不要** `literal:N` 写死（UI 选项失效），**不要** processVar 透传 SelectField/RadioField（字符串 → 接口参数异常）。详见 pitfalls.md #17 |
| executor 字段 | 表单填充 `[String(userId)]` 数组；集成自动化 `unionId` 和 `executorIds` 都绑该字段，宜搭后端自动从 EmployeeField 解析出 unionId |
| detailUrl（可选） | 看板组织内短链 + `?dashboardTodoSourceId=xxx`；通过在触发表加一个隐藏字段透传 |

不要在前端直接 `fetch('https://api.dingtalk.com/...')`，也不要在页面代码里保存 accessToken/appSecret。钉钉鉴权交给集成自动化的连接器后台托管。

### 1.0.1 为什么不再推荐"页面数据源直连连接器"

历史方案是在宜搭页面编辑器里把「待办2.0」动作绑成页面数据源 `createDingTodo`，然后前端调 `this.dataSourceMap.createDingTodo.load(payload)`。问题：

1. **AI 无法 CLI 闭环**：页面数据源绑定只能在设计器 UI 里点；Agent 没法保证这一步完成
2. **难以 PoC 回归**：连接器入参变了没法自动校验
3. **不可版本化**：绑定结果不在代码里

当前方案把连接器调用完全下沉到集成自动化（saveProcess.json 全字段 CLI 可控），前端只负责往一张普通表写一条数据。看板发布 = 前端代码 + 派单触发表 + 已发布集成自动化，三件套都能用 CLI 重放。

### 1.1 全局状态声明

```javascript
// 派单触发表字段映射：必须与 openyida integration create 里的 --connector-assignment 完全一致
// 若集成自动化入参变更（例如连接器升级），只改这里
var FORM_CONFIG = {
  todoTrigger: {
    formUuid: 'FORM-XXXXXXXX',     // yida-create-form-page 返回的 formUuid
    fields: {
      subject:     'textField_xxx',      // TextField
      executor:    'employeeField_xxx',  // EmployeeField，值需 [String(userId)]
      description: 'textareaField_xxx',  // TextareaField
      dueTime:     'dateField_xxx',      // DateField，值为毫秒时间戳 Number
      priority:    'selectField_xxx',    // SelectField（展示/审计，选项「紧急/高/中/低」）
      priorityNum: 'numberField_xxx'     // NumberField（集成流透传用，10/20/30/40）
    }
  }
  // 可选：task 审计表单字段（若启用 1.6 审计表）
  // , task: { formUuid: 'FORM-YYY', fields: {
  //     taskTitle, priority, priorityNum, assignee, deadline, description, taskStatus,
  //     todoSourceId, detailUrl,
  //     // 生命周期字段（v3，集成流回写用，前端只读；supply-chain 和 shangri-la 看板已预留）
  //     dingTaskId:  'textField_xxx',      // TextField 钉钉待办ID，成功分支回写 response.taskId
  //     lifecycleStatus: 'selectField_xxx',// SelectField 待办生命周期状态（已创建/已完成/已取消/失败）
  //     errorMsg:    'textareaField_xxx'   // TextareaField 错误信息，失败分支回写 error.message
  //   } }
};

var _customState = {
  loading: true,
  currentTime: '',
  marqueeText: '',
  // ... 业务数据

  // 派单弹窗状态
  todoModal: {
    visible: false,
    title: '',
    relatedKpi: '',        // 关联指标（会拼到 description 前）
    priority: '中',        // UI 展示用；提交时 priority 字符串留审计、priorityNum 数字透传给集成流
    description: '',
    deadline: Date.now() + 86400000 * 3,  // 默认 3 天后
    assignee: null,        // { userId, unionId, odUserId, name } 形态
    assigneeUserId: '',
    assigneeName: '',
    userSearchKeyword: '',
    userSearchResults: [],
    userSearchLoading: false,
    userSearchOpen: false
  },
  todoSubmitting: false
};

// UI 优先级文案 → 钉钉待办2.0 Number 码值（集成流通过 priorityNum NumberField 透传）
// 10=较低 / 20=普通 / 30=紧急 / 40=非常紧急
var PRIORITY_NUM_MAP = { '低': 10, '中': 20, '高': 30, '紧急': 40 };
```

### 1.2 打开派单弹窗（核心入口）

所有"派单"按钮最终都调这个函数。`relatedKpi` 可选。

```javascript
export function openDashboardTodo(title, relatedKpi, priority, description) {
  this.setCustomState({
    todoModal: {
      visible: true,
      title: title || '',
      relatedKpi: relatedKpi || '',
      priority: priority || '中',
      description: description || '',
      deadline: Date.now() + 86400000 * 3,
      assignee: null,
      assigneeUserId: '',
      assigneeName: '',
      userSearchKeyword: '',
      userSearchResults: [],
      userSearchLoading: false,
      userSearchOpen: false
    }
  });
}
```

### 1.3 每元素派单绑定示例

**KPI 卡片 action menu**：
```jsx
<div style={s.kpiActionMenu}>
  {k.actions.map(function(action) {
    return (
      <div
        style={s.kpiActionItem}
        onClick={function() {
          self.openDashboardTodo(
            action.label,
            k.label,            // 关联指标
            action.priority,
            '来自 KPI 卡：' + k.label + ' 当前值 ' + k.value
          );
        }}
      >
        {action.label}
      </div>
    );
  })}
</div>
```

**风险卡片**：
```jsx
<div
  style={s.riskItem}
  onClick={function() {
    self.openDashboardTodo(
      '风险应对：' + r.name,
      r.name,
      r.level,
      r.hint + '（当前水位 ' + r.score + '）'
    );
  }}
>
  ...
</div>
```

**图表卡片标题旁"派单"按钮**：
```jsx
<div style={s.chartHeader}>
  <span>{chart.title}</span>
  <button
    className="sl-no-capture"
    style={s.chartActionBtn}
    onClick={function() {
      self.openDashboardTodo('分析 ' + chart.title, chart.title, '中', '');
    }}
  >
    派单
  </button>
</div>
```

### 1.4 工具函数

```javascript
var getDashboardDetailUrl = function(sourceId) {
  var url = window.location.href || '';
  var joiner = url.indexOf('?') >= 0 ? '&' : '?';
  return url + joiner + 'dashboardTodoSourceId=' + encodeURIComponent(sourceId || '');
};

var buildTodoSourceId = function() {
  return 'dashboard-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
};

// 构造派单触发表的 formDataJson
// 返回值直接作为 saveFormData 的 formDataJson 字段（宜搭要求 JSON.stringify 后的字符串）
var buildTriggerFormData = function(modal, fullDesc, sourceId) {
  var f = FORM_CONFIG.todoTrigger.fields;
  var data = {};
  if (f.subject)     data[f.subject]     = modal.title;
  if (f.executor)    data[f.executor]    = [String(modal.assigneeUserId)]; // EmployeeField 必须数组
  if (f.description) data[f.description] = fullDesc;
  if (f.dueTime)     data[f.dueTime]     = modal.deadline;                 // DateField 毫秒时间戳
  // priority 双写：SelectField 字符串留审计，NumberField 透传给钉钉待办连接器（processVar 不做类型转换）
  if (f.priority)    data[f.priority]    = modal.priority;                           // 「低/中/高/紧急」
  if (f.priorityNum) data[f.priorityNum] = PRIORITY_NUM_MAP[modal.priority] || 20;   // 10/20/30/40
  // 若触发表加了 sourceId 字段，也可回填：f.sourceId && (data[f.sourceId] = sourceId);
  return data;
};

var ensureTodoTriggerForm = function() {
  if (!FORM_CONFIG || !FORM_CONFIG.todoTrigger || !FORM_CONFIG.todoTrigger.formUuid) {
    throw new Error('派单触发表未配置，请先用 yida-create-form-page 创建并填写 FORM_CONFIG.todoTrigger');
  }
  return FORM_CONFIG.todoTrigger.formUuid;
};

// 可选：独立审计表（与主链路解耦，失败可忽略）
var writeTodoAudit = function(self, modal, fullDesc, sourceId, detailUrl) {
  if (!FORM_CONFIG.task || !FORM_CONFIG.task.formUuid) {
    return Promise.resolve(null);
  }
  var appType = window.pageConfig && window.pageConfig.appType;
  if (!appType) return Promise.resolve(null);

  var fields = FORM_CONFIG.task.fields || {};
  var formDataObj = {};
  if (fields.taskTitle)    formDataObj[fields.taskTitle]    = modal.title;
  if (fields.priority)     formDataObj[fields.priority]     = modal.priority;           // SelectField 字符串（审计/展示）
  if (fields.priorityNum)  formDataObj[fields.priorityNum]  = PRIORITY_NUM_MAP[modal.priority] || 20;  // NumberField（如 task 兼任触发表）
  if (fields.assignee)     formDataObj[fields.assignee]     = [String(modal.assigneeUserId)];
  if (fields.deadline)     formDataObj[fields.deadline]     = modal.deadline;
  if (fields.description)  formDataObj[fields.description]  = fullDesc;
  if (fields.taskStatus)   formDataObj[fields.taskStatus]   = '已创建待办';
  if (fields.todoSourceId) formDataObj[fields.todoSourceId] = sourceId;
  if (fields.detailUrl)    formDataObj[fields.detailUrl]    = detailUrl;

  return self.utils.yida.saveFormData({
    formUuid: FORM_CONFIG.task.formUuid,
    appType: appType,
    formDataJson: JSON.stringify(formDataObj)
  });
};
```

### 1.5 提交派单（核心实现）

```javascript
export function submitTodo() {
  var self = this;
  var state = this.getCustomState();
  var modal = state.todoModal;

  // 校验
  if (!modal.title) {
    self.utils.toast({ title: '请填写任务标题', type: 'warning' });
    return;
  }
  if (!modal.assigneeUserId) {
    self.utils.toast({ title: '请选择负责人', type: 'warning' });
    return;
  }

  // relatedKpi 拼到 description 前（方便在钉钉待办描述里看到）
  var fullDesc = modal.description || '';
  if (modal.relatedKpi) {
    fullDesc = '【关联指标】' + modal.relatedKpi + '\n' + fullDesc;
  }

  var sourceId = buildTodoSourceId();
  var detailUrl = getDashboardDetailUrl(sourceId);

  var triggerFormUuid;
  try {
    triggerFormUuid = ensureTodoTriggerForm();
  } catch (err) {
    self.utils.toast({ title: err.message, type: 'error' });
    return;
  }

  var appType = window.pageConfig && window.pageConfig.appType;
  var formDataObj = buildTriggerFormData(modal, fullDesc, sourceId);

  self.setCustomState({ todoSubmitting: true });
  console.log('[Dashboard] saveFormData → todoTrigger =', formDataObj);

  // 主链路：写入派单触发表，触发集成自动化创建钉钉待办
  self.utils.yida.saveFormData({
    formUuid: triggerFormUuid,
    appType: appType,
    formDataJson: JSON.stringify(formDataObj)
  }).then(function(res) {
    console.log('[Dashboard] 派单触发表写入成功, instanceId =', res && (res.content || res.data));
    // 可选：独立审计表兜底（主链路成功不阻塞）
    return writeTodoAudit(self, modal, fullDesc, sourceId, detailUrl)
      .catch(function(auditErr) {
        console.warn('[Dashboard] todo audit write failed', auditErr);
        return null;
      });
  }).then(function() {
    self.utils.toast({
      title: '派单已发起，' + modal.assigneeName + ' 将在 30 秒内收到钉钉待办',
      type: 'success'
    });
    self.setCustomState({
      todoSubmitting: false,
      todoModal: _.assign({}, modal, { visible: false })
    });
  }).catch(function(err) {
    console.error('[Dashboard] submitTodo err', err);
    self.setCustomState({ todoSubmitting: false });
    self.utils.toast({
      title: '派单失败: ' + (err && err.message ? err.message : err),
      type: 'error'
    });
  });
}
```

> ⚠ **钉钉待办不是瞬时到达**：集成自动化调用连接器通常 5~30 秒延迟，toast 文案不要写"已创建待办"而是"将在 30 秒内收到"，避免用户看到没有误以为失败。

### 1.6 审计表单标准字段（可选）

在 app 下创建"看板任务审计"表单（通过 `yida-create-form-page` 技能），用于记录待办来源、状态和排障信息。它不是待办创建主链路。

| 字段 | 类型 | label | 字段 ID 常量名 |
|------|------|-------|----------------|
| 任务标题 | `TextareaField` | 任务标题 | `taskTitle` |
| 优先级 | `SelectField` (高/中/低) | 优先级 | `priority` |
| 负责人 | `EmployeeField` | 负责人 | `assignee` |
| 截止时间 | `DateField` | 截止时间 | `deadline` |
| 任务说明 | `TextareaField` | 任务说明 | `description` |
| 状态 | `SelectField` (待创建/已创建待办/创建失败/已完成/已取消) | 状态 | `taskStatus` |
| 待办 sourceId | `TextField` | 待办 sourceId | `todoSourceId` |
| 钉钉待办 ID | `TextField` | 钉钉待办 ID | `dingTodoId` |
| 详情页 URL | `TextField` | 详情页 URL | `detailUrl` |

如客户明确只要消息提醒，才配置集成自动化"新增数据触发 → 钉钉通知"。这种降级必须在交付说明里写清楚"不是钉钉待办"。

---

## 2. 负责人搜索（searchUser）

### 2.1 核心工具函数（复用 pitfalls.md #2、#4）

```javascript
// 鲁棒的 userList 提取（兼容多种响应结构）
// 说明：看板样例统一用 lodash（_.isArray / _.get / _.clone）替代原生 Object.assign / Array.isArray / 可选链
var extractUserList = function(res) {
  if (!res) return [];
  var data = res.data || res;
  if (_.isArray(_.get(data, 'data.content'))) return data.data.content;
  if (_.isArray(_.get(data, 'data.content.userList'))) return data.data.content.userList;
  if (_.isArray(_.get(data, 'data'))) return data.data;
  if (_.isArray(_.get(data, 'content'))) return data.content;
  if (_.isArray(_.get(data, 'content.userList'))) return data.content.userList;
  return [];
};

// 派单触发表 EmployeeField 值必须是 [String(userId)]
// 集成自动化会在 ConnectorCall 时自动从 EmployeeField 解析 unionId 传给待办连接器
// 所以前端这里只关心 userId；unionId/odUserId 仅作备份保留
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
    userId: String(employeeUserId),                     // 写入 EmployeeField 的数组元素
    unionId: user.unionId ? String(user.unionId) : '',  // 备用，若未来直接透传连接器
    odUserId: user.odUserId ? String(user.odUserId) : '',
    name: user.name || user.nickname || user.displayName || employeeUserId,
    avatar: user.avatar || '',
    deptName: user.deptName || user.department || ''
  };
};

var normalizeUserList = function(raw) {
  return (raw || []).map(normalizeUserCandidate).filter(Boolean);
};
```

### 2.2 搜索接口调用

```javascript
export function searchUsers(keyword) {
  var self = this;
  if (!keyword || keyword.length < 1) {
    self.setCustomState({
      todoModal: _.assign({}, self.getCustomState('todoModal'), {
        userSearchResults: [],
        userSearchLoading: false
      })
    });
    return;
  }

  self.setCustomState({
    todoModal: _.assign({}, self.getCustomState('todoModal'), {
      userSearchLoading: true
    })
  });

  self.utils.yida.searchUserList({ keyword: keyword, pageSize: 20 })
    .then(function(res) {
      var users = normalizeUserList(extractUserList(res));
      self.setCustomState({
        todoModal: _.assign({}, self.getCustomState('todoModal'), {
          userSearchResults: users,
          userSearchLoading: false,
          userSearchOpen: true
        })
      });
    })
    .catch(function(err) {
      console.error('[Dashboard] searchUsers err', err);
      self.setCustomState({
        todoModal: _.assign({}, self.getCustomState('todoModal'), {
          userSearchResults: [],
          userSearchLoading: false
        })
      });
    });
}
```

### 2.3 防抖输入

```javascript
var _searchDebounceTimer = null;

export function handleAssigneeInput(keyword) {
  var self = this;
  self.setCustomState({
    todoModal: _.assign({}, self.getCustomState('todoModal'), {
      userSearchKeyword: keyword
    })
  });
  if (_searchDebounceTimer) clearTimeout(_searchDebounceTimer);
  _searchDebounceTimer = setTimeout(function() {
    self.searchUsers(keyword);
  }, 300);
}

export function selectAssignee(user) {
  var self = this;
  self.setCustomState({
    todoModal: _.assign({}, self.getCustomState('todoModal'), {
      assignee: user,
      assigneeUserId: user.userId,
      assigneeName: user.name,
      userSearchKeyword: user.name,
      userSearchOpen: false,
      userSearchResults: []
    })
  });
}
```

### 2.4 弹窗内 JSX

```jsx
<div style={s.modalFormRow}>
  <label style={s.modalLabel}>负责人*</label>
  <div style={s.userSearchWrap}>
    <input
      style={s.modalInput}
      value={modal.userSearchKeyword}
      placeholder="输入姓名/工号搜索"
      onChange={function(e) { self.handleAssigneeInput(e.target.value); }}
      onFocus={function() {
        if (modal.userSearchResults.length > 0) {
          self.setCustomState({
            todoModal: _.assign({}, modal, { userSearchOpen: true })
          });
        }
      }}
    />
    {modal.userSearchOpen && (
      <div style={s.userSearchDropdown}>
        {modal.userSearchLoading && <div style={s.userSearchLoading}>搜索中…</div>}
        {!modal.userSearchLoading && modal.userSearchResults.length === 0 && (
          <div style={s.userSearchEmpty}>无结果</div>
        )}
        {modal.userSearchResults.map(function(user) {
          return (
            <div
              key={user.userId}
              style={s.userSearchItem}
              onClick={function() { self.selectAssignee(user); }}
            >
              <div style={s.userSearchName}>{user.name}</div>
              <div style={s.userSearchDept}>{user.deptName}</div>
            </div>
          );
        })}
      </div>
    )}
  </div>
</div>
```

---

## 3. 卡片截图分享（html2canvas）

### 3.1 按需加载 html2canvas

```javascript
var HTML2CANVAS_CDN = 'https://g.alicdn.com/code/lib/html2canvas/1.4.1/html2canvas.min.js';

export function ensureScreenshotLib() {
  if (window.html2canvas) return Promise.resolve();
  return new Promise(function(resolve, reject) {
    var script = document.createElement('script');
    script.src = HTML2CANVAS_CDN;
    script.onload = function() { resolve(); };
    script.onerror = function() { reject(new Error('html2canvas 加载失败')); };
    document.head.appendChild(script);
  });
}
```

### 3.2 截图函数（排除按钮自身）

```javascript
export function captureCard(domId, fileName) {
  var self = this;
  self.ensureScreenshotLib().then(function() {
    var el = document.getElementById(domId);
    if (!el) {
      self.utils.toast({ title: '截图目标不存在', type: 'warning' });
      return;
    }
    window.html2canvas(el, {
      backgroundColor: THEME.bg,
      scale: 2,
      useCORS: true,
      logging: false,
      ignoreElements: function(node) {
        return !!(node && node.className
               && String(node.className).indexOf('sl-no-capture') >= 0);
      }
    }).then(function(canvas) {
      // 下载到本地
      var link = document.createElement('a');
      link.download = (fileName || 'dashboard-card') + '-' + Date.now() + '.png';
      link.href = canvas.toDataURL('image/png');
      link.click();
      self.utils.toast({ title: '截图已下载', type: 'success' });
    }).catch(function(err) {
      console.error('[Dashboard] captureCard err', err);
      self.utils.toast({ title: '截图失败: ' + err.message, type: 'error' });
    });
  }).catch(function(err) {
    self.utils.toast({ title: err.message, type: 'error' });
  });
}
```

### 3.3 截图按钮渲染组件

截图控件是一个真实可点击按钮，不能只因为要加 `.sl-no-capture` 就写成静态 `<button>`。所有可见 `<button>` 必须绑定真实事件；如果只是展示"截图"状态、演示占位或不需要点击的标签，用 `span/div`。

```javascript
var renderCaptureButton = function(self, domId, fileName, s) {
  return (
    <button
      className="sl-no-capture"          /* 关键：排除自己 */
      style={s.captureBtn}
      onClick={function(e) {
        e.stopPropagation();              /* 避免触发卡片点击 */
        self.captureCard(domId, fileName);
      }}
    >
      📷
    </button>
  );
};
```

使用：
```jsx
<div id="chart-region" style={s.chartCard}>
  <div style={s.chartHeader}>
    <span>区域营收地图</span>
    {renderCaptureButton(self, 'chart-region', '区域营收', s)}
  </div>
  {/* 图表容器 */}
</div>
```

---

## 4. AI 快讯 marquee（底部滚动字幕）

### 4.1 纯 CSS 滚动（推荐，不卡）

```jsx
<div style={s.marquee}>
  <style dangerouslySetInnerHTML={{ __html: `
    @keyframes dashboard-marquee {
      0% { transform: translateX(100%); }
      100% { transform: translateX(-100%); }
    }
    .dashboard-marquee-track {
      display: inline-block;
      white-space: nowrap;
      animation: dashboard-marquee 60s linear infinite;
      will-change: transform;
    }
    .dashboard-marquee-track:hover {
      animation-play-state: paused;
    }
  `}} />
  <span style={s.marqueeIcon}>AI</span>
  <div style={{ overflow: 'hidden', flex: 1 }}>
    <span className="dashboard-marquee-track" style={s.marqueeText}>
      {state.marqueeText}
    </span>
  </div>
</div>
```

### 4.2 marqueeText 动态更新

```javascript
var MARQUEE_ITEMS = [
  '[智能预警] 新兴市场 RevPAR 增速达 23.5%，建议增加品牌投放',
  '[经营洞察] Circle 会员复购率上升至 68%，金钻客群贡献 42% 营收',
  '[风险提示] Paris Le Grand 翻新延期 3 周，已触发备用方案',
  '[数字化] Circle 2.0 会员 App DAU 破 120 万'
];

export function didMount() {
  var self = this;
  // 每 8 秒换一条
  self._marqueeTimer = setInterval(function() {
    var idx = (self.getCustomState('marqueeIdx') || 0) + 1;
    idx = idx % MARQUEE_ITEMS.length;
    self.setCustomState({
      marqueeIdx: idx,
      marqueeText: MARQUEE_ITEMS.join('  |  ')   // 或仅当前条
    });
  }, 8000);
}

export function didUnmount() {
  if (this._marqueeTimer) clearInterval(this._marqueeTimer);
}
```

---

## 5. 组织内短链 + 隐藏导航

### 5.1 发布后验证短链

发布成功拿到 formUuid 后：

```bash
# 1. 在宜搭后台配置页面公开访问，拿到短链 URL（如 https://y.aliwork.com/t/abc123）
# 2. 用 CLI 验证
openyida verify-short-url <appType> <formUuid> https://y.aliwork.com/t/abc123

# 3. 保存分享配置，开启隐藏导航
openyida save-share-config <appType> <formUuid> --hide-nav --internal
```

### 5.2 URL 规则

| URL 形态 | 用途 |
|---------|------|
| `{base}/{appType}/custom/{formUuid}` | 默认访问，带导航 |
| `{base}/{appType}/custom/{formUuid}?isRenderNav=false` | 隐藏导航（看板专用）|
| `{base}/{appType}/custom/{formUuid}?isRenderNav=false&corpid={corpId}` | 跨组织访问时追加 corpid |
| 组织内短链 `{base}/t/xxx` | 分享给内部用户，自动认证 |

### 5.3 交付话术模板

给客户的交付说明统一用这个格式：

```
【看板访问方式】

1. PC 端（推荐，大屏投屏用）
   短链：https://y.aliwork.com/t/xxx
   直链：https://www.aliwork.com/APP_XXX/custom/FORM-XXX?isRenderNav=false

2. 移动端（钉钉内）
   在钉钉对话框粘贴短链 → 自动展开卡片 → 点击"立即查看"

3. 截图分享
   看板上任意卡片右上角 📷 → 自动下载 PNG

4. 派单
   任意 KPI / 模块 / 风险点击 → 弹出派单窗 → 搜索负责人 →
   提交后宜搭派单触发表 insert 事件触发集成自动化，调用钉钉「待办2.0」连接器，
   负责人 5~30 秒内收到真实钉钉待办；审计表（可选）记录派单来源和回执
```

---

## 全部整合的 didMount 范式

```javascript
export function didMount() {
  var self = this;

  // 1. 并行加载 ECharts + html2canvas + 初始化数据
  var loadEcharts = new Promise(function(resolve, reject) {
    if (window.echarts) return resolve();
    var s = document.createElement('script');
    s.src = 'https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js';
    s.onload = function() { resolve(); };
    s.onerror = function() { reject(new Error('ECharts 加载失败')); };
    document.head.appendChild(s);
  });

  Promise.all([loadEcharts])
    .then(function() { return self.loadAllData(); })
    .then(function() {
      self.renderAllCharts();
      self.setCustomState({ loading: false });
    })
    .catch(function(err) {
      console.error('[Dashboard] didMount err', err);
      self.utils.toast({ title: '初始化失败: ' + err.message, type: 'error' });
      self.setCustomState({ loading: false });
    });

  // 2. 启动时钟
  self._clockTimer = setInterval(function() {
    self.setCustomState({ currentTime: new Date().toLocaleString('zh-CN') });
  }, 1000);

  // 3. 启动 marquee
  self._marqueeTimer = setInterval(function() {
    self.setCustomState({ marqueeText: MARQUEE_ITEMS.join('  |  ') });
  }, 8000);

  // 4. resize 监听（图表自适应）
  self._resizeHandler = function() { self.renderAllCharts(); };
  window.addEventListener('resize', self._resizeHandler);
}

export function didUnmount() {
  if (this._clockTimer)   clearInterval(this._clockTimer);
  if (this._marqueeTimer) clearInterval(this._marqueeTimer);
  if (this._resizeHandler) window.removeEventListener('resize', this._resizeHandler);
  // 销毁所有 ECharts 实例
  ['chart-region','chart-brand','chart-segment','chart-channel','chart-nps','chart-trend']
    .forEach(function(id) {
      var el = document.getElementById(id);
      if (el && window.echarts) {
        var inst = window.echarts.getInstanceByDom(el);
        if (inst) inst.dispose();
      }
    });
}
```
