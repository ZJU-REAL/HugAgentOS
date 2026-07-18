---
name: excel-editing
display_name: Excel 表格作业手册
description: "**生成、编辑、修改、套公式、加图表或导出 .xlsx Excel 工作簿 / 财务模型 / 报表 / 数据表 / 预算 / 名册 / 排期** 时使用。既覆盖从零建表（表头样式·列宽·合并单元格），也覆盖：按公式建模（同比·毛利率·三年滚动预测等活公式，而非写死数值）、批量改单元格、加 sheet、插入原生图表、给已有工作簿加列或加汇总行。把 Excel/工作簿转成 PDF 也走本技能（.xlsx 为主产物）。仅当用户要 .xlsx 产物、或改/转既有 Excel 时触发；查数据库、读表格做总结、写 SQL、把数据画成图片、出纯文字数据分析报告、翻译表格内容 时不要触发。"
tags: xlsx, excel, office, spreadsheet, cli
---

# Excel 表格作业手册

本 skill 是处理 .xlsx 文件的**唯一权威入口**。所有 Excel 能力都收敛成单一 CLI——**`excel-cli`**——的 5 个子命令。

**目标只有一个**：每次操作 .xlsx 都选对子命令、写对参数。

---

## 进入本技能前先停一秒：别走偏

只有在用户明确需要 **Excel / xlsx / .xlsx / 工作簿 / spreadsheet** 文件产物，
或要求生成、编辑、加 sheet、改单元格、建模型、导出 PDF 时，才进入这份 SKILL。
用户只是说"查一下数据"、"看看这个表"、"统计一下指标"时，除非同时明确要求交付
Excel/.xlsx 成稿，否则应改用 `query_database`、`retrieve_dataset_content` 或
其它更匹配的工具。

一旦确认这是 Excel 产物任务，你的最终产物必须是 `.xlsx`：用 `excel-cli create` 或
`excel-cli edit` 产出，并 `pin_to_workspace` 交付。下面几件事看上去能"完成任务"，
实际是失败：

1. **`Write` 写 CSV 不可替代 `excel-cli create`**。用户要 .xlsx 时不能写一个
   `.csv` 兜底。csv 不带表头样式、不带列宽、不带公式——**输出格式不符合用户请求
   就是失败**。看到用户消息里明确要 Excel/.xlsx 文件——心里默念一句"产物必须是
   .xlsx"，再开始动手。
2. **不要把 markdown 表格塞回 chat 让用户"自己复制粘贴到 Excel"**。这是 hard
   fail。如果你已经写好了表格内容，**正确做法是**用 `excel-cli create --mode
   workbook --sheets '[{...}]'` 把它生成成 .xlsx，再 `sandbox_get_artifact` +
   `pin_to_workspace` 交付。从来不是"贴在回答里收工"。
3. **不要拿 `list_myspace_files` 返回的旧 artifact 当作"新生成的工作簿"再 pin 一遍。**
   `sandbox_put_artifact` 是把"用户**这一轮上传的**原始 xlsx"送进沙盒；它不是
   artifact 复制器、不是"我把以前生成过的同名文件再用一次"的捷径。新工作簿永远走
   `excel-cli create` 从零生成。
4. **`pdf-cli create` / `word-cli create` 不能替代**。用户要 .xlsx 时不能直接出
   .pdf 或 .docx 兜底。同时要 .xlsx + .pdf 时，**.xlsx 是主产物，.pdf 是由
   excel-cli convert --to pdf 从 xlsx 转出来的副产物**，不能跳过 .xlsx 直接出
   .pdf。
5. **改完中间稿（`--patches` JSON / `--sheets` JSON / 数据 spec）就停手，忘了
   重新跑 `excel-cli` 出最终 .xlsx**。常见现场：你把 patches 列表改了一轮，回复
   就说"已经把 X 列改成 Y，patches 保存在 `/workspace/patches.json`"——然后收尾。
   **这些中间 JSON 不是用户要的东西，`.xlsx` 才是**。中间稿每改一轮都得重新跑
   `excel-cli create` / `excel-cli edit` 把新版 `.xlsx` 产出来，再走
   `sandbox_get_artifact` + `pin_to_workspace` 把最新文件推给用户。stop 在
   patches/sheets JSON 上 = 这一轮没交付。
6. **`excel-cli` 跑完了，只在回复里说"工作簿已生成，路径 `/workspace/xxx.xlsx`"
   就收尾**。沙盒磁盘对用户**完全是黑盒**——你告诉他"沙盒里有个文件在这里"，
   等同于没交付：用户的对话区 / 我的空间不会自动出现这个文件，他没办法点开、
   下载、看里面的数据。CLI 跑完后必须接 `sandbox_get_artifact` 拿 `file_id`，
   再 `pin_to_workspace` 钉到工作区，用户那边才会看到 .xlsx 卡片。

