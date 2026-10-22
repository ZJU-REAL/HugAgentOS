# Verified Endpoints

以下接口已在当前环境实际验证通过。

环境：
- baseUrl: `https://lvsumd.aliwork.com`
- appType: `APP_AQGJYC78UTS6B66B7I80`

## 表单实例

| 能力 | 方法 | 路径 |
| --- | --- | --- |
| 查询详情列表 | GET | `/dingtalk/web/{appType}/v1/form/searchFormDatas.json` |
| 查询 ID 列表 | GET | `/dingtalk/web/{appType}/v1/form/searchFormDataIds.json` |
| 查询单条详情 | GET | `/dingtalk/web/{appType}/v1/form/getFormDataById.json` |
| 新增表单实例 | POST | `/dingtalk/web/{appType}/v1/form/saveFormData.json` |
| 更新表单实例 | POST | `/dingtalk/web/{appType}/v1/form/updateFormData.json` |
| 查询子表单 | GET | `/dingtalk/web/{appType}/v1/form/listTableDataByFormInstIdAndTableId.json` |

## 流程实例

| 能力 | 方法 | 路径 |
| --- | --- | --- |
| 发起流程 | POST | `/dingtalk/web/{appType}/v1/process/startInstance.json` |
| 查询流程列表 | GET | `/dingtalk/web/{appType}/v1/process/getInstances.json` |
| 查询流程 ID 列表 | GET | `/dingtalk/web/{appType}/v1/process/getInstanceIds.json` |
| 查询流程详情 | GET | `/dingtalk/web/{appType}/v1/process/getInstanceById.json` |
| 更新流程实例 | POST | `/dingtalk/web/{appType}/v1/process/updateInstance.json` |
| 获取审批记录 | GET | `/dingtalk/web/{appType}/v1/process/getOperationRecords.json` |

## 任务与审批

| 能力 | 方法 | 路径 |
| --- | --- | --- |
| 执行单个任务 | POST | `/dingtalk/web/{appType}/v1/task/executeTask.json` |
| 查询待办任务 | GET | `/dingtalk/web/{appType}/v1/task/getTodoTasksInApp.json` |
| 查询已完成任务 | GET | `/dingtalk/web/{appType}/v1/task/getDoneTasksInApp.json` |
| 查询已提交任务 | GET | `/dingtalk/web/{appType}/v1/process/getMySubmitInApp.json` |
| 查询抄送任务 | GET | `/dingtalk/web/{appType}/v1/task/getNotifyMeTasksInApp.json` |

## 已验证的参数能力

- `query form --search-json`
- `query form --ids-only`
- `query process --search-json`
- `query process --ids-only`
- `create process --process-code --data-json --dept-id`
- `update process --data-json`
- `execute task --out-result --remark`
- `execute task --data-json`
- `execute task --no-execute-expressions`

## 说明

- 文档历史版本中的部分接口名在当前环境不可用，例如：
  - `createFormInstance`
  - `updateFormInstance`
  - `getProcessInstances`
  - `getProcessInstanceById`
  - `updateProcessInstance`
  - `getTodoTasks`
- 以上已统一替换为本文件列出的真实可用接口。
