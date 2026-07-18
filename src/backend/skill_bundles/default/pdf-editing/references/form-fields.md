# pdf-cli fill-form 字段语义

`pdf-cli fill-form` 写 AcroForm 字段。每种字段类型对值的解释不一样——填错
会被 pypdf 拒绝写入或写成废数据。**永远先用 `pdf-cli read --mode form-fields`
摸清结构再填**。

---

## 流程

```bash
# Step 1：先盘点
pdf-cli read --mode form-fields --input /workspace/form.pdf
# → 返回 fields: [{name, type, value?, choices?, states?, checked_value?, radio_values?, page?}, ...]

# Step 2：按字段类型构造 JSON
Write(file_path="/workspace/fields.json", content='{"Name":"张三","Subscribed":"yes",...}')

# Step 3：写入
pdf-cli fill-form --input /workspace/form.pdf --output /workspace/filled.pdf \
    --fields-file /workspace/fields.json
```

---

## 字段类型 → 值规则

### text （文本框）

任意字符串。空字符串会清空字段。

```json
{"Name": "张三", "Address": "示例市示例区..."}
```

### checkbox （复选框）

接受 truthy / falsy 字符串。引擎规则：值的小写形式落在
`{"true", "yes", "1", "on"}` 中即视为勾选；其他任何字符串（含 `"false"` /
`"no"` / `"0"` / `"off"` / 空字符串）都视为不勾选。

| 取值 | 含义 |
|---|---|
| `"true"` / `"yes"` / `"1"` / `"on"`（大小写不敏感） | 勾选 → 写入字段的 `checked_value`（从 `read --mode form-fields` 拿） |
| 其他任意值 | 不勾选 → 写入 `/Off` |

```json
{"AcceptTerms": "yes", "Newsletter": "no"}
```

### dropdown / combobox （下拉选择）

`choices` 在 `read --mode form-fields` 里返回的结构是
`[{"value": "<内部值>", "label": "<显示标签>"}, ...]`——**两个字段都给出来，但
填表时传的是 `value`，不是 `label`**。这是 AcroForm 的内部约定：选项的
"显示给用户看的中文"和"PDF 里实际保存的值"可以不同。

```json
{"Country": "CN"}      // 不是 "中国"
```

如果用户用中文 label 描述选项（"我要选中国"），你需要从 choices 数组里查
出对应的 `value` 字段去填。

引擎会校验值是否在 `[c["value"] for c in choices]` 里；若不在，**该字段不
写入**，并把这条记录加到结果的 `validation_errors` 里（见下方"返回结构"）。

### radio （单选按钮组）

字段名指向一个 radio group，值必须是该 group 的某个 `radio_value`（pypdf
内部加了斜杠前缀，传值时**可以**带也可以不带——填表函数会自动规范化）。

```json
{"Gender": "Male"}      // 或 "/Male"
```

`radio_values` 列表来自 `read --mode form-fields` 的 `fields[i].radio_values`。

---

## 返回结构

```json
{
  "ok": true,
  "meta": {
    "filled_count": 3,
    "filled_fields": ["Name", "AcceptTerms", "Country"],
    "size_bytes": 12345,
    "validation_errors": [
      {"field": "Country",
       "error": "Value 'Mars' not in allowed choices",
       "allowed": ["CN", "US", "JP"]}
    ],
    "not_found": ["NonExistentField"],
    "hint": "use inspect_fields to see all available field names"
  }
}
```

- `validation_errors` 不算整体失败，但代表那几个字段**没被写入**，应当回去
  检查。每条记录的 key 是 `error`（人类可读消息）+ `allowed`（合法值列表）。
- `not_found` 同理（用户传的字段名 PDF 里没有）；同时返回 `hint` 提示用
  `inspect_fields` 看完整字段名清单。

---

## 常见踩坑

1. **不查 form-fields 就直接填**：90% 的错填来自此。字段名通常带英文/拼音/
   不直观的 key（如 `Field_Birth_DT_03`），不查就基本必错。
2. **把字段值传成数字**：fill_fields 的 `field_values: Dict[str, str]` 必须
   是字符串。`{"Age": 30}` 要写 `{"Age": "30"}`。
3. **radio 写成 boolean**：radio 是"组里选哪个值"，不是 true/false。
4. **加密 PDF**：`pdf-cli read --mode metadata` 会显示 `is_encrypted: true`；
   AcroForm 写入需要解密，目前 fill-form 不自动解密——遇到要先 `pdf-cli
   reformat` 转一遍把加密层去掉，再填。
