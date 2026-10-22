
# 应用多语言管理

## 适用场景

当用户需要操作应用后台的“全球化设置 / 多语言管理”时使用本技能，包括：

- 查询应用是否具备多语言能力、当前语言列表和翻译状态
- 设置默认语言和启用语言
- 查询、创建、更新、删除文案词条
- 为页面绑定多语言词条
- 调用单条翻译或对目标语言执行一键翻译
- 对旧应用执行多语言配置升级

## 前置检查

先执行只读检查：

```bash
openyida i18n overview <appType>
```

重点查看：

- `ability.enabled` 是否为 `true`
- `baseUrl` 是否为目标环境；Global YiDA 默认入口是 `https://www.yidaapps.com`（不是裸域 `https://yidaapps.com`），但客户可能使用自定义二级域名
- `config.defaultLanguage` 和 `config.languageList`
- `upgraded` 是否表示应用已升级到多语言配置

如果 `ability.enabled=false`，通常说明当前组织未开通国际化能力包，或需要先切到国际版登录态：

```bash
openyida login --browser --intl
openyida i18n overview <appType>
```

## 常用命令

查询语言配置：

```bash
openyida i18n config get <appType>
```

设置默认语言和启用语言：

```bash
openyida i18n config set <appType> --default zh_CN --languages zh_CN,en_US,ja_JP
```

查看可添加语言：

```bash
openyida i18n languages <appType>
openyida i18n languages <appType> --extend
```

查询词条：

```bash
openyida i18n list <appType> --keyword "欢迎" --page 1 --size 20
openyida i18n list <appType> --form-uuid <formUuid> --target-type page
```

新增或更新词条：

```bash
openyida i18n upsert <appType> welcome_title --zh-CN "欢迎" --en-US "Welcome" --ja-JP "ようこそ"
```

新增或更新词条并绑定页面：

```bash
openyida i18n upsert <appType> page_header --zh-CN "工作台" --en-US "Workbench" --bind <formUuid> --target-type page
```

批量导入词条：

```bash
openyida i18n batch-upsert <appType> --file .cache/openyida/<项目名或任务名>/i18n/i18n-items.json
```

`i18n-items.json` 先用 create_file / Write / file edit tool 创建到 `<projectRoot>/.cache/openyida/<项目名或任务名>/i18n/`；不要用 shell heredoc 或重定向写文件。从 workspace 根执行命令时路径加 `project/` 前缀。

文件内容示例：

```json
[
  {
    "i18nKey": "welcome_title",
    "i18nText": {
      "zh_CN": "欢迎",
      "en_US": "Welcome",
      "ja_JP": "ようこそ"
    }
  }
]
```

删除词条需要显式确认：

```bash
openyida i18n delete <appType> welcome_title --confirm
```

单条翻译：

```bash
openyida i18n translate <appType> --text "欢迎" --source zh_CN --targets en_US,ja_JP
```

一键翻译会批量写入目标语言，必须显式确认：

```bash
openyida i18n translate-all <appType> --targets en_US,ja_JP --confirm
```

旧应用升级多语言配置也必须显式确认：

```bash
openyida i18n upgrade <appType> --confirm
```

## 安全规则

- `overview`、`config get`、`languages`、`list` 是只读命令，可以直接执行。
- `config set`、`upsert`、`batch-upsert`、`delete`、`translate-all`、`upgrade` 会写入应用配置或词条；执行前说明影响范围。
- 删除词条会取消相关多语言绑定，必须带 `--confirm`。
- 一键翻译可能覆盖目标语言文案，必须带 `--confirm`，执行后用 `openyida i18n overview <appType>` 查看翻译任务状态。
- 如果目标是 Global YiDA，先确认登录态 `baseUrl` 属于目标国际站环境或客户自定义二级域名；默认入口是 `https://www.yidaapps.com`，不要用裸域或国内站 Cookie 调国际站应用。

## 接口映射

| 能力 | CLI | 后台接口 |
|------|-----|----------|
| 能力检查 | `overview` | `/query/commodity/checkI18nAbility.json` |
| 能力上下文 | `overview` | `/query/commodity/i18nAbilityContext.json` |
| 语言配置 | `config get/set` | `/query/appI18n/getAppLanguageConfig.json` / `/query/appI18n/updateAppLanguageConfig.json` |
| 词条查询 | `list` | `/query/appI18n/queryI18nItems.json` |
| 页面绑定词条查询 | `list --form-uuid` | `/query/appI18n/getBinded18nItems.json` |
| 词条保存 | `upsert` | `/query/appI18n/createOrUpdateI18nItem.json` |
| 批量保存 | `batch-upsert` | `/query/appI18n/batchCreateI18nItem.json` |
| 绑定词条 | `bind` / `upsert --bind` | `/query/appI18n/addI18nBinding.json` |
| 删除词条 | `delete` | `/query/appI18n/deleteI18nItem.json` |
| 单条翻译 | `translate` | `/query/appI18n/translateMultiLang.json` |
| 一键翻译 | `translate-all` | `/query/appI18n/translateAllItems.json` |
| 翻译状态 | `overview` | `/query/appI18n/checkAppTranslationStatus.json` |
| 应用升级 | `upgrade` | `/query/appI18n/upgradeAppI18n.json` |