什么时候**应该**离开这份 SKILL：用户明确说要 **Word / 报告 / 公文** → 那应该走
`word-editing` 技能。用户要 **PPT / 演示文稿** → 走 `ppt-design`。用户要 **PDF
文档（不需要 Excel）** → 走 `pdf-editing` 的 `pdf-cli create` / `pdf-cli reformat`。
如果不小心被 load
进了这份 SKILL 但用户其实想要别的格式，先承认走错了再换工具，不要勉强用 excel-cli。

---

## 运行模型（必须先理解这个，再谈调命令）

LLM 没有 `excel_*` 工具可以直接调。每次 Excel 操作是 **3 步组合 + 1 步交付**：

```
# Step 1：如果用户上传过原始 xlsx（有 file_id），把它送进沙盒
sandbox_put_artifact(artifact_id="<原始 xlsx 的 file_id>",
                     dest_path="/workspace/in.xlsx")
  → {"ok": true, "artifact_id": "...", "dest_path": "/workspace/in.xlsx"}
# 注意：`put` = put INTO sandbox（沙盒是终点）。从零创建的场景跳过这一步。

# Step 2：bash 跑 excel-cli
bash("excel-cli <subcmd> --input /workspace/in.xlsx \
                          --output /workspace/out.xlsx \
                          <子命令特定参数>")
  → stdout 返回 JSON 结果（{"ok": bool, "meta": ...}）

# Step 3：把沙盒里的成稿提取出来登记成 artifact
sandbox_get_artifact(src_path="/workspace/out.xlsx",
                     name="数据汇总.xlsx")
  → {"ok": true, "file_id": "<新 file_id>", "url": "/files/...", ...}
# 注意：`get` = get OUT of sandbox（沙盒是源）。这一步返回新的 file_id。

# Step 4（必做）：交付给用户
pin_to_workspace(file_ids=["<新 file_id>"])
  → 文件出现在对话区/我的空间。没 pin = 用户看不到。
```

读取类子命令（`read` 的 summary/sheet/validate 模式）只有第 1、2 步，没有第 3、4
步（没有新文件交付）。

---

## 调用方式：只敲 `excel-cli`，别去 `python` 跑 `scripts/` 里的文件

`excel-cli` 已经装在沙盒 PATH 上（`/usr/local/bin/excel-cli`），当成普通命令直接敲：

```bash
bash("excel-cli create --mode workbook --output /workspace/out.xlsx --sheets-file /workspace/data.json")
```

`scripts/` 目录下的 `_common.py` / `cli.py` / `create.py` 等是**实现细节，不是入口**。
**绝不要**用 `python scripts/xxx.py` 去跑它们：

- ❌ `python scripts/_common.py excel-cli create …` —— `_common.py` 是共享库，
  跑它**什么都不会发生**：它只是定义函数然后退出，不解析参数、不产文件。
  （已加保护：现在会报 `NotAnEntryPoint` 并回显应该执行的正确命令。）
- ❌ `python scripts/create.py …` —— 绕过了 PATH 解析和 Python 解释器挑选逻辑，
  容易撞 `ModuleNotFoundError`。
- ✅ `excel-cli create …` —— 唯一正确入口。

只有当 `excel-cli` 真的 `command not found` / 退出码 127 时，才退到
`python /workspace/skills/excel-editing/scripts/cli.py <subcmd> …`（用 `cli.py`，
不是 `_common.py`）。

**自检**：每次创建/编辑后，下一步 `sandbox_get_artifact` 之前，务必确认 stdout 是
`{"ok": true, ...}`。命令"成功了但没产出文件"——比如 stdout 为空、exit 0 但
`{"ok": ...}` 缺失——一律当失败处理，先查调用方式对不对，**不要原样重试**。

---

## 5 个子命令速查（详见 references/cli-commands.md）

| 子命令 | 用途 | 典型场景 |
|---|---|---|
| `excel-cli read --mode summary` | 工作簿概览（sheet 名/维度/表头/样例行） | 用户上传 xlsx 后的"第一眼" |
| `excel-cli read --mode sheet` | 单 sheet 的单元格值（可指定 range） | 提取某 sheet 数据做后续分析 |
| `excel-cli read --mode validate` | 公式静态校验（#REF! / 跨表引用） | 用户问"这工作簿公式有没有错" |
| `excel-cli create --mode workbook` | 数据表新建（headers + rows + 列宽） | 把表格数据落成 .xlsx |
| `excel-cli create --mode model` | 财务/分析模型（公式 + 角色样式） | 多 sheet 含公式的成熟模型 |
| `excel-cli edit --patches` | 批量编辑（**字节保留**：保 VBA/数据透视/格式） | 改公式、插行、加列、改 sheet 名 |
| `excel-cli edit --set-cells` | 单元格写入（openpyxl 回填，可能丢宏） | 单点改值/公式，宿主无 VBA |
| `excel-cli edit --add-sheet` | 追加新 sheet | 加 Q4 sheet、参数表 |
| `excel-cli edit --add-chart` | 插入原生图表（bar/line/pie） | 在数据 sheet 旁加柱状图 |
| `excel-cli save` | 另存到最终文件名 | 命名好交付名（"年度报表.xlsx"） |
| `excel-cli convert --to pdf` | xlsx → pdf（LibreOffice） | 需要 PDF 副本时 |

