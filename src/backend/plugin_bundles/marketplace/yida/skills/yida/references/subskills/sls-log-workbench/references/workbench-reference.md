# SLS 工作台参考

本文件在需要具体 Tab、API 参数、状态字段或完整排查示例时读取。

## 页面基本信息

| 属性 | 值 |
|------|-----|
| 页面标题 | SLS 日志查询工作台 |
| 应用 ID | `APP_VS2F0YQPOTTCHXNKT16K` |
| 页面 UUID | `FORM-8392A7A7B3F74D45AC9787AD4C2E4742YZ4H` |
| 访问地址 | `https://www.aliwork.com/APP_VS2F0YQPOTTCHXNKT16K/workbench/FORM-8392A7A7B3F74D45AC9787AD4C2E4742YZ4H` |

## 查询 Tab

| Tab | 用途 | API |
|-----|------|-----|
| 页面 / 接口请求 | 查询页面或接口请求日志，排查接口异常 | `/query/morning/queryMonitorContext.json` |
| 业务规则 | 查询业务规则执行日志 | `/query/morning/queryBusinessRules.json` |
| 集成自动化 / 流程自动节点 | 查询集成自动化和流程自动节点日志 | `/query/morning/queryFlowRecord.json` |
| 流程人工审批 | 查询人工审批流程执行日志 | `/query/morning/queryFlowRecord.json` 或流程路径上下文 |
| 流程分支计算结果 | 查询流程分支条件计算日志 | `/query/morning/queryProcConditionLogs.json` |
| TraceId | 精确查询完整调用链路 | TraceId 相关三个 API |
| 单组织灰度匹配 | 查询单组织灰度策略匹配情况 | `/query/morning/grayChangeAndDescribeSimilarity.json` |
| 多组织灰度匹配 | 批量查询多个组织灰度策略匹配情况 | `/query/morning/calculateGraySimilarityForSimple.json` |
| 更多 | 扩展区域 | 无 |

## API 参数

### queryMonitorContext.json

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `path` | String | 接口路径，如 `/APP_xxx/v1/form/saveFormData.json` |
| `corpId` | String | 客户组织 corpId |
| `appId` | String | 应用标识；参数名是 `appId`，不是 `appType` |
| `userId` | String | 用户 ID |
| `searchKeys` | String | 检索关键词，多个用 `\|` 连接 |
| `beginTime` | String | 开始时间戳，毫秒 |
| `endTime` | String | 结束时间戳，毫秒 |
| `pageIndex` | String | 页码，从 1 开始 |
| `pageSize` | String | 每页条数 |
| `reverse` | String | 排序方式 |

响应结构：`{ success, content: { currentPage, data: [...], totalCount, hasMore } }`

### queryFlowRecord.json

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `isLogicFlow` | String | 是否逻辑流程 |
| `appId` | String | 应用 ID |
| `procInstId` | String | 流程实例 ID |
| `activityName` | String | 活动名称 |
| `beginTime` | String | 开始时间戳 |
| `endTime` | String | 结束时间戳 |
| `reverse` | String | 排序方式 |

### queryBusinessRules.json

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `executeRecordUuid` | String | 规则执行记录 UUID |

### queryProcConditionLogs.json

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `procInstId` | String | 流程实例 ID |
| `activityId` | String | 活动 ID |
| `searchKeys` | String | 检索关键词，多个用 `\|` 连接 |
| `beginTime` | String | 开始时间戳 |
| `endTime` | String | 结束时间戳 |

### TraceId API

| API | 参数 |
|-----|------|
| `/query/morning/queryMonitorContextByTraceId.json` | `traceId` |
| `/query/morning/queryMonitorErrorLog.json` | `traceId` |
| `/query/morning/queryMonitorBusinessLog.json` | `traceId` |

## 页面状态字段

| 字段 | 含义 |
|------|------|
| `queryType` | 当前 Tab |
| `queryParams` | 查询参数对象 |
| `pageIndex` / `pageSize` | 分页 |
| `logsResult` | 日志结果 |
| `loading` | 查询加载状态 |
| `pathList` | 接口路径列表 |
| `corpList` | 组织列表 |
| `traceIdLogs` | TraceId 结果 |
| `link` | 灰度分析结果链接 |

## 常见场景

### 客户表单提交报错

1. 从 URL 提取 `appType` 和 `formUuid`。
2. 构造报错前后 5 分钟时间窗。
3. 先查 `/APP_xxx/v1/form/saveFormData.json`。
4. 若无结果且是流程表单，改查 `/APP_xxx/v1/process/startInstance.json`。
5. 找到 `success: "n"` 或 `businessSuccess: "n"` 的日志，提取 `traceId`。
6. 用 `traceId` 查完整调用链。

### 业务规则问题

```bash
node project/skills/sls-log-workbench/sls-query.js --passphrase <授权口令> businessRules '{"executeRecordUuid":"<规则执行记录UUID>"}'
```

### 集成自动化 / 流程问题

```bash
node project/skills/sls-log-workbench/sls-query.js --passphrase <授权口令> flowRecord '{"appId":"APP_xxx","procInstId":"<流程实例ID>","beginTime":"<时间戳>","endTime":"<时间戳>"}'
```

### 流程分支计算

```bash
node project/skills/sls-log-workbench/sls-query.js --passphrase <授权口令> procCondition '{"procInstId":"<流程实例ID>","beginTime":"<时间戳>","endTime":"<时间戳>"}'
```

## 结果解读

- 重点看 `traceId`、`success`、`businessSuccess`、`errorCode`、`exceptionName`、`context`。
- 正常日志通常可作为链路证据；异常日志优先定位错误码和上下文。
- 未查到结果时，优先收窄或修正时间、corpId、path、appId、流程实例 ID。
