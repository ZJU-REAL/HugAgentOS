# excel-editing 高频踩坑

碰到反直觉的行为先翻这里。

---

## 1. 4 个 edit 引擎不能互相组合

`excel-cli edit` 一次只能选一个：`--patches` / `--set-cells` / `--add-sheet` /
`--add-chart`。多选会报 `multiple edit engines specified`。

**为什么不让混**：它们用的写入路径完全不一样：
- `--patches` → 字节保留 unpack/pack
- `--set-cells` / `--add-chart` → openpyxl round-trip
- `--add-sheet` → openpyxl round-trip

放一起会让某些 op 的中间态丢失。要做多件事就：

```
方案 A（推荐）：把它们装到一个 --patches 数组里（set_cell / add_column / 等覆盖 80% 场景）
方案 B：分多次 edit，每次输入是上一次的输出
        excel-cli edit ... -o /workspace/v1.xlsx --add-sheet '...'
        excel-cli edit -i /workspace/v1.xlsx -o /workspace/v2.xlsx --add-chart '...'
```

---

## 2. `--set-cells` 会丢 VBA / 数据透视

openpyxl 不读 VBA、不读 PivotCache、不读部分高级条件格式。一旦走 `--set-cells`
或 `--add-chart` 或 `--add-sheet`，这些会消失。

**自检**：源文件是不是 .xlsm（含宏），有没有数据透视，有没有复杂条件格式？有就
**只用 `--patches`**——`--patches` 是字节保留的，安全。

---

## 3. `--patches` 的 op 串在一起，后面 op 看得到前面 op 的结果

`patches` 数组里 op 是**串行**执行，**后续 op 的索引按变化后的状态算**：

- 先 `insert_row at=5` → 原第 5 行变成第 6 行
- 接着 `set_cell sheet=X cell=B5` → 写的是**新插入的那一行**

所以**先想清楚**：你要写的目标行，是相对于原始文件，还是相对于已经插过几行的文
件？通常想要的是相对于新状态，op 数组顺序就按此摆。

---

## 4. add_column 会覆盖列里已有内容

`add_column` 不检查目标列是否为空，直接写入。先 `read --mode summary` 看一眼空
列在哪。

如果不小心覆盖了，没有 undo——重新 `sandbox_put_artifact` 拿原始文件再做。

---

## 5. `rename_sheet` 默认会改公式里的引用

`update_formulas` 默认为 `true`。把 `Sheet1!A1` 自动改成 `新名!A1`。这通常正是
你想要的。

**只有**当你**就是想保留旧引用**（极少见）时才 `update_formulas: false`，否则
不要碰这个字段。

---

## 6. payload 超过 ~128KB 会触发 bash `Argument list too long`

所有 `--xxxx '<json>'` 都有 `--xxxx-file <path>` 变体。超过几 KB 就用文件：

```python
Write(file_path="/workspace/patches.json", content=json.dumps(big_patches_list))
bash("excel-cli edit --input ... --output ... --patches-file /workspace/patches.json")
```

---

## 7. `excel-cli convert --to pdf` 失败大概率是 LibreOffice 缺 JRE

底层走 `soffice --headless --convert-to pdf`。Calc（xlsx 引擎）需要 JRE，缺
`JAVA_HOME` 会**静默**失败（exit code 仍可能是 0，但输出文件不存在）。

技能脚本里我们已经自动找 `/usr/lib/jvm/` 下的 JRE 并设环境变量，正常情况下不会
踩这个坑。若仍报错，先 `bash("which soffice && ls /usr/lib/jvm/")` 排查镜像。

---

## 8. `read --mode sheet` 默认 max-rows=1000

防止 LLM 上下文爆炸——超过 1000 行会被截断。大表先 `--mode summary` 看维度，
再分段读 `--range A1:E1000`、`--range A1001:E2000` 等。

---

## 9. `excel-cli save` 只是改名，不会"保存"任何东西

`save` 实质就是 `cp`，**不会**修改单元格内容、不会重算公式。如果用户说"保存这
个 Excel"，95% 概率他想的是：① 给文件起个交付名；② 让文件出现在「我的空间」。
对应做法是 `save` + `sandbox_get_artifact` + `pin_to_workspace`。

千万**不要**把 `save` 当成"提交修改"的语义——所有真正的修改都已经在前一步
`edit` / `create` 里落到文件系统里了。

---

## 10. 直接给用户 markdown 表格当答复 = 任务失败

用户明确要 Excel/.xlsx 时，markdown 表格塞回 chat **不算**交付。务必：

```
markdown 想法 → Write 成 /workspace/draft.md（可选）
            → excel-cli create --mode workbook --sheets '[...]'
            → sandbox_get_artifact + pin_to_workspace
```

最后用户能下载到 .xlsx 才算完成。

---

## 11. `python scripts/_common.py …` 静默成功但不产文件

只能敲 `excel-cli <subcmd>`。`scripts/` 里的文件不是入口，最坑的是 `_common.py`：
它是共享库，`python scripts/_common.py excel-cli create …` 跑下去**只是 import
模块、定义函数、exit 0**——参数完全没解析，文件根本没生成。

旧版本下这会变成最难排查的失败：bash 返回 `exit_code 0`、stdout/stderr 全空，
你以为成功了，去 `sandbox_get_artifact` 却 404，然后反复重试同一条命令——这正是
trace `6896082e` 里模型空转十几轮的根因。

现在 `_common.py` 被直接执行时会报 `{"ok": false, "error": {"type":
"NotAnEntryPoint", ...}}` 并在 `suggested_command` 字段里给出正确命令——照着敲即可。

**判据**：`excel-cli create/edit` 之后，stdout 必须是 `{"ok": true, "meta": …}`。
凡是 stdout 为空、或没有 `"ok"` 字段，一律视为失败：先核对是不是用 `excel-cli`
入口调用的，而不是把同一条错命令再跑一遍。
