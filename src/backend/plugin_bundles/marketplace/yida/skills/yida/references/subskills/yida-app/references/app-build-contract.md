# yida-app 详细编排参考

本文件只在 `full_demo` / `deep_design` 或排障时读取。`fast_build` 阶段不要默认读取。

## PRD 最小模板

```markdown
# <项目名> 需求文档

## 应用配置

| 配置项 | 值 |
|--------|-----|
| appType | APP_XXXXXX |
| corpId | dingXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX |
| baseUrl | https://www.aliwork.com |

## MVP 范围

| 范围 | 内容 |
|------|------|
| V1 必做 |  |
| V1 不做 |  |
| 后续候选 |  |

## 角色与核心旅程

| 角色 | 场景 | 可执行操作 |
|------|------|------------|
|  |  |  |

## 页面与表单

| 名称 | 类型 | 关键字段/功能 |
|------|------|---------------|
|  | 表单/页面 |  |

## 完成标准

| 场景 | 标准 |
|------|------|
| 主页面访问 | 页面发布成功并可打开 |
```

PRD 只记录业务语义，不记录 `formUuid`、`fieldId` 等 Schema ID；真实 ID 写入 `.cache/<项目名>-schema.json`。

## 字段文件示例

字段配置文件写到 `.cache/openyida/<项目名>/xxx-fields.json`，从 workspace 根执行时传 `project/.cache/openyida/<项目名>/xxx-fields.json`。

```json
[
  { "type": "TextField", "label": "访客姓名", "required": true },
  { "type": "PhoneField", "label": "联系电话" },
  { "type": "DateField", "label": "到访时间" },
  { "type": "SelectField", "label": "访问状态", "options": ["预约中", "已到访", "已离开"] }
]
```

创建后把返回 ID 汇总到：

```json
{
  "appType": "APP_XXXXXX",
  "pages": {
    "访客登记表": {
      "formUuid": "FORM-XXXXXX",
      "fields": {
        "访客姓名": "textField_xxxxxxxx"
      }
    },
    "访客工作台": {
      "formUuid": "FORM-YYYYYY"
    }
  }
}
```

## 页面链路选择

- 默认使用 `yida-canvas-custom-page`：现代 React 交互、hooks、可视化、开放 API 读数据、AI 生成页面。
- 仅当强依赖原生实例数据桥时使用 `yida-custom-page`：`this.$(fieldId)`、`this.utils.yida.*`、`dataSourceMap`、提交流程或设计器数据源深度耦合。
- 视觉方向、密度、看板技能只在用户要求深度设计、看板产品化或 `deep_design` 时加载。

## 常用 URL

| 页面类型 | URL 格式 |
|---------|---------|
| 应用首页 | `{base_url}/{appType}/workbench` |
| 表单提交页 | `{base_url}/{appType}/submission/{formUuid}` |
| 自定义页面 | `{base_url}/{appType}/custom/{formUuid}` |
| 自定义页面隐藏导航 | `{base_url}/{appType}/custom/{formUuid}?isRenderNav=false` |
| 表单详情页 | `{base_url}/{appType}/formDetail/{formUuid}?formInstId={formInstId}` |

建议在链接末尾拼接 `corpid={corpId}`，便于切换到正确组织。

## full_demo 示例顺序

### 访客系统

1. 创建应用，拿到 `appType`。
2. 创建访客登记表、访问记录表。
3. 创建访客工作台页面。
4. 编写主页面，展示今日预约、待确认、最近访问记录。
5. 发布页面并输出 URL。
6. 用户要求演示时，再整理导航并写入少量示例访客记录。

### CRM 系统

1. 创建应用。
2. 创建客户信息表、跟进记录表。
3. 若用户要求审批，再创建流程表单。
4. 创建 CRM 首页。
5. 发布首页。
6. full_demo 模式下再加报表、导航、示例数据。

### 数据大屏

1. 创建数据录入表单。
2. 创建原生报表作为聚合数据源。
3. 创建 ECharts 或 Canvas 大屏页面。
4. 发布页面并输出 URL。

## 可选示例数据规则

只有 `full_demo` / `deep_design` 或用户明确要求时写示例数据。

- 每个核心表单 2-3 条即可。
- `DateField` / `CascadeDateField` 使用 13 位毫秒时间戳。
- 写入后执行 query 抽查至少 1 条，确认 `formData` 非空。
- 用户说不要 mock / 不要示例数据时必须跳过。

## 删除应用确认

删除应用不可逆。执行前必须展示应用名称、应用 ID、影响范围，并等待用户回复“确认删除”或同等明确确认；模糊回复不能执行。

## 故障处理

| 场景 | 处理 |
|------|------|
| 发布提示登录失效 | 重新登录后再发布，不无修改重试 |
| corpId 不一致 | 询问重新登录或当前组织继续 |
| 不知道字段 ID | 使用 `yida-get-schema` 或 `.cache/<项目名>-schema.json` |
| Babel/页面校验失败 | 依据报错修 JSX，再重新校验 |
| 创建应用/表单失败 | 检查登录态、组织、参数、输入文件 |
