# pdf-editing 高频踩坑

碰到反直觉的行为先翻这里。

---

## 1. `pdf-cli create` 失败 = node / chromium 没装

`pdf-cli create` 的封面是 chromium 渲染的（scripts/engine/node_scripts/
render_cover.js）。**沙盒/mcp 镜像里 node + chromium 都已经装好**；如果在
本地裸机跑回归，缺这两个会 fallback 到 reportlab 渲染——能用但封面没那么
精美。

判断方式：返回的 `meta.cover_mode` 是 `chromium`（正常）还是
`reportlab_fallback`（缺 chromium）。前者好看，后者凑合。

---

## 2. `pdf-cli reformat` 看上去什么都没做就报"file not found in workdir"

`reformat` 的源文件**后缀必须**是 `.md / .markdown / .txt / .docx / .pdf
/ .json`（小写）。后缀别的都会报"unsupported source type"。

如果用户给你的是 `.MD`、`.Markdown` 等大小写不对的后缀，本机 Linux 区分
大小写——`Path.suffix.lower()` 在 cli 里做了 normalize，但 stage 文件用的
是 source 后缀。一般不会踩坑，但若用户上传的文件名带空格或非 ASCII 字符
后跟非常规后缀，先 `Move` 一下到规范名字。

---

## 3. `pdf-cli fill-form` 不查字段就填 = 几乎必错

AcroForm 字段名通常是英文/拼音/不直观的 key（`Field_Birth_DT_03`、
`txtCompanyName_2`）。直接按业务字段名（"出生日期"、"公司名称"）填会全
部落到 `not_found` 数组里——表单等于没填。

**步骤铁律**：
1. `pdf-cli read --mode form-fields --input form.pdf`
2. 按返回的 `fields[i].name` 构造 fields.json
3. 检查 checkbox 用 "yes"/"no"、dropdown 用 choices 里的值

详见 `references/form-fields.md`。

---

## 4. `pdf-cli split` 拆出来不会自动 pin

split 一次生成 N 个文件，CLI 把它们都写到 `--output-dir`。**每个文件都要
逐个 `sandbox_get_artifact`**，然后**一次性** pin 整个 file_id 列表：

```python
new_fids = []
for name in produced_names:
    r = sandbox_get_artifact(src_path=f"/workspace/parts/{name}", name=name)
    new_fids.append(r["file_id"])
pin_to_workspace(file_ids=new_fids)   # 一次性 pin 全部
```

不要 N 次调用 `pin_to_workspace(file_ids=[one_id])`——既慢又冗余。

---

## 5. PDF 加密时操作会静默失败

`is_encrypted: true` 的 PDF：
- `read --mode text` / `outline` / `metadata`：能读（pypdf 自动尝试空密码）
- `merge` / `split`：能跑，但会保留加密设置
- `fill-form`：**通常失败**（pypdf 不会自动绕过加密层）

绕路：先 `pdf-cli reformat --input encrypted.pdf --output unlocked.pdf` 重
排成新 PDF（同时去掉加密层），再继续操作。注意 reformat 不是 1:1 复刻，
会按 doc_type 重新设计版式。

---

## 6. `pdf-cli merge` 最少 2 个文件

只有 1 份就报 ValueError。这是有意的——单文件没必要"合并"。如果实在只
有 1 份要复制改名，用 bash 的 `cp` 或者把文件 `sandbox_get_artifact` 直
接登记成新 artifact。

---

## 7. `--pages` 写法别错

`read --mode text --pages 1,3,5` 是逗号分隔（不要空格、不要分号）。也不
支持范围语法 `1-3`——要范围请提前展开成 `1,2,3`。

注意是 1-based，不是 0-based。

---

## 8. spec.content 必须是非空数组

`pdf-cli create` 的 spec 里 `content` 必填且至少 1 个 block。空数组直接
报错。如果只想生成"封面 + 摘要"，加一个 `{"type": "body", "text": ""}` 或
`{"type": "pagebreak"}` 占位。

---

## 9. payload 超过 ~128KB 触发 `Argument list too long`

`--spec '<inline>'` 或 `--fields '<inline>'` 都有 `-file` 兜底变体。超过
几 KB 就先 `Write` 到 `/workspace/<name>.json`，再用 `--spec-file` /
`--fields-file`。

---

## 10. `pdf-cli create` 里图片要走 `--image` 映射

spec 里 `{"type":"image","path":"chart1"}` 的 `path` 字段是**本地 id**，
不是绝对路径。要让 cli 找到真图片，必须额外传
`--image chart1=/workspace/chart1.png`。

两种典型错法：

- **`--image` 给了但 spec 里没引用** → cli 报
  `--image entries not referenced in spec: ['chart1']`，exit 2。
- **spec 里写了 `path:"chart1"` 但没 `--image chart1=...`** → cli 不会预先
  报错，会把字面字符串 `"chart1"` 当成 workdir 下的文件名传给引擎，最终
  在渲染阶段以"FileNotFoundError: chart1"或封面渲染失败抛出。**正确做法
  永远是 spec 里每一个 image/figure 块都对应一条 `--image local_id=/abs/path`**。

PDF 元数据里也是同理：`spec.cover_image` 必须先在 `--image` 里给出一个
绝对路径映射。
