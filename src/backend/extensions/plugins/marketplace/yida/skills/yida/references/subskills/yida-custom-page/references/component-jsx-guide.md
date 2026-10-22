# 自定义页面 JSX 组件指南

> 适用于宜搭自定义页面运行时：React 16、宜搭原生 `export function` 页面模式、无 `import/require`、通过 `this.utils.yida.*` 调用数据 API。

## 先说清楚边界

- 不要假设自定义页面能直接 `import` 宜搭内部表单组件；当前规范下应使用原生 JSX 元素、Tailwind `className` 和必要的内联兜底样式组合。
- Code Canvas 是自定义页面的默认链路（现代 React/hooks/可视化/AI）；本 native 指南只服务纯展示无刷新页和需数据桥的数据驱动页。用户要现代交互页，或提到代码画布、`YidaCodeCanvas`、`runtimeCode`、`importedModules` 时，切换到 `yida-canvas-custom-page`。
- 不要把字段中文名当作 `fieldId`；字段 ID 必须来自 `openyida get-schema`。
- 不要把原生表单页面的组件配置 JSON 直接复制到自定义页面 JSX；两者不是同一个运行面。
- 如果确有平台内置选择器、上传器等 API，必须先由用户提供官方示例或在目标环境验证，再写入代码。

## 组件实现原则

1. **Tailwind 视觉优先**：面向用户的控件默认用 Tailwind utility className 组合，并保留必要的 `style` 兜底；页面默认开启 Tailwind preflight，消除浏览器原生控件默认边框。
2. **非受控输入**：输入类控件使用 `defaultValue` + `onChange` 写入 `_customState`，避免 `value` 受控模式导致输入卡顿或无法输入。
3. **字段值按接口格式保存**：DateField 使用毫秒时间戳；选择/成员/部门等字段以平台数据实际结构为准，未验证时只存 ID 或文本，不伪装复杂对象。
4. **禁用可见原生下拉**：用户可见的单选下拉不要使用 `<select>`；用 `button + menu + option` 自定义下拉，避免浏览器原生控件观感不一致。
5. **移动端考虑触控尺寸**：按钮和输入框高度建议不小于 36px，表格在移动端改为卡片列表或横向滚动。

## 通用状态写入

```javascript
export function setDraftField(key, value) {
  _customState.draft = _customState.draft || {};
  _customState.draft[key] = value;
}
```

带输入法组合输入的文本输入：

```jsx
var self = this;

<input
  defaultValue={_customState.keyword || ''}
  onCompositionStart={(e) => { self._isComposing = true; }}
  onCompositionEnd={(e) => {
    self._isComposing = false;
    _customState.keyword = e.target.value;
  }}
  onChange={(e) => {
    if (self._isComposing) { return; }
    _customState.keyword = e.target.value;
  }}
  style={styles.input}
/>
```

## TextField / TextareaField

```jsx
var self = this;

<input
  defaultValue={(record.formData && record.formData[FIELDS.name]) || ''}
  placeholder="请输入"
  onChange={(e) => { self.setDraftField(FIELDS.name, e.target.value); }}
  style={styles.input}
/>

<textarea
  defaultValue={(record.formData && record.formData[FIELDS.remark]) || ''}
  placeholder="请输入备注"
  onChange={(e) => { self.setDraftField(FIELDS.remark, e.target.value); }}
  style={styles.textarea}
/>
```

## SelectField / RadioField

选项值必须来自业务配置或已有数据，不要猜测平台选项对象结构。自定义页面批量提交时，先用简单值或经验证的对象结构。

面向用户的下拉交互默认使用自定义下拉组件，不使用原生 `<select>`。组件状态放在 `_customState.openDropdown`，字段值通过 `setDraftField` 写入草稿。

```javascript
export function findOption(options, value) {
  var matched = options.filter((option) => option.value === value);
  return matched[0] || null;
}

export function toggleDropdown(key) {
  var nextOpen = _customState.openDropdown === key ? '' : key;
  _customState.openDropdown = nextOpen;
  this.forceUpdate();
}

export function chooseDropdown(key, value) {
  this.setDraftField(key, value);
  _customState.openDropdown = '';
  this.forceUpdate();
}
```

