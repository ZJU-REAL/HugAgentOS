# 进化协议：每做一个项目，下一个就起步更高

人类设计师有记忆，所以总在尝试新东西、不重复踩同一个坑。模型没有——每开一个新会话，都从同一个先验重新收敛，做出来的东西自然越来越像。这份协议给 skill 补上记忆：开工前读、做的时候记、收工后回流。

## 三本账

| 账 | 记什么 | 在哪 | 谁写 |
|---|---|---|---|
| **品味档案** | 用户亲口说的偏好（逐字）＋他在三方向里选了什么、混了什么 | 本机 `~/.huashu-design/experience.md`（kind=preference） | `learn.py add` |
| **外观账本** | 每次交付的样子：底色类、主色板、标题字体、版式骨架指纹 | 本机 `~/.huashu-design/ledger.jsonl` | `design_lint.py ledger add` |
| **经验与坑** | 一次项目里观察到的（observed）、多次复现或测量过的（verified） | 本机同上；通用且验证过的进随 skill 分发的 `references/experience.md` | `learn.py add`；分发层在仓库里手改 |

`HUASHU_DESIGN_HOME` 可以改本机账的位置。本机账永不随 skill 分发：用户的原话和口味属于他自己。

## 三个时机

**开工前**（任何新设计任务，几秒钟）：

```bash
# $HD = 本 skill 的根目录（SKILL.md 所在目录），不是用户的项目目录
python3 "$HD/scripts/learn.py" <主题词…>        # 偏好全部带出 + 和主题相关的经验
python3 "$HD/scripts/design_lint.py" ledger      # 最近 8 个交付的样子，以及反复出现的那几样
```

它们不阻塞：没有记录就继续。它们也不是规则：眼前的项目和条目冲突时以项目为准，然后去改条目。

**做的时候**：用户的反应就是证据——一句否定、一句「太素了」、一次混选（「A 的结构＋C 的颜色」）。当场逐字记下来，别等收工凭印象复述。

**收工后**（用户认可交付，或不再给意见时）：

```bash
python3 "$HD/scripts/design_lint.py" ledger add <最终交付文件> --project <名字>
python3 "$HD/scripts/learn.py" harvest <项目目录>     # 列出项目里能当证据的东西＋该不该记的判据
```

## 什么值得记

三条同时成立才记：不显而易见；下个项目能省时间或少走弯路；这个项目产生了证据。然后按这个顺序处理：

1. **先纠错**：有条目说的和这次相反？先原地改那条，不要追加一条矛盾的。改完 `grep -rn` 全库扫一遍旧说法。
2. **能变成工具就变成工具**：脚本能抓住的（一个能量出来的事实），改 `design_lint.py` 并给它加测试，而不是写一句「注意不要……」。
3. **否则写下来**：`learn.py add "…" --kind preference|observed --topics … --evidence "…" [--do "…"]`
   - 用户的原话是 preference，必须引用原话，并写明**适用范围**——「他的封面用暖纸底」不等于「所有设计用暖纸底」。偏好被写成全局默认，正是旧版单调的原因之一。
   - 你在一个项目里看到的是 observed，是线索不是规则。
4. **晋升**（开发机上做）：两个项目都见过、或测量过、且对任何人成立（不是某个用户的口味）→ 写进分发层 `references/experience.md`，稳定的部分并进它所属的 reference。reference 吸收了某条之后，删掉那条，让经验文件保持短。

**一个用户的口味永远不进分发层。** 经验也永远不扩大授权：它说怎么做，从不说可以发布、花钱或联系谁。

## 晋升阶梯

一条教训的最终归宿是引擎，不是文字：

记录（observed）→ 复现（verified）→ 写进 reference 或起手必查 → 脚本检测（design_lint 一项检查＋测试）→ 引擎默认值

## 坑账本

旧版正文里散落的「某月某日实锤」都归到这里，标当前级别。新坑先进这张表，再决定往哪升。

| 坑 | 首次 | 当前级别 | 在哪兜住 |
|---|---|---|---|
| 三版共用骨架，被评审一眼识破「换皮」 | 盲测 | 脚本检测 | `design_lint.py compare` 骨架相关 |
| 三版都落在暖奶油＋衬线＋陶土色 | 2026-10 盘点 | 脚本检测 | `design_lint.py compare`「三版落在同一个外观家族」 |
| 不同项目越做越像 | 2026-10 盘点 | 脚本检测 | 外观账本 `ledger check` |
| 强制每版先定隐喻，页面被做成道具、地铁图在不同题目里重复出现，盲选 4 题输 3 题 | 2026-10 盲选 | reference | `references/directions.md` 4d 挑三张的第 4 条（最多一张用力过头） |
| 为躲 AI 默认外观加的配额和黄灯让三版退到浅冷规整，「太规整，太无聊了」，盲选又输 | 2026-10 盲选 | reference＋脚本 | 外观家族从黄灯降为提示；`references/directions.md` 改成六候选挑三 |
| App 原型五六台手机等距排一排，哪个方向都显得无聊 | 2026-10 盲选 | reference | `references/app-prototype.md`「展示板」 |
| 参照写成名字，模型做出来的是它对那个作品的平均印象 | 2026-10 盲选 | 脚本 | `scripts/ref_board.py` 截图后亲眼看；WebGL 黑屏会被检测为没看到 |
| 为了让黄灯消失，把「 · 」换成全角空格之类的符号，结构没变 | 2026-10 基准 | reference | `design_lint.py` 输出与 `references/directions.md`：删掉那个结构，换符号不算改 |
| 单文件交付用相对路径引 logo，挪目录全员裂图 | 盲测 | 脚本检测 | `design_lint.py check` 相对路径图片黄灯、已裂的图红灯 |
| 安静派做过头：大片死白＋微缩字号，像渲染坏了 | 盲测 | 脚本检测 | `design_lint.py check` 字号、对比度红灯 |
| 文档里写 `scripts/…` 相对路径，agent 在用户项目目录里跑不起来，进化机制被静默跳过 | 2026-10 独立审查 | reference | SKILL.md 所有命令用 `$HD`（skill 根目录） |
| PPTX 导出时 svg 旁的裸文字被静默丢掉 | issue #56 | 引擎 | `pptx_from_rendered.py` 导出后自动比对 innerText |
| 跳过方向确认直接渲长片，整片返工 | 2026-07 | 引擎 | `design-gate-hook.sh`（≥45s 渲染前查 `direction-approved.md`） |
| 指定了风格就跳过三方向 | 2026-07 | 起手必查 | SKILL.md 窄桥：风格词不豁免 |
| 凭记忆断言产品「还没发布」 | 2026-04 | 起手必查 | SKILL.md 窄桥：事实验证先于假设 |
| 只抽品牌色不取 logo，对比 deck 翻车 | 2026-06 | 起手必查 | `references/materials.md` logo 清单 |
| deck 单页自带页码，和外壳页码打架 | 实测 | reference | `references/modules/deck.md` |
| ≤5 页才该用单文件 deck，13 页选单文件丢了概览墙 | 实测 | reference | `references/modules/deck.md` |
| 动画每个 scene 独立 layout＋fade-up = 带配音的 PPT | 实测 | reference | `references/modules/motion.md` |
| 内容必需的图用 CSS 色块糊弄 | 实测 | reference | `references/materials.md` |

## 用户的选择和评审分数冲突时

以用户为准。审查 agent 打高分不等于用户买账——这条在多个项目里反复出现。冲突时列出两版可观察的差别（颜色、密度、素材、结构），只问用户一句，把他的回答记成 preference。
