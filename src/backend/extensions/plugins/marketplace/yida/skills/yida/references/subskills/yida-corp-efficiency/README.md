
# 宜搭企业效能

通过 `openyida corp-efficiency` 封装平台管理的企业效能页面能力。不要通过浏览器点击页面来抓取数据；优先使用 CLI 的稳定接口输出。

## 前置检查

执行任何操作前先确认环境和登录态：

```bash
openyida env --json
openyida login --check-only --json
```

如果未登录，先执行 `openyida login` 并在登录成功后重试。

## 查询企业效能概览

```bash
openyida corp-efficiency
openyida corp-efficiency overview --locale zh_CN
openyida corp-efficiency overview --locale en_US --raw
```

输出为 JSON，核心字段：

| 字段 | 说明 |
|------|------|
| `corpId` / `corpName` | 当前组织 |
| `overview` | 企业效能概览、是否达参考标准、节省预算和开发天数 |
| `learning` | 低代码学习完成人数、开发者认证人数 |
| `performance.metrics` | 效能指标列表，含当前值、行业参考值、百分比、是否高于参考值 |
| `performance.metrics[].detailReportFullUrl` | 对应明细报表完整链接 |
| `details` | 页面所有“查看明细”入口，含学习认证明细和效能指标明细 |
| `commodity` | 组织版本和资源使用摘要，不包含敏感 token |

## 查看效能明细

页面中的“查看明细”按钮不是单独的数据接口，而是打开宜搭原生报表页面。先列出入口，再按标题、key 或序号选择一个明细：

```bash
openyida corp-efficiency details
openyida corp-efficiency detail --title "应用数"
openyida corp-efficiency detail --key lowcodeCertification --open
openyida corp-efficiency detail --index 3 --no-open
```

`detail` 输出为 JSON，核心字段：

| 字段 | 说明 |
|------|------|
| `detail.detailReportFullUrl` | 页面点击“查看明细”实际打开的报表 URL |
| `detail.report` | 从 URL 解析出的 `appType`、`reportId/formUuid/pageId`、`topicId/prdId`、`data`、`standardData` |
| `detail.reportApi.schema` | 报表页启动接口：`GET /alibaba/web/{appType}/query/formdesign/getLatestFormWithNavNew.json` |
| `detail.reportApi.data` | 报表组件实时数据接口：`POST /alibaba/web/{appType}/visual/visualizationDataRpc/getDataAsync.json` |
| `detail.reportApi.cacheData` | 报表组件缓存数据接口：`POST /alibaba/web/{appType}/visual/visualizationDataRpc/getCacheData.json` |
| `detail.reportApi.searchData` | 筛选器/搜索类组件候选值接口：`POST /alibaba/web/{appType}/visual/visualizationDataRpc/searchDataAsync.json` |
| `browser_handoff` | 添加 `--open` 时返回，可交给浏览器打开 |

注意：`getDataAsync.json` 还需要组件级参数 `cid`、`cname`、`componentClassName`、`dataSetKey`、`queryContext`。这些参数必须来自真实报表 Schema 或浏览器 Network 请求，不能猜测。当前技能只封装明细入口和接口模板，不直接伪造组件查询。

## 搜索通知群

```bash
openyida corp-efficiency groups --query "项目群" --page 1 --size 20
```

用于找到可接收学习/认证通知的钉钉群，返回 `cid` 和群名称。

## 发送学习/认证通知

发送通知会触达真实钉钉群，必须先向用户确认目标群和通知类型，再添加 `--yes`。

```bash
openyida corp-efficiency notify --cid <群ID> --type noticeStudy --yes
openyida corp-efficiency notify --cid <群ID> --type noticeCertify --yes
openyida corp-efficiency notify --cid <群ID> --type completeStudy --yes
```

支持的 `--type`：

| 类型 | 页面含义 |
|------|----------|
| `noticeStudy` | 通知学习 |
| `noticeCertify` | 通知认证 |
| `completeStudy` | 分享学习 |

## 注意事项

- 默认查询命令是只读操作，可以直接执行。
- `notify` 是外部可见动作；没有用户明确确认时不要执行。
- 页面中的“查看明细”对应 `corp-efficiency details/detail` 输出里的报表 URL；专属版且具备 `dataFactoryPermission` 时页面直接 `window.open`，否则展示“效能分析明细”的咨询/升级引导。
- 如需排查接口结果，使用 `--raw`，但不要要求用户打开 Chrome 页面人工复制数据。
