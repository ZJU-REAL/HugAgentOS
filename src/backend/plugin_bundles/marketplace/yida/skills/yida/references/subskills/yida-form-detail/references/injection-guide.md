# 表单详情页 CSS 注入流程

## 核心原则

`formDetail` 详情页不会执行表单页面 JS，因此样式必须进入 Schema 渲染树。推荐在 `FormContainer` 中注入宜搭原生 `Html` 组件，并将 CSS 放入 `<style>` 标签。

## API 顺序

### 1. 获取 Schema

```http
GET /alibaba/web/{appType}/_view/query/formdesign/getFormSchema.json?formUuid={formUuid}&schemaVersion=V5
```

需要携带登录 Cookie 和 CSRF。若当前环境返回的 Schema 包在 `content` 字段内，先解析 `content`。

### 2. 注入或更新 Html 组件

推荐使用固定 id，保证重复执行是幂等更新：

```js
const FORM_DETAIL_HTML_ID = 'yida-form-detail-css-html';
const FORM_DETAIL_FIELD_ID = 'html_yida_detail_css';

function readNodeChildren(node) {
  if (!node) {
    return [];
  }
  if (Array.isArray(node.children)) {
    return node.children;
  }
  if (Array.isArray(node.items)) {
    return node.items;
  }
  return [];
}

function ensureNodeChildren(node) {
  if (Array.isArray(node.children)) {
    return node.children;
  }
  if (Array.isArray(node.items)) {
    return node.items;
  }
  node.children = [];
  return node.children;
}

function findNode(node, componentName) {
  if (!node) {
    return null;
  }
  if (node.componentName === componentName) {
    return node;
  }

  const children = readNodeChildren(node);
  for (let i = 0; i < children.length; i++) {
    const found = findNode(children[i], componentName);
    if (found) {
      return found;
    }
  }
  return null;
}

function upsertFormDetailCss(schema, css) {
  const page = schema.pages && schema.pages[0];
  const rootNode = page && page.componentsTree && page.componentsTree[0];
  if (!rootNode) {
    throw new Error('Schema 中未找到 RootContent');
  }

  rootNode.css = 'body{background-color:#f6f7f9;}' + css;

  const formContainer = findNode(rootNode, 'FormContainer');
  if (!formContainer) {
    throw new Error('Schema 中未找到 FormContainer');
  }

  const children = ensureNodeChildren(formContainer);
  const content = `<style>${css}</style>`;
  const existing = children.find((item) => item && item.id === FORM_DETAIL_HTML_ID);

  if (existing) {
    existing.componentName = 'Html';
    existing.props = existing.props || {};
    existing.props.content = content;
    existing.props.__style__ = {
      height: '0px',
      overflow: 'hidden',
      padding: '0',
      margin: '0',
    };
    existing.props.fieldId = FORM_DETAIL_FIELD_ID;
    existing.hidden = false;
    existing.title = '勿删:详情页CSS';
    existing.isLocked = true;
    existing.condition = true;
    existing.conditionGroup = '';
    return 'updated';
  }

  children.unshift({
    componentName: 'Html',
    id: FORM_DETAIL_HTML_ID,
    props: {
      content,
      __style__: {
        height: '0px',
        overflow: 'hidden',
        padding: '0',
        margin: '0',
      },
      fieldId: FORM_DETAIL_FIELD_ID,
    },
    hidden: false,
    title: '勿删:详情页CSS',
    isLocked: true,
    condition: true,
    conditionGroup: '',
  });

  return 'inserted';
}
```

## 3. 保存 Schema

```http
POST /dingtalk/web/{appType}/_view/query/formdesign/saveFormSchema.json
Content-Type: application/x-www-form-urlencoded
```

Body:

```json
{
  "formUuid": "FORM-XXX",
  "content": "JSON.stringify(schema)",
  "schemaVersion": "V5",
  "importSchema": "true"
}
```

说明：

- 使用 `/dingtalk/web/{appType}/_view/...` 前缀。
- `schemaVersion` 必须是字符串 `V5`。
- `importSchema` 建议传字符串 `"true"`，与 OpenYida 表单保存链路保持一致。

## 4. 刷新表单配置

保存后调用 `updateFormConfig`，否则前端可能继续读缓存。

```http
POST /dingtalk/web/{appType}/query/formdesign/updateFormConfig.json
Content-Type: application/x-www-form-urlencoded
```

Body:

```json
{
  "formUuid": "FORM-XXX",
  "version": 1,
  "configType": "MINI_RESOURCE",
  "value": 0
}
```

表单页面使用 `value: 0`。自定义页面发布链路使用其他值，不要混用。

## 校验

1. 重新获取 Schema，确认存在：
   - `id: "yida-form-detail-css-html"`
   - `componentName: "Html"`
   - `hidden: false`
   - `props.content` 包含 `yida-form-detail`
2. 如果已有一条数据记录，可以打开：
   ```text
   {base_url}/{appType}/formDetail/{formUuid}?formInstId={formInstId}
   ```
3. 如果用户后续在设计器中手动删除 Html 组件，重新执行注入流程即可恢复。

## 注入方式对比

| 方式 | 是否推荐 | 说明 |
| --- | --- | --- |
| `Html` 组件注入 | 推荐 | 设计器可识别，保存时不易丢失，支持幂等更新 |
| `root.css` 字段 | 作为兜底 | formDetail 可加载，但设计器保存后可能被覆盖 |
| `RichTextField` 注入 | 不推荐 | 组件注册不完整时会出现“组件未找到” |
| `didMount` JS 注入 | 不可用 | formDetail 不执行表单页面 JS |