```jsx
export function renderDropdown(key, options, value, placeholder) {
  var self = this;
  var open = _customState.openDropdown === key;
  var selected = this.findOption(options, value);

  return (
    <div className="relative w-full" style={styles.dropdownWrap}>
      <button
        type="button"
        className="oyd-select-trigger flex h-10 w-full items-center justify-between rounded-md border border-slate-300 bg-white px-3 text-left text-sm text-slate-800 shadow-sm hover:border-blue-400 focus:outline-none focus:ring-2 focus:ring-blue-100"
        style={styles.selectTrigger}
        aria-expanded={open}
        onClick={(e) => { self.toggleDropdown(key); }}
      >
        <span className={selected ? 'truncate text-slate-800' : 'truncate text-slate-400'}>
          {selected ? selected.label : placeholder}
        </span>
        <span className="ml-2 text-xs text-slate-400">v</span>
      </button>

      {open && (
        <div
          className="oyd-select-menu absolute z-30 mt-1 max-h-64 w-full overflow-auto rounded-lg border border-slate-200 bg-white p-1 shadow-lg"
          style={styles.selectMenu}
          role="listbox"
        >
          {options.map((option) => {
            var active = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                className={active
                  ? 'oyd-select-option oyd-select-option-active flex w-full items-center justify-between rounded-md bg-blue-50 px-3 py-2 text-left text-sm font-medium text-blue-700'
                  : 'oyd-select-option flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50'}
                style={active ? styles.optionActive : styles.option}
                onClick={(e) => { self.chooseDropdown(key, option.value); }}
              >
                <span>{option.label}</span>
                {active && <span className="text-xs">selected</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
```

在 `renderJsx` 中调用：

```jsx
{this.renderDropdown(
  FIELDS.status,
  statusOptions,
  (record.formData && record.formData[FIELDS.status]) || '',
  '全部状态'
)}
```

兜底样式示例（Tailwind 未加载时仍可用）：

```javascript
var styles = {
  dropdownWrap: { position: 'relative', width: '100%' },
  selectTrigger: {
    width: '100%',
    minHeight: 38,
    border: '1px solid #D0D5DD',
    borderRadius: 6,
    background: '#fff',
    padding: '0 12px',
    textAlign: 'left',
    appearance: 'none',
    WebkitAppearance: 'none',
    fontFamily: 'inherit',
    boxShadow: '0 6px 14px rgba(15,23,42,.06)',
  },
  selectMenu: {
    position: 'absolute',
    zIndex: 30,
    marginTop: 6,
    width: '100%',
    background: '#fff',
    border: '1px solid #E4E7EC',
    borderRadius: 10,
    padding: 6,
    boxShadow: '0 16px 32px rgba(16,24,40,.14)',
  },
  option: {
    width: '100%',
    minHeight: 36,
    padding: '8px 12px',
    textAlign: 'left',
    background: '#fff',
    border: 0,
    borderRadius: 8,
    appearance: 'none',
    WebkitAppearance: 'none',
    fontFamily: 'inherit',
    cursor: 'pointer',
  },
  optionActive: {
    width: '100%',
    minHeight: 36,
    padding: '8px 12px',
    textAlign: 'left',
    background: 'var(--color-brand1-1, #EFF6FF)',
    border: 0,
    borderRadius: 8,
    color: 'var(--color-brand1-6, #1D4ED8)',
    appearance: 'none',
    WebkitAppearance: 'none',
    fontFamily: 'inherit',
    cursor: 'pointer',
  },
};
```

## DateField

宜搭 DateField 常用毫秒时间戳。`input[type="date"]` 输出 `YYYY-MM-DD`，写入前转为毫秒。

```javascript
export function dateInputToTimestamp(value) {
  if (!value) { return ''; }
  var timestamp = new Date(value + 'T00:00:00').getTime();
  return isNaN(timestamp) ? '' : timestamp;
}
```

```jsx
var self = this;

<input
  type="date"
  defaultValue={self.formatDateInput(record.formData && record.formData[FIELDS.planDate])}
  onChange={(e) => { self.setDraftField(FIELDS.planDate, self.dateInputToTimestamp(e.target.value)); }}
  style={styles.input}
/>
```

## NumberField

保持空值为空字符串；有值时再转数字，避免把未填项误写成 `0`。

```jsx
var self = this;

<input
  type="number"
  defaultValue={(record.formData && record.formData[FIELDS.amount]) || ''}
  onChange={(e) => {
    var raw = e.target.value;
    self.setDraftField(FIELDS.amount, raw === '' ? '' : Number(raw));
  }}
  style={styles.input}
/>
```

