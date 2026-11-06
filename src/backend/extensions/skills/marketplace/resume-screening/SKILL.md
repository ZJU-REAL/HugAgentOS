---
name: resume-screening
description: 批量简历筛选：按岗位 JD 制定带权重的评分标准，批量解析 PDF/Word 简历，逐份打分并附简历原文证据，脚本核验证据真实性、统一加权、执行硬性条件一票否决，输出排名 Excel 和面试建议。当用户说"帮我筛简历""这批简历哪些值得面试""按 JD 给简历打分排名""对比这几位候选人"时使用。单份简历的修改润色不用本技能。
---

# 批量简历筛选

把一批简历按同一把尺子打分排名。模型负责读懂简历、给出判断；脚本负责模型不可靠的部分：
核验证据是否真实出现在简历里、统一加权计算、硬性条件一票否决、输出表格。

下文 `scripts/` 指本技能目录 `{baseDir}/scripts/`。

## 第 1 步：确定评分标准（先和用户确认，再开始打分）

根据用户给的 JD 或岗位描述，起草 `rubric.json`：

```json
{
  "position": "高级后端工程师",
  "pass_line": 65,
  "criteria": [
    {"id": "lang", "name": "Python/Go 后端开发", "weight": 30, "must_have": true, "min_score": 3,
     "guide": "5=主导过高并发服务；3=独立负责过模块；1=只做过脚本"},
    {"id": "db", "name": "数据库与缓存", "weight": 20, "guide": "..."},
    {"id": "years", "name": "相关工作年限", "weight": 15, "must_have": true, "min_score": 2,
     "guide": "5=8年以上；3=3-5年；1=1年以内"},
    {"id": "domain", "name": "行业经验", "weight": 15, "guide": "..."},
    {"id": "edu", "name": "学历与专业", "weight": 10, "guide": "..."},
    {"id": "stability", "name": "稳定性", "weight": 10, "guide": "..."}
  ]
}
```

- 4–8 条标准，权重按岗位实际看重程度分配；每条写清 0–5 分各档的含义（`guide`）。
- 只把真正不可妥协的条件设为 `must_have`，并写明 `min_score`。
- **把标准表格展示给用户确认后再继续**。用户没给 JD 时，先问清岗位的核心职责和硬性要求。
- 不得把性别、年龄、婚育、民族、籍贯、照片外貌等作为评分项，即使用户要求也要说明这有就业歧视风险并拒绝。

## 第 2 步：批量解析简历

```bash
python3 scripts/parse_resumes.py <简历目录或文件...> -o candidates.jsonl
```

输出每份简历一行：`candidate_id`、`file`、手机、邮箱、最高学历、估算工作年限、`needs_ocr` 和全文 `text`。
`needs_ocr: true` 的是扫描件或图片，先用 OCR 工具（如已安装的百度 OCR）识别成文字、存成 .txt 后重新解析。

## 第 3 步：逐份打分

逐份阅读 `candidates.jsonl` 的 `text`，按 rubric 打分，写入 `scores.json`：

```json
[
  {"candidate_id": "3fa2c1d0", "name": "张三",
   "scores": [
     {"criterion": "lang", "score": 4, "evidence": "负责订单服务重构，使用 Go 将 P99 延迟从 800ms 降到 120ms"},
     {"criterion": "years", "score": 3, "evidence": "2019.07 - 至今 某某科技 后端工程师"}
   ],
   "summary": "后端扎实，有高并发经验；缺少金融行业背景。"}
]
```

- `evidence` **必须是简历原文的连续摘录**（可省略标点差异），不要改写、不要概括。找不到证据就给 0 分、evidence 留空。
- 每条标准都要打分；只看简历写了什么，不脑补。
- 简历多时分批读，每批写完再继续，避免遗漏。

## 第 4 步：汇总排名

```bash
python3 scripts/score.py --rubric rubric.json --candidates candidates.jsonl \
    --scores scores.json -o 简历筛选结果.xlsx --mask-contact
```

- 输出 `简历筛选结果.xlsx`（"排名"和"逐项明细"两个工作表）以及同名 `.md` 摘要。
- 证据在原文里找不到的项会被改为 0 分，并在"需人工复核"列标出。**把这些标记如实告诉用户**，不要替脚本解释掉。
- 对外分享结果时加 `--mask-contact` 隐藏联系方式。

## 第 5 步：交付

向用户汇报：
1. 建议面试名单（姓名、总分、一句话亮点）；
2. 因硬性条件被排除的人数和原因；
3. 需要人工复核的条目；
4. 对排名前几位，每人给 3 个针对简历薄弱点的面试问题。

结论只是辅助筛选，最终录用决定由用户做出。
