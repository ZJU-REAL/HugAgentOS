# 宜搭跨应用 JS API

调用方式：`this.utils.yida.<函数名>(params)`

所有接口返回 Promise，统一使用 `.then()` 和 `.catch()` 处理结果和异常。

---

## 目录

- [表单数作类 API](#表单操作类-api)
  - [saveFormData](#saveformdata) - 新建表单实例
  - [updateFormData](#updateformdata) - 更新表单组件值
  - [searchFormDataIds](#searchformdataids) - 搜索表单实例 ID 列表
  - [getFormComponentDefinationList](#getformcomponentdefinationlist) - 获取表单定义
  - [deleteFormData](#deleteformdata) - 删除表单实例
  - [getFormDataById](#getformdatabyid) - 查询表单实例详情
  - [searchFormDatas](#searchformdatas) - 搜索表单实例详情列表
- [流程操作类 API](#流程操作类-api)
  - [startProcessInstance](#startprocessinstance) - 流程发起
  - [updateProcessInstance](#updateprocessinstance) - 流程实例更新
  - [deleteProcessInstance](#deleteprocessinstance) - 删除流程实例
  - [getProcessInstances](#getprocessinstances) - 获取流程实例详情列表
  - [getProcessInstanceIds](#getprocessinstanceids) - 搜索流程实例 ID 列表
  - [getProcessInstanceById](#getprocessinstancebyid) - 获取流程实例详情
- [表单设计类 API](#表单设计类-api)
  - [saveFormSchemaInfo](#saveformschemainfo) - 创建空白表单
  - [getFormSchema](#getformschema) - 获取表单 Schema
  - [saveFormSchema](#saveformschema) - 保存表单 Schema
  - [updateFormConfig](#updateformconfig) - 更新表单配置
- [工具类 API](#工具类-api)
  - [dialog](#dialog) - 对话框
  - [formatter](#formatter) - 格式化工具
  - [getDateTimeRange](#getdatetimerange) - 获取日期时间范围
  - [getLocale](#getlocale) - 获取语言环境
  - [getLoginUserId](#getloginuserid) - 获取登录用户 ID
  - [getLoginUserName](#getloginusername) - 获取登录用户名称
  - [isMobile](#ismobile) - 判断是否移动端
  - [isSubmissionPage](#issubmissionpage) - 判断是否提交页面
  - [isViewPage](#isviewpage) - 判断是否查看页面
  - [loadScript](#loadscript) - 动态加载脚本
  - [loadStyleSheet](#loadstylesheet) - 动态加载样式表
  - [openPage](#openpage) - 打开新页面
  - [router.push](#router.push) - 页面路由跳转工具
  - [previewImage](#previewimage) - 图片预览
  - [toast](#toast) - 信息提醒

---

## 表单操作类 API

### saveFormData

**描述**：新建表单实例

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |
| appType | String | 是 | 应用 ID | `APP_XXX` |
| formDataJson | String | 是 | 表单数据（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

**formDataJson 示例**：

> 日期组件写入值必须是 13 位毫秒时间戳，例如 `1516636800000`；不要传 `"2024-01-30"` 这类日期字符串。日期区间组件传毫秒时间戳数组。

```json
{
  "textField_jcr0069m": "danhang",
  "textareaField_jcr0069n": "duohang",
  "numberField_jcr0069o": 1,
  "radioField_jcr0069p": "选项一",
  "selectField_jcr0069q": "选项一",
  "checkboxField_jcr0069r": [
    "选项二",
    "选项三"
  ],
  "multiSelectField_jcr0069s": [
    "选项二",
    "选项三"
  ],
  "dateField_jcr0069t": 1516636800000,
  "cascadeDate_jcr0069u": [
    "1514736000000",
    "1517328000000"
  ],
  "employeeField_jcr0069x": [
    "xxxxx"
  ],
  "citySelectField_jcr0069y": [
    "110000",
    "110100",
    "110101"
  ],
  "departmentField_jcr0069z": 1123456,
  "cascadeSelectField_jcr006a0": [
    "part",
    "part_b"
  ],
  "attachmentField_jna1lvyb": [
    {
      "downloadUrl": "https://www.aliwork.com/fileHandle?appType=default_tianshu_app&fileName=edd07ca9-1d2e-44b5-98fe-c1e16202f90d.txt&instId=&type=download",
      "name": "test.txt",
      "previewUrl": "https://www.aliwork.com/inst/preview?appType=default_tianshu_app&fileName=test.txt&fileSize=4&downloadUrl=edd07ca9-1d2e-44b5-98fe-c1e16202f90d.txt",
      "url": "https://www.aliwork.com/fileHandle?appType=default_tianshu_app&fileName=edd07ca9-1d2e-44b5-98fe-c1e16202f90d.txt&instId=&type=download",
      "ext": "txt"
    }
  ],
  "tableField_jcr006a1": [
    {
      "cascadeDate_jcr006aa": [
        "1514736000000",
        "1517328000000"
      ],
      "cascadeSelectField_jcr006ae": [
        "product",
        "product_a"
      ],
      "checkboxField_jcr006a7": [
        "选项一",
        "选项二",
        "选项三"
      ],
      "citySelectField_jcr006ac": [
        "120000",
        "120100",
        "120102"
      ],
      "dateField_jcr006a9": 1517328000000,
      "departmentField_jcr006ad": ["1123456"],
      "employeeField_jcr006ab": [
        "yyyyy",
        "xxxxx"
      ],
      "multiSelectField_jcr006a8": [
        "选项一",
        "选项二",
        "选项三"
      ],
      "numberField_jcr006a4": 2,
      "radioField_jcr006a5": "选项二",
      "selectField_jcr006a6": "选项三",
      "textField_jcr006a2": "子表单下单行",
      "textareaField_jcr006a3": "子表单下多行"
    }
  ]
}
```

**返回值**：

| 字段 | 类型 | 描述 |
| :--- | :--- | :--- |
| success | Boolean | 请求是否成功 |
| result | String | 实例ID |
| errorMsg | String | 错误信息 |
| errorCode | String | 错误码 |

**返回值示例**：

```json
{
  "result": "FINST-XXX",
  "success": true
}
```

**请求示例**：

```javascript
this.utils.yida.saveFormData({
  formUuid: 'FORM-XXX',
  appType: pageConfig.appType,
  formDataJson: JSON.stringify({
    textField_m1g4dcpy: '单行文本',
    textareaField_m1g4dcpz: '多行文本',
  }),
}).then((res) => {
  console.log('新建结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### updateFormData

**描述**：更新表单中指定组件值

**后端 API 路径**：`POST /dingtalk/web/{appType}/v1/form/updateFormData.json`

> ⚠️ **注意**：请勿使用错误路径 `/query/form/updateFormData.json`，该路径会返回错误。

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formInstId | String | 是 | 表单实例ID | `FINST-xxx` |
| updateFormDataJson | String | 是 | 需要更新的表单数据（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |
| useLatestVersion | String | 否 | 是否使用最新版本 | `y` |

**请求示例**：

```javascript
this.utils.yida.updateFormData({
  formInstId: 'FINST-xxx',
  updateFormDataJson: JSON.stringify({
    textField_m1g4dcpy: '单行文本',
    textareaField_m1g4dcpz: '多行文本',
  }),
  useLatestVersion: 'y',
}).then((res) => {
  console.log('更新成功');
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### searchFormDataIds

**描述**：根据条件搜索表单实例 ID 列表

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |
| currentPage | Number | 否 | 当前页，默认 1 | `1` |
| pageSize | Number | 否 | 每页记录数，默认 10，**最大 100，超过 100 会报错** | `10` |
| searchFieldJson | String | 否 | 根据表单内组件值查询（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

**请求示例**：

```javascript
this.utils.yida.searchFormDataIds({
  formUuid: 'FORM-XXX',
  currentPage: 1,
  pageSize: 10,
  searchFieldJson: JSON.stringify({
    textField_m1g4dcpy: '单行文本',
  }),
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### getFormComponentDefinationList

**描述**：获取表单定义

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |
| version | String | 否 | 版本号 | `""` |

**请求示例**：

```javascript
this.utils.yida.getFormComponentDefinationList({
  formUuid: 'FORM-XXX',
  version: '',
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### deleteFormData

**描述**：删除表单实例

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |

**请求示例**：

```javascript
this.utils.yida.deleteFormData({
  formUuid: 'FORM-XXX',
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### getFormDataById

**描述**：根据表单实例 ID 查询表单实例详情

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formInstId | String | 是 | 表单实例ID | `FINST-xxxx` |

**请求示例**：

```javascript
this.utils.yida.getFormDataById({
  formInstId: 'FINST-xxxx',
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### searchFormDatas

**描述**：根据条件搜索表单实例详情列表

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |
| searchFieldJson | String | 否 | 根据表单内组件值查询（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |
| currentPage | Number | 否 | 当前页，默认 1 | `1` |
| pageSize | Number | 否 | 每页记录数，默认 10，**最大 100，超过 100 会报错** | `10` |
| originatorId | String | 否 | 根据数据提交人工号查询 | `'2134'` |
| createFrom | String | 否 | 创建时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| createTo | String | 否 | 创建时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| modifiedFrom | String | 否 | 修改时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| modifiedTo | String | 否 | 修改时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| dynamicOrder | String | 否 | 指定排序字段 | `'{"numberField_1ac":"+"}'` |

**searchFieldJson 示例**：

> 日期查询也使用毫秒时间戳。`DateField` 区间查询传 `[开始毫秒时间戳, 结束毫秒时间戳]`。

```json
{
  "textField_jcr0069m": "danhang",
  "textareaField_jcr0069n": "duohang",
  "numberField_jcr0069o": ["1", "10"],
  "radioField_jcr0069p": "选项一",
  "selectField_jcr0069q": "选项一",
  "checkboxField_jcr0069r": ["选项二"],
  "multiSelectField_jcr0069s": ["选项二", "选项三"],
  "dateField_jcr0069t": [1514736000000, 1517414399000],
  "cascadeDate_jcr0069u": [
    [1514736000000, 1517414399000],
    [1514736000000, 1517414399000]
  ],
  "employeeField_jcr0069x": ["xxxxx"],
  "citySelectField_jcr0069y": ["110000", "110100", "110101"],
  "departmentField_jcr0069z": ["1123456"],
  "cascadeSelectField_jcr006a0": ["part", "part_b"],
  "tableField_jcr006a1": "子表单数据"
}
```

**返回值**：

| 字段 | 类型 | 描述 |
| :--- | :--- | :--- |
| currentPage | Number | 当前页 |
| totalCount | Number | 符合条件的实例总数 |
| data | Array | 实例详情列表 |

> ⚠️ **已知问题：返回结构不一致**
>
> 宜搭 API 在不同场景下会返回两种结构，**必须使用兼容写法**读取数据：
> - **直接结构**（常见）：`{ success, data: [...], totalCount, currentPage }`
> - **嵌套结构**（部分场景）：`{ success, content: { data: [...], totalCount, currentPage } }`
>
> 推荐的兼容写法：
> ```javascript
> var data = (res && res.data) || (res && res.content && res.content.data) || [];
> var total = (res && res.totalCount) || (res && res.content && res.content.totalCount) || 0;
> ```
>
> 使用 `openyida data query form` CLI 命令时，工具已自动归一化为直接结构，无需手动兼容。

> ⚠️ **pageSize 上限**：`pageSize` 最大值为 **100**，超过 100 会导致宜搭 API 返回 HTTP 500 错误（错误信息为"参数校验失败pageSize"）。推荐使用 `10`～`100` 之间的值。

> 📌 **appType 参数说明**：
> - **页面内部调用**（即在宜搭自定义页面的 JS 代码中调用 `this.utils.yida.searchFormDatas`）：**不需要传 `appType`**，SDK 会自动从当前页面上下文中获取。
> - **外部 HTTP 直接调用**（如通过 `openyida data query form` CLI 或服务端脚本）：**必须传 `appType`**，即应用的唯一标识（可在宜搭应用 URL 中找到，格式如 `APP_XXXXXXXX`）。

**请求示例**：

```javascript
this.utils.yida.searchFormDatas({
  formUuid: 'FORM-XXX',
  searchFieldJson: '',
  currentPage: 1,
  pageSize: 10,
  originatorId: '',
  createFrom: '2024-01-01',
  createTo: '2024-02-01',
  modifiedFrom: '2024-01-01',
  modifiedTo: '2024-02-01',
  dynamicOrder: '',
}).then((res) => {
  // 兼容两种返回结构
  var data = (res && res.data) || (res && res.content && res.content.data) || [];
  var total = (res && res.totalCount) || (res && res.content && res.content.totalCount) || 0;
  console.log('数据列表', data, '总数', total);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

**返回值示例（data 字段）**：

```json
{
  "gmtModified": "2018-01-24 11:22:01",
  "formUuid": "FORM-XXX",
  "formInstId": "FINST-xxx",
  "formData": {
    "numberField_jcr0069o": 1,
    "multiSelectField_jcr0069s": ["选项三", "选项二"],
    "textareaField_jcr0069n": "duohang",
    "employeeField_jcr0069x": ["xxxx"],
    "departmentField_jcr0069z": "xxxx",
    "cascadeDate_jcr0069u": ["1514736000000", "1517328000000"],
    "cascadeSelectField_jcr006a0": ["part", "part_b"],
    "tableField_jcr006a1": [
      {
        "departmentField_jcr006ad": "xxxx",
        "cascadeDate_jcr006aa": ["1514736000000", "1517328000000"],
        "selectField_jcr006a6": "选项三",
        "citySelectField_jcr006ac": ["天津", "天津市", "河东区"],
        "radioField_jcr006a5": "选项二",
        "employeeField_jcr006ab": ["xxxxxx", "yyyyyy"],
        "dateField_jcr006a9": 1517328000000,
        "textField_jcr006a2": "子表单下单行",
        "textareaField_jcr006a3": "子表单下多行",
        "cascadeSelectField_jcr006ae": ["product", "product_a"],
        "numberField_jcr006a4": 2,
        "checkboxField_jcr006a7": ["选项一", "选项三", "选项二"],
        "multiSelectField_jcr006a8": ["选项一", "选项三", "选项二"]
      }
    ],
    "selectField_jcr0069q": "选项一",
    "citySelectField_jcr0069y": ["北京", "北京市", "东城区"],
    "checkboxField_jcr0069r": ["选项三", "选项二"],
    "textField_jcr0069m": "danhang",
    "radioField_jcr0069p": "选项一",
    "dateField_jcr0069t": 1516636800000
  },
  "originator": {
    "name": {
      "pureEn_US": "userEnglishName",
      "en_US": "userEnglishName",
      "zh_CN": "userName",
      "type": "i18n"
    },
    "userId": "xxxx"
  }
}
```

---

## 流程操作类 API

### startProcessInstance

**描述**：流程发起

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 是 | 表单ID | `FORM-XXX` |
| processCode | String | 是 | 流程编码 | `TPROC--xxx` |
| deptId | String | 否 | 部门ID | `''` |
| formDataJson | String | 是 | 表单数据（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

**请求示例**：

```javascript
this.utils.yida.startProcessInstance({
  formUuid: 'FORM-XXX',
  processCode: 'TPROC--xxx',
  deptId: '',
  formDataJson: JSON.stringify({
    textField_xxx: '单行文本',
    textareaField_xxx: '多行文本',
  }),
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### updateProcessInstance

**描述**：流程实例更新

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| processInstanceId | String | 是 | 流程实例ID | `f30233fb-xxx-9ee530` |
| updateFormDataJson | String | 是 | 需要更新的表单数据（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

**请求示例**：

```javascript
this.utils.yida.updateProcessInstance({
  processInstanceId: 'f30233fb-xxx-9ee530',
  updateFormDataJson: JSON.stringify({
    textField_xxx: '单行文本',
    textareaField_xxx: '多行文本',
  }),
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### deleteProcessInstance

**描述**：删除流程实例

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| processInstanceId | String | 是 | 流程实例ID | `f30233fb-xxx-9ee530` |

**请求示例**：

```javascript
this.utils.yida.deleteProcessInstance({
  processInstanceId: 'f30233fb-xxx-9ee530',
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### getProcessInstances

**描述**：根据搜索条件获取流程实例详情列表

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 否 | 表单ID | `FORM-XXX` |
| taskId | String | 否 | 任务ID | `'2199132092'` |
| instanceStatus | String | 否 | 实例状态 | `'RUNNING'` |
| approvedResult | String | 否 | 流程审批结果 | `'agree'` |
| currentPage | Number | 否 | 当前页，默认 1 | `1` |
| pageSize | Number | 否 | 每页记录数，默认 10，**最大 100，超过 100 会报错** | `10` |
| originatorId | String | 否 | 流程发起人工号 | `'2134'` |
| createFrom | String | 否 | 创建时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| createTo | String | 否 | 创建时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| modifiedFrom | String | 否 | 修改时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| modifiedTo | String | 否 | 修改时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| searchFieldJson | String | 否 | 根据表单内组件值查询（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

**请求示例**：

```javascript
this.utils.yida.getProcessInstances({
  formUuid: 'FORM-XXX',
  taskId: '2199132092',
  instanceStatus: 'RUNNING',
  approvedResult: 'agree',
  currentPage: 1,
  pageSize: 10,
  originatorId: '2134',
  createFrom: '2024-01-01',
  createTo: '2024-02-01',
  modifiedFrom: '2024-01-01',
  modifiedTo: '2024-02-01',
  searchFieldJson: JSON.stringify({
    textField_xxx: '单行文本',
  }),
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### getProcessInstanceIds

**描述**：根据条件搜索流程实例 ID 列表

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| formUuid | String | 否 | 表单ID | `FORM-XXX` |
| taskId | String | 否 | 任务ID | `'2199132092'` |
| instanceStatus | String | 否 | 实例状态 | `'RUNNING'` |
| approvedResult | String | 否 | 流程审批结果 | `'agree'` |
| currentPage | Number | 否 | 当前页，默认 1 | `1` |
| pageSize | Number | 否 | 每页记录数，默认 10，**最大 100，超过 100 会报错** | `10` |
| originatorId | String | 否 | 流程发起人工号 | `'2134'` |
| createFrom | String | 否 | 创建时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| createTo | String | 否 | 创建时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| modifiedFrom | String | 否 | 修改时间范围起始，格式 yyyy-MM-dd | `'2024-01-01'` |
| modifiedTo | String | 否 | 修改时间范围结束，格式 yyyy-MM-dd | `'2024-02-01'` |
| searchFieldJson | String | 否 | 根据表单内组件值查询（JSON字符串） | `JSON.stringify({ textField_xxx: '值' })` |

> ⚠️ **注意**：`pageSize` 最大值为 **100**，禁止设置超过 100 的值，否则接口会报错。

**请求示例**：

```javascript
this.utils.yida.getProcessInstanceIds({
  formUuid: 'FORM-XXX',
  taskId: '2199132092',
  instanceStatus: 'RUNNING',
  approvedResult: 'agree',
  currentPage: 1,
  pageSize: 10,
  originatorId: '2134',
  createFrom: '2024-01-01',
  createTo: '2024-02-01',
  modifiedFrom: '2024-01-01',
  modifiedTo: '2024-02-01',
  searchFieldJson: JSON.stringify({
    textField_xxx: '单行文本',
  }),
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```

---

### getProcessInstanceById

**描述**：根据实例 ID 获取流程实例详情

**参数**：

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| processInstanceId | String | 是 | 流程实例ID | `f30233fb-xxx-530` |

**请求示例**：

```javascript
this.utils.yida.getProcessInstanceById({
  processInstanceId: 'f30233fb-xxx-530',
}).then((res) => {
  console.log('请求结果', res);
}).catch(({ message }) => {
  this.utils.toast({ title: message, type: 'error' });
});
```


---

## 表单设计类 API

### saveFormSchemaInfo（创建空白表单，create 模式）

- **地址**：`POST /dingtalk/web/{appType}/query/formdesign/saveFormSchemaInfo.json`
- **Content-Type**：`application/x-www-form-urlencoded`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `_csrf_token` | String | 是 | CSRF Token（由 yida-login 获取） |
| `formType` | String | 是 | 表单类型，固定 `receipt` |
| `title` | String (JSON) | 是 | 表单名称，i18n 格式：`{"zh_CN":"名称","en_US":"名称","type":"i18n"}` |

- **返回值**：

```json
{
  "content": { "formUuid": "FORM-XXX" },
  "success": true
}
```

### getFormSchema（获取表单 Schema，update 模式）

- **地址**：`GET /alibaba/web/{appType}/_view/query/formdesign/getFormSchema.json`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `formUuid` | String | 是 | 表单 UUID |
| `schemaVersion` | String | 否 | Schema 版本，默认 `V5` |

- **返回值**：完整的表单 Schema JSON，包含 `pages` 数组，结构与 `saveFormSchema` 保存的格式一致。各字段的 `fieldId`（如 `textField_xxxxxxxx`）可从 Schema 中读取。

### saveFormSchema（保存表单 Schema，两种模式共用）

- **地址**：`POST /dingtalk/web/{appType}/_view/query/formdesign/saveFormSchema.json`
- **Content-Type**：`application/x-www-form-urlencoded`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `_csrf_token` | String | 是 | CSRF Token（由 yida-login 获取） |
| `formUuid` | String | 是 | 表单 UUID |
| `content` | String (JSON) | 是 | 表单 Schema 内容（`schemaType: "superform"`） |
| `schemaVersion` | String | 是 | 固定 `V5` |
| `importSchema` | String | 是 | 固定 `"true"` |

- **返回值**：

```json
{ "success": true }
```

### updateFormConfig（更新表单配置）

- **地址**：`POST /dingtalk/web/{appType}/query/formdesign/updateFormConfig.json`
- **Content-Type**：`application/x-www-form-urlencoded`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `_csrf_token` | String | 是 | CSRF Token（由 yida-login 获取） |
| `formUuid` | String | 是 | 表单 UUID |
| `version` | Number | 是 | 版本号（新创建的表单从 1 开始） |
| `configType` | String | 是 | 固定 `MINI_RESOURCE` |
| `value` | Number | 是 | 固定 `0`（表单页面配置值） |

- **返回值**：

```json
{
  "success": true,
  "traceId": null,
  "throwable": null,
  "errorCode": null,
  "content": null,
  "errorMsg": null
}
```


## 工具类 API

宜搭提供了很多内置的工具类函数，帮助用户更好地实现一些常用功能。

### dialog

**描述**：弹出对话框，用户需要手动关闭。底层采用 Fusion 组件实现，支持配置所有 Dialog 组件属性。

**参数**：

| 参数名 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| type | String | `'alert'` | 对话框类型：`alert` / `confirm` / `show` |
| title | String | - | 对话框标题 |
| content | String \| ReactNode | - | 内容，可传入 HTML/JSX 实现复杂布局 |
| hasMask | Boolean | `true` | 是否有遮罩 |
| footer | Boolean | `true` | 是否有底部操作按钮 |
| footerAlign | String | `'right'` | 底部操作对齐方向：`left` / `center` / `right` |
| footerActions | Array | - | 底部操作类型和顺序，如 `['cancel', 'ok']` / `['ok']` / `['cancel']` |
| onOk | Function | - | 点击确定的回调函数 |
| onCancel | Function | - | 点击取消的回调函数 |

**请求示例**：

```javascript
export function popDialog() {
  this.utils.dialog({
    type: 'confirm',
    title: '确认操作',
    content: '确定要执行此操作吗？',
    onOk: () => {
      console.log('点击了确定');
    },
    onCancel: () => {
      console.log('点击了取消');
    },
  });
}

// 支持手动关闭对话框
export function closeDialog() {
  const dialog = this.utils.dialog({
    title: '处理中',
    content: '请稍候...',
  });

  // 3秒后自动关闭
  setTimeout(() => dialog.hide(), 3000);
}
```

---

### formatter

**描述**：常用的格式化函数，支持日期、金额、手机号、银行卡号等格式转换。

**参数**：

| 参数名 | 类型 | 是否必填 | 说明 |
| :--- | :--- | :--- | :--- |
| type | String | 是 | 格式化类型：`date` / `money` / `cnmobile` / `card` |
| value | String \| Number \| Date | 是 | 待格式化的值 |
| format | String | 条件必填 | 日期格式模板（仅 type=date 时必填） |

**常用格式化类型**：

| 类型 | 示例 | 输出 |
| :--- | :--- | :--- |
| `date` | `formatter('date', new Date(), 'YYYY-MM-DD')` | `2022-01-29` |
| `date` | `formatter('date', new Date(), 'YYYY/MM/DD')` | `2022/01/29` |
| `date` | `formatter('date', new Date(), 'YYYY-MM-DD HH:mm:ss')` | `2022-01-29 13:01:02` |
| `money` | `formatter('money', '10000.99', ', ')` | `10, 000.99` |
| `cnmobile` | `formatter('cnmobile', '+8615652988282')` | `+86 1565 2988 282` |
| `card` | `formatter('card', '1565298828212233')` | `1565 2988 2821 2233` |

**请求示例**：

```javascript
export function format() {
  // 格式化日期
  const date1 = this.utils.formatter('date', new Date(), 'YYYY-MM-DD');
  const date2 = this.utils.formatter('date', new Date(), 'YYYY/MM/DD');
  const dateTime = this.utils.formatter('date', new Date(), 'YYYY-MM-DD HH:mm:ss');

  // 格式化金额
  const money = this.utils.formatter('money', '10000.99', ', ');

  // 格式化手机号
  const phone = this.utils.formatter('cnmobile', '+8615652988282');

  // 格式化银行卡号
  const card = this.utils.formatter('card', '1565298828212233');
}
```

---

### getDateTimeRange

**描述**：获取当前或指定日期的开始/结束区间时间戳。

**参数**：

| 参数名 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| when | Number \| Date | `new Date()` | 指定日期，支持时间戳或 Date 对象 |
| type | String | `'day'` | 区间类型：`year` / `month` / `week` / `day` / `date` / `hour` / `minute` / `second` |

**返回值**：`[开始时间戳, 结束时间戳]` 数组

**请求示例**：

```javascript
export function search() {
  // 获取当天的开始和结束时间戳
  const [dayStart, dayEnd] = this.utils.getDateTimeRange();
  console.log(`当天范围: ${dayStart} ~ ${dayEnd}`);

  // 获取当月的开始和结束时间戳
  const [monthStart, monthEnd] = this.utils.getDateTimeRange(new Date(), 'month');
  console.log(`当月范围: ${monthStart} ~ ${monthEnd}`);
}
```

---

### getLocale

**描述**：获取当前页面的语言环境。

**返回值**：`String` - 语言代码，如 `zh_CN`、`en_US`

**请求示例**：

```javascript
export function locale() {
  const locale = this.utils.getLocale();
  console.log(`当前语言: ${locale}`); // 输出：当前语言: zh_CN
}
```

---

### getLoginUserId

**描述**：获取当前登录用户的 ID。

**返回值**：`String` - 用户 ID

**请求示例**：

```javascript
export function getUserInfo() {
  const userId = this.utils.getLoginUserId();
  console.log(`用户ID: ${userId}`); // 输出：用户ID: 43314767738888
}
```

---

### getLoginUserName

**描述**：获取当前登录用户的名称。

**返回值**：`String` - 用户名称

**请求示例**：

```javascript
export function getUserInfo() {
  const userName = this.utils.getLoginUserName();
  console.log(`用户名: ${userName}`); // 输出：用户名: 韩火火
}
```

---

### isMobile

**描述**：判断当前访问环境是否是移动端。

**返回值**：`Boolean` - `true` 表示移动端，`false` 表示 PC 端

**请求示例**：

```javascript
export function someFunctionName() {
  if (this.utils.isMobile()) {
    console.log('当前是移动端');
  } else {
    console.log('当前是 PC 端');
  }
}
```

---

### isSubmissionPage

**描述**：判断当前页面是否是数据提交页面。

**返回值**：`Boolean`

**请求示例**：

```javascript
export function someFunctionName() {
  console.log('是否提交页面:', this.utils.isSubmissionPage());
}
```

---

### isViewPage

**描述**：判断当前页面是否是数据查看页面。

**返回值**：`Boolean`

**请求示例**：

```javascript
export function someFunctionName() {
  console.log('是否查看页面:', this.utils.isViewPage());
}
```

---

### loadScript

**描述**：动态加载远程 JavaScript 脚本。

**参数**：

| 参数名 | 类型 | 是否必填 | 说明 |
| :--- | :--- | :--- | :--- |
| url | String | 是 | 脚本 URL 地址 |

**返回值**：`Promise` - 加载完成后 resolve

**请求示例**：

```javascript
export function didMount() {
  this.utils.loadScript('https://g.alicdn.com/code/lib/qrcodejs/1.0.0/qrcode.min.js')
    .then(() => {
      const qrcode = new QRCode(document.getElementById('qrcode'), {
        text: 'https://www.aliwork.com',
        width: 128,
        height: 128,
        colorDark: '#000000',
        colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.H,
      });
    });
}
```

**CDN 版本验证（loadScript 使用注意事项）**

使用 `this.utils.loadScript(url)` 加载第三方库时，需注意以下要点：

**1. `loadScript` 只接受 URL 字符串参数**

```javascript
// ✅ 正确：直接传 URL 字符串
this.utils.loadScript('https://g.alicdn.com/code/lib/echarts/5.5.0/echarts.min.js')

// ❌ 错误：不支持对象形式
this.utils.loadScript({ src: '...', type: 'css' })
```

**2. `g.alicdn.com` CDN 版本必须验证**

`g.alicdn.com` 是阿里 CDN 镜像，不是所有 npm 版本都有镜像。使用前**必须通过 `curl` 验证版本是否存在**：

```bash
# 验证 CDN 上是否存在该版本（200 = 存在，404 = 不存在）
curl -sI 'https://g.alicdn.com/code/lib/echarts/5.5.0/echarts.min.js' | head -1
```

**已验证可用的常用库版本**：

| 库 | 可用版本 | CDN URL |
| --- | --- | --- |
| ECharts | 5.5.0 | `https://g.alicdn.com/code/lib/echarts/5.5.0/echarts.min.js` |
| QRCode.js | 1.0.0 | `https://g.alicdn.com/code/lib/qrcodejs/1.0.0/qrcode.min.js` |
| QRCode | 1.5.1 | `https://g.alicdn.com/code/lib/qrcode/1.5.1/qrcode.min.js` |

> ⚠️ **典型踩坑**：ECharts 5.5.1 在 `g.alicdn.com` 上不存在（404），必须使用 5.5.0。AI 生成代码时容易使用最新版本号，但 CDN 镜像可能未同步，务必先验证。

---

### loadStyleSheet

**描述**：动态加载远程 CSS 样式表，在 `<head>` 中插入 `<link rel="stylesheet" href="...">` 标签。适用于加载图标库、第三方组件样式等 CSS 文件。

> ⚠️ **重要**：加载 CSS 文件必须使用 `loadStyleSheet`，**不能**使用 `loadScript`。
> - `loadScript(url)` → 插入 `<script>` 标签，**仅用于 JS 脚本**
> - `loadStyleSheet(url)` → 插入 `<link rel="stylesheet">` 标签，**仅用于 CSS 文件**

**参数**：

| 参数名 | 类型 | 是否必填 | 说明 |
| :--- | :--- | :--- | :--- |
| url | String | 是 | CSS 文件的完整 URL 地址 |

**返回值**：`Promise` - 样式表加载完成后 resolve

**场景一：加载 iconfont 图标库**

在 [iconfont.cn](https://www.iconfont.cn) 创建项目并收藏图标后，生成在线 CSS 链接，然后在 `didMount` 中加载：

```javascript
export function didMount() {
  // 从 iconfont.cn 项目「查看在线链接」中复制，格式如下
  this.utils.loadStyleSheet('https://at.alicdn.com/t/c/font_XXXXX_YYYYY.css');
}

export function renderJsx() {
  return (
    <div>
      {/* 类名格式：iconfont icon-<图标名> */}
      <i className="iconfont icon-home" style={{ fontSize: '24px', color: '#1677FF' }}></i>
      <i className="iconfont icon-setting" style={{ fontSize: '24px', color: '#86909C' }}></i>
    </div>
  );
}
```

**场景二：加载第三方组件 CSS（如 react-cropper）**

当使用 `loadScript` 加载第三方 JS 组件库时，若该库有对应的 CSS 文件，需同时用 `loadStyleSheet` 加载：

```javascript
export function didMount() {
  // 先加载 CSS 样式
  this.utils.loadStyleSheet('https://g.alicdn.com/yida-platform/react-cropper/1.0.0/css/react-cropper.css');
  // 再加载 JS 脚本
  this.utils.loadScript('https://g.alicdn.com/yida-platform/react-cropper/1.0.0/js/react-cropper.js')
    .then(() => {
      console.log('react-cropper 加载完成');
    });
}
```

---

### openPage

**描述**：打开新页面。在钉钉环境下会使用钉钉 API 打开，体验更友好。

### router.push

**描述**：页面路由跳转工具，用于在宜搭应用内进行页面跳转。

#### 参数说明

| 参数位置 | 参数名 | 类型 | 必填 | 说明 |
| :------- | :----- | :--- | :--- | :--- |
| 参数 1 | `target` | String | 是 | 跳转目标：同应用内的页面 ID（如 `FORM-XXX`）或完整 URL |
| 参数 2 | `params` | Object | 否 | 携带的跳转参数对象，默认 `{}` |
| 参数 3 | `newTab` | Boolean | 否 | 是否新开标签页，`true` 新开，`false` 当前页跳转，默认 `false` |
| 参数 4 | `isExternal` | Boolean | 否 | 是否是外部网址，仅当参数 1 为完整 URL 时需要传 `true` |

#### 使用场景与示例

**场景一：跳转同应用内的表单页或自定义页（推荐）**

当跳转目标与当前页面属于同一个宜搭应用时，直接传页面 ID 即可，无需手拼 URL。

```javascript
// ✅ 正确：同应用内跳转，传页面 ID
this.utils.router.push('FORM-XXX', {}, false);

// ✅ 正确：带参数跳转
this.utils.router.push('FORM-XXX', { id: '123', type: 'edit' }, false);

// ✅ 正确：跳转到自定义页（PAGE-XXX）
this.utils.router.push('PAGE-XXX', { mode: 'view' }, false);

// ✅ 正确：新标签页打开
this.utils.router.push('FORM-XXX', {}, true);
```

**场景二：跳转外部网址**

跳转到宜搭应用外部的网址时，必须传第四个参数 `isExternal = true`。

```javascript
// ✅ 正确：跳转外部网址，必须传第四个参数 true
this.utils.router.push('https://www.example.com', {}, false, true);

// ❌ 错误：跳转外部网址但未传第四个参数，会导致跳转失败
this.utils.router.push('https://www.example.com', {}, false);
```

#### 最佳实践

1. **同应用内跳转优先使用页面 ID**：不要手拼完整 URL，直接使用 `FORM-XXX` 或 `PAGE-XXX`，让系统自动处理路由。

2. **参数传递**：需要传递参数时，通过第二个参数 `params` 对象传递，目标页面可通过 `this.state.urlParams` 获取。

   ```javascript
   // 跳转时传参
   this.utils.router.push('FORM-XXX', { orderId: '123', action: 'edit' }, false);

   // 目标页面获取参数
   export function didMount() {
     const orderId = this.state.urlParams.orderId;  // '123'
     const action = this.state.urlParams.action;    // 'edit'
   }
   ```

3. **是否新开标签页**：
   - 管理系统内部页面切换，优先使用 `false`（当前页跳转），避免打开过多标签页
   - 跳转到外部系统或需要保留当前页面状态时，使用 `true`（新标签页打开）

#### 常见错误

```javascript
// ❌ 错误：跳转外部网址未传第四个参数
this.utils.router.push('https://example.com', {}, false);

// ✅ 正确：跳转外部网址必须传 isExternal = true
this.utils.router.push('https://example.com', {}, false, true);

// ❌ 错误：同应用内跳转却手拼了完整 URL（容易出错，不推荐）
this.utils.router.push('https://www.aliwork.com/APP_XXX/workbench/FORM-XXX', {}, false, true);

// ✅ 正确：同应用内跳转直接传页面 ID
this.utils.router.push('FORM-XXX', {}, false);
```

#### ⚠️ 重要提醒：禁止手拼宜搭 URL

**`aliwork.com` 是宜搭平台的域名，同应用内跳转永远不要手拼完整 URL！**

```javascript
// ❌ 绝对禁止：手拼宜搭 URL（包括 submission、workbench 等路径）
this.utils.router.push('https://www.aliwork.com/APP_XXX/submission/FORM-XXX');
this.utils.router.push('https://www.aliwork.com/APP_XXX/workbench/FORM-XXX');

// ✅ 正确：直接传页面 ID，系统自动处理路由
this.utils.router.push('FORM-XXX', {}, false);
```

**判断标准**：
- 如果 URL 包含 `aliwork.com` → 这是宜搭应用内页面，**必须**使用页面 ID（`FORM-XXX` 或 `PAGE-XXX`）
- 如果 URL 是其他域名（如 `example.com`）→ 这是外部网址，需要传第四个参数 `true`

---

### previewImage

**描述**：图片预览，支持手势缩放、滑动切换。

**参数**：

| 参数名 | 类型 | 是否必填 | 说明 |
| :--- | :--- | :--- | :--- |
| current | String | 是 | 当前预览图片的 URL |
| urls | Array | 否 | 图片 URL 列表（多图预览时使用） |

**请求示例**：

```javascript
export function previewImg() {
  // 单图预览
  this.utils.previewImage({
    current: 'https://img.alicdn.com/tfs/TB1xxx.png',
  });

  // 多图预览
  this.utils.previewImage({
    current: 'https://img.alicdn.com/tfs/TB1xxx.png',
    urls: [
      'https://img.alicdn.com/tfs/TB1xxx.png',
      'https://img.alicdn.com/tfs/TB2xxx.png',
      'https://img.alicdn.com/tfs/TB3xxx.png',
    ],
  });
}
```

---

### toast

**描述**：信息提醒，比 Dialog 更轻量，自动消失。

**参数**：

| 参数名 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| type | String | `'notice'` | 类型：`success` / `warning` / `error` / `notice` / `help` / `loading` |
| title | String | - | 提示内容 |
| size | String | `'medium'` | 尺寸：`medium` / `large` |
| duration | Number | - | 显示时长（毫秒），`loading` 类型时无效 |

**返回值**：`Function` - 关闭方法（`loading` 类型时返回）

**请求示例**：

```javascript
export function popToast() {
  // 成功提示
  this.utils.toast({
    title: '操作成功',
    type: 'success',
    size: 'large',
  });
}

// loading 提示（需手动关闭）
export function showLoadingToast() {
  const close = this.utils.toast({
    title: '加载中...',
    type: 'loading',
    size: 'large',
  });

  // 3秒后关闭
  setTimeout(close, 3000);
}

// 错误提示
export function showError() {
  this.utils.toast({
    title: '操作失败，请重试',
    type: 'error',
    duration: 3000,
  });
}
```
