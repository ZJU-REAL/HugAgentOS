---
name: skill-creator
description: 从零创建一个新技能，或改进/打包一个已有技能，然后把它存进你自己的私有技能库。当用户说"帮我做一个能做 X 的技能"、"把刚才这套流程/做法固化成一个技能"、"创建/新建一个技能"、"我想要一个自动做 Y 的技能"、"把这个技能改一下再存起来"、或给出一个技能包的下载链接要求安装时，务必使用本技能。它教你如何撰写规范的 SKILL.md、组织资源文件、自检，并通过 register_skill 工具把成品落库成私有技能。
---

# 技能创建器（Skill Creator）

本技能教你在**沙箱**里创作一个新技能，自检合格后**登记进当前用户的私有技能库**，之后该用户在对话中
就能直接用上这个新技能。

## 先搞清"技能长什么样"

一个技能就是一个目录，核心是一份 `SKILL.md`（YAML frontmatter + Markdown 正文），可选带
`scripts/`（脚本）、`references/`（需要时才读的文档）、`assets/`（产出用到的模板等）。

frontmatter **必须**有两个字段：

```yaml
---
name: my-skill              # 小写字母/数字/下划线/连字符，1–63 字符
description: 一句话说清"做什么 + 什么时候该触发"（技能被唤起的主要依据）。
---
```

- 结构、三层渐进式披露、按变体组织 → 详见 **`references/skill-anatomy.md`**。
- description 怎么写才容易触发、正文写作风格、举例与安全红线 → 详见 **`references/writing-guide.md`**。
  开始写之前建议先读这两份。

## 创作流程（在沙箱里做）

1. **厘清意图**：这个技能要让智能体能做什么？用户会在什么场景、说什么话时触发它？期望产出是什么？
   如果当前对话里已经有一套用户认可的做法（"把刚才这套固化成技能"），直接从对话里提炼。

2. **脚手架**：用打包的脚本生成目录骨架（省得手敲 frontmatter）：

   ```bash
   # 本技能目录是系统提示词中 skill-creator 对应的 <dir>，也是你刚才读取的 SKILL.md 父目录。
   # 不要假设目录名固定为 skill-creator；安装后的技能 id 会被命名空间化。
   export SKILL_CREATOR_DIR="/workspace/skills/<当前 skill-creator 实际目录名>"
   cd "$SKILL_CREATOR_DIR"
   python3 scripts/init_skill.py my-skill      # → /workspace/my-skill/SKILL.md 模板
   ```

3. **写正文**：用 Write/Edit 编辑 `/workspace/my-skill/SKILL.md`，按需加 `references/`、`scripts/`、
   `assets/`。参照 `references/writing-guide.md` 的风格（祈使句、解释原因、别过拟合、把重复逻辑写成脚本）。

4. **自检**（重要，能提前拦下落库失败）：

   ```bash
   python3 "$SKILL_CREATOR_DIR/scripts/quick_validate.py" /workspace/my-skill
   ```

   校验规则对齐后端技能引擎（name 格式、description 必填等）。通过再往下走。若技能里带了脚本，也在
   沙箱里实跑一遍确认能用。

5. **打包成 tar**（`-C <目录> .` 让 SKILL.md 落在包根）：

   ```bash
   tar -czf /workspace/my-skill.tgz -C /workspace/my-skill .
   ```

6. **推进产物库**：调框架自带的 **`sandbox_get_artifact`** 把 tar 取回后端产物库，拿到 **artifact_id**：

   ```
   sandbox_get_artifact(src_path="/workspace/my-skill.tgz")
   ```

7. **落库**：把 artifact_id 传给 **`register_skill`**：

   ```
   register_skill(artifact_id="<上一步返回的 id>")
   ```

   成功后这个技能就进了当前用户的私有技能库，**下一轮对话即可直接使用**。

## 从一个 web 链接安装技能 / 插件

用户给出一个技能包（或插件包）的下载链接时：

1. 在沙箱里下载并解压（沙箱出网受控，能规避 SSRF 风险）。如果还没设置 `SKILL_CREATOR_DIR`，
   先按上文脚手架步骤中的方法设为当前技能的实际 `<dir>`：

   ```bash
   cd /workspace && curl -fsSL "<URL>" -o pkg.tgz
   mkdir -p pkg && tar -xzf pkg.tgz -C pkg   # .zip 用 unzip pkg.zip -d pkg
   ls -R pkg
   ```

2. **自检**：确认包里有 `SKILL.md`（技能）或 `plugin.json`（插件）；
   `python3 "$SKILL_CREATOR_DIR/scripts/quick_validate.py" /workspace/pkg`
   看技能合不合法；扫一眼有没有可疑脚本，向用户复述这个包大概会做什么，让用户确认。
3. 重新打包并落库（`register_skill` 会自动识别：含 `plugin.json` 按插件导入，否则按技能落库）：

   ```bash
   tar -czf /workspace/pkg.tgz -C pkg .
   ```
   然后 `sandbox_get_artifact("/workspace/pkg.tgz")` → `register_skill(artifact_id=...)`。

## 管理已有技能

这些动作直接调技能管理工具，不用进沙箱：

- **看我有哪些技能**：`list_my_skills()`。
- **申请上架到市场**（和别人分享）：`submit_to_marketplace(skill_id, category, summary, note)`——进管理员
  审核队列，通过后其他用户可安装。`category` 必须从这 8 个固定分类里挑最贴切的一个：写作助手 /
  文档处理 / 数据分析 / 政策产业 / 营销创意 / 法务合规 / 办公效率 / 研发效率。
- **删除我的技能**：`delete_skill(skill_ref)`（传 skill_id 或名称；匹配到多个会让你确认，别猜删）。
- **从市场找现成的装上**：`search_marketplace(query)` → `install_from_marketplace(slug)`。

## 改进一个已有技能

用户要改一个已有技能而不是新建：先 `list_my_skills()` 找到它，在沙箱里重建该技能目录、按反馈改
`SKILL.md`（保持 `name` 不变），再走同样的"自检 → 打包 → sandbox_get_artifact → register_skill"流程——
`register_skill` 对同名技能会**原地更新**，不会重复创建。

---

**核心回路**：厘清意图 → init_skill 脚手架 → 写 SKILL.md → quick_validate 自检 → 打包 tar →
sandbox_get_artifact 拿 artifact_id → register_skill 落库。