## EmployeeField / DepartmentField

在没有已验证 picker API 时，不要编造成员选择器。可采用以下保守方案：

- 查询场景：把成员/部门作为文本筛选条件展示，实际查询条件以接口支持为准。
- 编辑场景：只接受已知用户 ID/部门 ID，或让用户从业务侧提供的候选列表中选择。
- 展示场景：优先展示接口返回的名称字段；没有名称时展示 ID。

```jsx
{this.renderDropdown(
  FIELDS.owner,
  ownerOptions.map((user) => ({
    value: user.userId,
    label: user.name || user.userId,
  })),
  (record.formData && record.formData[FIELDS.owner]) || '',
  '请选择负责人'
)}
```

## ImageField / AttachmentField

上传能力依赖具体页面环境和接口权限。没有经验证上传接口时：

- 不要写“可直接上传”的组件承诺。
- 可以展示已有图片/附件链接。
- 如用户提供上传接口或 CDN 配置，按 `openyida cdn-upload` 或已验证接口处理。

```jsx
{attachments.map((file) => (
  <a key={file.url} href={file.url} target="_blank" rel="noreferrer" style={styles.link}>
    {file.name || file.url}
  </a>
))}
```

## TableField / 数据表格

自定义页面中的表格通常是展示或批量编辑 UI，不等同于宜搭原生子表组件。批量写入子表数据前必须确认接口接受的数据结构。

```jsx
<table style={styles.table}>
  <thead>
    <tr>
      <th style={styles.th}>客户</th>
      <th style={styles.th}>金额</th>
      <th style={styles.th}>状态</th>
    </tr>
  </thead>
  <tbody>
    {records.map((record) => (
      <tr key={record.formInstId}>
        <td style={styles.td}>{record.formData[FIELDS.customerName]}</td>
        <td style={styles.td}>{record.formData[FIELDS.amount]}</td>
        <td style={styles.td}>{record.formData[FIELDS.status]}</td>
      </tr>
    ))}
  </tbody>
</table>
```

## 筛选栏

筛选栏建议由关键词、状态、日期范围和按钮组成；点击查询时统一读取 `_customState.filters`，再调用 `this.utils.yida.searchFormDatas`。

```jsx
var self = this;

<div style={styles.filterBar}>
  <input
    defaultValue={(_customState.filters && _customState.filters.keyword) || ''}
    placeholder="搜索关键词"
    onChange={(e) => {
      _customState.filters = _customState.filters || {};
      _customState.filters.keyword = e.target.value;
    }}
    style={styles.input}
  />
  <button
    type="button"
    onClick={(e) => { self.loadRecords({ page: 1 }); }}
    style={styles.primaryButton}
  >
    查询
  </button>
</div>
```

## 最小样式基线

```javascript
const styles = {
  input: {
    height: 36,
    padding: '0 10px',
    border: '1px solid #d9dee8',
    borderRadius: 4,
    fontSize: 14,
    outline: 'none',
    background: '#fff',
  },
  textarea: {
    minHeight: 80,
    padding: 10,
    border: '1px solid #d9dee8',
    borderRadius: 4,
    fontSize: 14,
    outline: 'none',
    resize: 'vertical',
  },
  primaryButton: {
    height: 36,
    padding: '0 14px',
    border: '1px solid #1f6feb',
    borderRadius: 4,
    background: '#1f6feb',
    color: '#fff',
    cursor: 'pointer',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
  },
  th: {
    padding: '10px 12px',
    borderBottom: '1px solid #e6eaf0',
    textAlign: 'left',
    fontWeight: 600,
  },
  td: {
    padding: '10px 12px',
    borderBottom: '1px solid #edf0f5',
  },
};
```

## 发布前检查

```bash
openyida check-page pages/src/page.jsx
openyida compile pages/src/page.jsx
```

如果页面使用字段 ID、提交数据或构造查询条件，发布前还必须重新确认 Schema：

```bash
openyida get-schema APP_XXX FORM-XXX
```

如需保存完整 Schema，使用 create_file / Write / file edit tool 创建 `<projectRoot>/.cache/openyida/<项目名或任务名>/form-schema.json`；不要使用 shell 重定向。
