
# 组织基本信息

## 严格禁止 (NEVER DO)

- 不要通过 Chrome UI 点击来封装 basicInfo 能力，优先使用 OpenYida CLI/API
- 不要默认输出 `corpToken`，除非用户明确要求并理解敏感性
- 不要修改企业域名，除非用户明确给出目标域名并确认影响范围
- 不要调用 `getTokenApi`、`getSeniorPrintTmpAuthCode` 等临时令牌接口来暴露敏感凭证

## 严格要求 (MUST DO)

- 查询组织概览时先运行 `openyida basic-info overview`
- 检查域名修改风险时先运行 `openyida basic-info abs-path` 查看固定域名引用页面
- 修改域名必须使用 `openyida basic-info domain set --target <domain> --confirm`
- **本技能不读写 memory**：组织信息通过 CLI 命令实时查询宜搭平台

## 适用场景

| 用户意图 | 命令 |
|---------|------|
| 查询 basicInfo 页面整体信息 | `openyida basic-info overview` |
| 查询组织名称、Corp ID、版本、企业域名 | `openyida basic-info commodity` |
| 查询授权人数和分配情况 | `openyida basic-info grant` |
| 查询附件/数据/自动化流容量 | `openyida basic-info capacity --type all` |
| 查询高级能力额度 | `openyida basic-info quota` |
| 检测固定域名引用页面 | `openyida basic-info abs-path --page 1 --size 10` |
| 查询数据准备额度 | `openyida basic-info dataflow` |
| 查询国际化能力 | `openyida basic-info i18n` |
| 查询当前企业域名 | `openyida basic-info domain` |

## 命令

### 概览

```bash
openyida basic-info overview
```

默认输出会隐藏 `corpToken`。如用户明确要求完整凭证，才可以追加：

```bash
openyida basic-info overview --include-secrets
```

### 组织信息

```bash
openyida basic-info commodity
```

返回组织名称、Corp ID、版本、企业域名、版本权益等信息。

### 授权信息

```bash
openyida basic-info grant
```

返回授权总量、已分配组织成员、上下游伙伴授权等信息。

### 资源容量

```bash
openyida basic-info capacity --type all
openyida basic-info capacity --type file
openyida basic-info capacity --type data
openyida basic-info capacity --type flow
```

### 额度

```bash
openyida basic-info quota
openyida basic-info quota --resource-key singleFormInstanceLimit
openyida basic-info quota --resource-keys portalAmount,faasAmount,ocrAmount
```

常见 `resourceKey`：

| key | 含义 |
|-----|------|
| `singleFormInstanceLimit` | 单表数据量上限 |
| `ocrAmount` | OCR 识别额度 |
| `portalAmount` | 门户额度 |
| `corpDataCardAmount` | 数据卡片额度 |
| `corpDataCardPushAmount` | 数据卡片推送额度 |
| `faasAmount` | FaaS 连接器调用额度 |
| `corpVviewAmount` | 数据大屏额度 |
| `ddDataSetAmount` | 钉钉数据集额度 |

### 固定域名引用检测

```bash
openyida basic-info abs-path --page 1 --size 10
```

域名修改前必须检查该列表；若有结果，应先修复相关页面里的固定域名引用。

### 域名

查询当前企业域名：

```bash
openyida basic-info domain
```

修改企业域名：

```bash
openyida basic-info domain set --target newname --confirm
```

`--target` 只支持小写字母、数字、中划线，必须以小写字母开头，最长 10 个字符。修改后组织内所有应用访问地址会变化，旧域名访问可能要求重新登录。

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 权限不足 | 提示用户确认当前账号是否为主管理员或平台管理员 |
| 登录态失效 | 运行 `openyida login` 或 `openyida auth status` |
| `abs-path` 返回大量记录 | 先按应用/页面逐步修复固定域名引用，再修改域名 |
| 域名修改失败 | 检查剩余修改次数、域名格式和当前组织版本 |
| `overview.errors` 非空 | 展示成功部分，同时说明失败的子接口和错误信息 |

## Agent 错误处理策略

| 错误类型 | 默认处理策略 |
|---------|-------------|
| 命令执行失败 | 停止执行，展示错误信息，不要猜测结果 |
| 参数缺失 | 主动询问用户补充，不要编造域名或 resourceKey |
| 修改域名未确认 | 停止执行，说明需要用户明确确认 |
| 敏感信息请求 | 先说明 `corpToken` 属于敏感凭证，再按用户确认决定是否追加 `--include-secrets` |
