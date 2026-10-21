
# 宜搭公式静态检查

## 严格禁止 (NEVER DO)

- 不要声称本地检查等同于宜搭平台真实执行结果；本技能只做静态检查。
- 不要编造平台支持的函数、字段 ID 或错误码；不确定时明确标注“需在宜搭平台验证”。
- 不要在没有 Schema 的情况下断言 `#{fieldId}` 一定存在；应提示用户提供 Schema 或先运行 `openyida get-schema`。
- 不要把 `>`、`>=`、`==` 这类运算符自动当作可靠写法；宜搭公式优先使用 `GT/GE/EQ/NE/LT/LE` 等函数。

## 适用场景

**正向触发**：

- “帮我检查这个公式”
- “这个宜搭公式为什么报错”
- “公式字段是否引用了正确 fieldId”
- “发布前检查公式风险”

## 命令

```bash
openyida formula evaluate <公式或文件> [--schema schema.json] [--json] [--strict]
```

| 参数 | 说明 |
|------|------|
| `<公式或文件>` | 公式字符串，或保存公式的本地文件路径 |
| `--schema <schema.json>` | 可选；用 `openyida get-schema` 输出校验 `#{fieldId}` 是否存在 |
| `--json` | 输出机器可读 JSON |
| `--strict` | 存在阻塞错误时返回非 0 退出码，适合 CI |

## 推荐流程

1. 如果公式包含字段引用，先获取 Schema：

```bash
openyida get-schema APP_XXX FORM-XXX
```

将 stdout 中需要复用的 Schema 通过 create_file / Write / file edit tool 保存到 `<projectRoot>/.cache/openyida/<项目名或任务名>/formula/form-schema.json`；不要用 shell 重定向。

2. 运行静态检查：

```bash
openyida formula evaluate 'IF(GT(#{numberField_total}, 100), "高", "低")' --schema .cache/openyida/<项目名或任务名>/formula/form-schema.json
```

3. 根据诊断结果修复：

| 诊断 | 处理 |
|------|------|
| `field_ref_not_in_schema` | 重新通过 `get-schema` 确认真实 fieldId，禁止猜测 |
| `comparison_operator` | 改用 `GT/GE/LT/LE/EQ/NE` |
| `fullwidth_punctuation` | 将中文逗号、括号、引号改为英文半角符号 |
| `function_unknown` | 检查函数是否确为宜搭支持函数；无法确认时在平台公式编辑器中验证 |
| `parenthesis_unclosed` / `string_unclosed` | 补全括号或引号 |

## 输出判断

- `ok: true` 只表示没有本地发现的阻塞错误。
- `warning` 表示高风险写法或本地无法确认的点，不应忽略。
- `error` 表示本地可确定的问题，例如字段引用不在提供的 Schema 中，或括号/字段引用明显不闭合。

## 与其他技能配合

| 步骤 | 技能 | 说明 |
|------|------|------|
| 前置 | `yida-get-schema` | 获取真实字段 ID |
| 前置/并行 | `yida-formula` | 编写或改写公式 |
| 后续 | `yida-create-form-page` | 将确认后的公式写入表单配置 |