> **优先级铁律**：编辑既有 .xlsx 优先用 `--patches`（字节保留，不会污染 VBA/数据
> 透视/条件格式）；非要改的内容 `--patches` 表达不了，再退到 `--set-cells`。
> 永远不要把 markdown/CSV 字符串塞给模型让它"自己想象成 Excel"。

---

## 渐进式加载：什么时候去看哪份 reference

- **第一次写 `excel-cli edit --patches`** → 必读 `references/apply-edits-ops.md`，
  里面是 7 个 patch op（set_cell / fix_formula / replace_text / insert_row /
  add_column / rename_sheet / delete_row）的卡片，每个含必填+可选 kwargs 与正反例。
- **没把握选哪个子命令** → 看 `references/cli-commands.md`，所有子命令的完整
  argparse 参数 + 返回结构都在这里。
- **要查一份完整配方**（如"用户传 xlsx → 我要插一列计算列 → 导出 PDF"）→ 看
  `references/workflows.md`，6~8 个端到端例子。
- **报错 / 行为反直觉** → 看 `references/pitfalls.md`，含 4 个 edit 引擎
  （`--patches` / `--set-cells` / `--add-sheet` / `--add-chart`）不能互相组合、
  `--set-cells` 会丢 VBA 等高频踩坑。

---

## 大 payload 兜底（永远记得）

`excel-cli edit --patches '[...]'` / `excel-cli create --sheets '[...]'` 的
JSON 数组很容易很大（一个 100 行的财务模型 spec 轻松破 20KB）。bash 命令行
有 ~128KB 的 `Argument list too long` 上限，触发就直接报错。

**永远的兜底**：先 `Write` 把 JSON 写到 `/workspace/<name>.json`，再用 `-file`
变体：

```bash
# 推荐写法（payload 大就这么写，别等出错才换）
Write(file_path="/workspace/patches.json", content="[{...}, {...}]")
bash("excel-cli edit --input /workspace/in.xlsx \\
                     --output /workspace/out.xlsx \\
                     --patches-file /workspace/patches.json")
```

`create --sheets-file` / `create --spec-file` / `edit --set-cells-file` /
`edit --add-sheet-file` / `edit --add-chart-file` 都有对应的 `-file` 变体。

---

## 一句话自检（交付前）

每次回复用户之前，按这个列表 5 秒过一遍。任何一项没满足 = 这一轮**没交付**，
回去补，不要发：

- 用户要的是 **.xlsx**，我交付的也是 .xlsx 吗？（不是偷偷换成了 .csv /
  .pdf / .docx / 「贴在回答里的 markdown 表格让用户自己复制粘贴」？）
- 是不是用 `excel-cli create`（从零）或 `excel-cli edit`（改既有）真的产出过
  `/workspace/<...>.xlsx` 这个文件？（没跑过这条命令 = 没有 .xlsx，必返工）
- 中途如果改过 patches / sheets / data spec 这类中间稿，**改完之后有没有再跑
  一次 `excel-cli` 把最新版 .xlsx 重新生成出来**？（停在改完 patches/JSON =
  没交付）
- `sandbox_get_artifact(src_path="/workspace/<...>.xlsx", name="...")` 跑了，
  拿到了 `file_id` 了吗？
- **`pin_to_workspace(file_ids=["<file_id>"])` 调了吗？**
  —— 这一步不可省：没 pin，文件只是后端 artifact 存储里的一条匿名记录，
  用户在对话区 / 我的空间里**完全看不到**，等于这一轮任务从没存在。
- 用户同时要 .xlsx + .pdf 时：两个 file_id 都进同一次
  `pin_to_workspace(file_ids=[...])` 调用了吗？（一次 pin 多个，别多次 pin）
- 用户指定了"放到我的空间里 XX 文件夹"：`name=` 参数填了贴切的中文文件名
  吗？（不是 `out.xlsx` / `draft.xlsx` 这种）

---

## 一句话回顾

> 看见 Excel/.xlsx 任务 → 进沙盒 → 选对 `excel-cli <subcmd>` → 出文件 → pin。
> 编辑既有工作簿优先 `--patches`（字节保留）；新建优先 `create --mode workbook`
> 或 `--mode model`；要 PDF 副本最后一步 `convert --to pdf`。
