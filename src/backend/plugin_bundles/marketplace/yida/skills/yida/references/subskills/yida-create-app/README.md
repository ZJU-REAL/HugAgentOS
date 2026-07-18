
# 创建应用

## 严格禁止 (NEVER DO)
- 不要编造 appType，必须从命令返回的 JSON 中提取
- 不要在未确认 corpId 的情况下创建应用（先运行 `openyida env` 确认登录态）
- 不要在同一轮已成功创建应用后重复创建。若接口明确返回名称冲突，单点任务先询问用户；`yida-app fast_build` 可追加短后缀重试一次，不要为了查重额外探测。

## 严格要求 (MUST DO)

- 创建成功后，将 appType 记录到 `.cache/<项目名>-schema.json`
- 创建前确认当前登录的组织（corpId）与目标组织一致
- **本技能不读写 memory**：appType 等信息输出到 stdout，通过 `.cache/<项目名>-schema.json` 持久化，不依赖跨会话的 memory 状态

## 适用场景

用户说"创建应用"、"新建系统"、"搭建平台"时使用此技能。
创建应用后，通常需要继续执行：创建表单（`yida-create-form-page`）→ 创建页面（`yida-create-page`）→ 发布页面（`yida-publish-page`）。
后续如果需要自定义页面，默认走 native 兼容链路：源码写到 `project/pages/src/<页面名>.oyd.jsx`，先 `openyida check-page` / `openyida compile`，再 `openyida publish`。Code Canvas 尚未全量，只有用户明确要求或已确认当前组织/页面支持时才走 `.canvas.jsx` 链路。

---

## 命令

```bash
openyida create-app <appName> [description] [icon] [iconColor] [colour] [navTheme] [layoutDirection]
```

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `appName` | 是 | — | 应用名称 |
| `description` | 否 | 同 appName | 应用描述 |
| `icon` | 否 | `xian-yingyong` | 图标标识（见下方图标表） |
| `iconColor` | 否 | `#0089FF` | 图标背景色 |
| `colour` | 否 | `deepBlue` | 主题色（见下方主题色表） |
| `navTheme` | 否 | `dark` | 导航风格：`dark`（深色）/ `light`（浅色） |
| `layoutDirection` | 否 | `slide` | 导航布局：`slide`（侧边栏）/ `ver`（L 型顶导） |

**主题色（colour）可选值**：

| 值 | 颜色 | 适合场景 |
|------|------|------|
| `deepBlue` | 深蓝 | 政务、金融、法律、企业管理、正式场合 |
| `podBlue` | 蓝色 | 科技、教育、通用办公、SaaS 应用 |
| `royalBlue` | 皇家蓝 | 高端商务、专业服务、企业级应用 |
| `lightBlue` | 浅蓝 | 清新简约、云服务、通讯社交 |
| `teal` | 青色 | 医疗健康、环保、清新简洁类应用 |
| `podGreen` | 绿色 | 农业、环保、健康、生态 |
| `deepPurple` | 深紫 | 创意设计、艺术、高端品牌 |
| `purple` | 紫色 | 女性用户、美妆、时尚、创新科技 |
| `podOrange` | 橙色 | 活力、电商、餐饮、娱乐、社交 |
| `yellow` | 黄色 | 儿童教育、阳光活力、警示提醒 |
| `magenta` | 玖红色 | 时尚、创意、社交、娱乐类应用 |
| `red` | 红色 | 党建、政务、新闻、紧急类应用 |
| `greyBlue` | 灰蓝 | 稳重商务、工业制造、技术工程 |
| `coffee` | 咖啡 | 传统行业、文化教育、复古风格 |
| `black` | 黑色 | 极简设计、奢侈品牌、科技前沿 |

## 输出

```json
{"success":true,"appType":"APP_XXX","appName":"考勤管理","url":"{base_url}/APP_XXX/admin"}
```

## 图标列表

| 名称 | 标识 | | 名称 | 标识 |
|------|------|-|------|------|
| 新闻 | `xian-xinwen` | | 地球 | `xian-diqiu` |
| 政府 | `xian-zhengfu` | | 汽车 | `xian-qiche` |
| 应用 | `xian-yingyong` | | 飞机 | `xian-feiji` |
| 学术帽 | `xian-xueshimao` | | 电脑 | `xian-diannao` |
| 企业 | `xian-qiye` | | 工作证 | `xian-gongzuozheng` |
| 单据 | `xian-danju` | | 购物车 | `xian-gouwuche` |
| 市场 | `xian-shichang` | | 信用卡 | `xian-xinyongka` |
| 经理 | `xian-jingli` | | 活动 | `xian-huodong` |
| 法律 | `xian-falv` | | 奖杯 | `xian-jiangbei` |
| 报告 | `xian-baogao` | | 流程 | `xian-liucheng` |
| 火车 | `huoche` | | 查询 | `xian-chaxun` |
| 申报 | `xian-shenbao` | | 打卡 | `xian-daka` |

**图标背景色**：`#0089FF` `#00B853` `#FFA200` `#FF7357` `#5C72FF` `#85C700` `#FFC505` `#FF6B7A` `#8F66FF` `#14A9FF`

## 代码示例

> 需要参考完整应用创建流程时，执行以下命令获取示例，再用 `read_file` 读取：

```bash
openyida sample yida-create-app ipd-app-template   # 完整应用创建示例（含多页面/表单/仪表盘的 IPD 应用模板）
```

## 创建后交付约定

- 将 `appType`、页面 `formUuid`、表单 `fieldId` 写入 `.cache/<项目名>-schema.json`，PRD 只保留业务语义。
- 自定义页面源码统一使用 `.oyd.jsx`，事件绑定、timestamp 隐藏节点、loading 兜底遵循 `yida-custom-page` 规范。
- 造测试数据或修旧数据时，可以用 Python 或 JS 编写 `.cache/` 下的一次性脚本；优先选择更快更清晰的方式，但字段 ID 和记录 ID 必须来自真实查询。

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 命令返回失败（非 success） | 检查登录态（`openyida env`），确认 corpId 正确 |
| 应用名称重复 | 询问用户是否使用已有应用，或修改应用名称后重试 |
| 登录态失效（401） | 执行 `openyida login` 重新登录后重试 |
| 返回 JSON 中无 appType | 不要猜测 appType，重新执行命令获取 |
