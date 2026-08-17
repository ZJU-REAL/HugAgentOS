---
name: plugin-creator
description: 从零攒一个插件包，或把一个 web 链接上的插件下载下来导入。当用户说"照着这个网页做个插件"、"把我这几个技能打包成插件"、"帮我做一个插件"、"这个链接的插件帮我装上"、或需要把一组配套的技能与工具打成可安装可卸载的整体时，务必使用本技能。它教你插件包的目录结构、plugin.json 怎么写、怎么自检、怎么通过 import_plugin 工具落库。
---

# 插件创建器（Plugin Creator）

插件 = **一组配套的技能 + 工具，打成一个可整体安装、整体卸载的单元**。
本技能教你在**沙箱**里攒出一个结构合法的插件包，自检合格后通过 `import_plugin` 落库。

> 只想做**单个技能**？那不需要插件——用技能管理插件的 `skill-creator` / `register_skill`
> 更直接。插件的价值在于"成套"：多个技能配一个工具服务、或者需要统一装卸的一组能力。

## 一、插件包长什么样

最小可用结构（原生格式）：

```
my-plugin/
├── plugin.json          # 必需：插件清单
├── mcp.json             # 可选：要带工具服务时才有
└── skills/              # 可选：要带技能时才有
    ├── skill-a/
    │   └── SKILL.md
    └── skill-b/
        └── SKILL.md
```

规则：

- **`plugin.json` 必须在包根**（也接受 `.claude-plugin/plugin.json` 布局）。没有它就不是插件包，
  `import_plugin` 会拒绝。
- `skills/` 下**每个子目录一个技能，各自必须有 `SKILL.md`**。没有 SKILL.md 的子目录会被忽略。
- 打包时要 **从插件目录内部打**，别把外层目录名也裹进去：
  `tar -czf /workspace/plugin.tgz -C my-plugin .`

## 二、plugin.json 怎么写

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "一句话说清这个插件给用户带来什么能力。",
  "author": { "name": "……" },
  "extensions": {
    "org.hugagent": {
      "mcp": {
        "my_server": {
          "display_name": "界面上显示的名字",
          "description": "这个工具服务是干什么的。",
          "tools": [
            { "name": "do_something", "description": "……" }
          ]
        }
      }
    }
  }
}
```

要点：

- `name` 就是 slug：**小写字母/数字/连字符**，会被用来生成安装 id，别用中文和空格。
- 标准清单本身**不放展示字段**（显示名、分类、图标属于界面配置，由平台侧维护）；
  平台专有字段一律放进 `extensions["org.hugagent"]`。
- `extensions["org.hugagent"].mcp.<服务名>` 里的 `display_name` / `description` / `tools`
  会覆盖补全到对应的 MCP 服务上，**服务名必须和 `mcp.json` 里的键一致**，否则贴不上去。
- 需要用户填凭据时，在扩展段里写 `required_secrets`（字符串数组），
  安装时平台会据此向用户索要。

字段清单与各种兼容布局详见 `references/manifest-spec.md`。

## 三、工具描述怎么写（最影响好不好用的一步）

`tools[].description` 不是给人看的文档，是**模型判断"该不该调这个工具"的唯一依据**。
写不好，插件装了也不会被用，或者被乱用。

一条好的工具描述包含四件事：

1. **它做什么**（一句话，动词开头）
2. **用户说什么话时该调它**（把真实说法列进去，"用户说'……'时调用"）
3. **参数从哪来**（尤其是 id 类参数：取自哪个工具的返回）
4. **红线**（不可恢复的操作要写"必须先确认"；写操作要写"未拿到成功回执前不要声称已完成"）

反面例子：`"description": "管理数据"`——模型无从判断何时该用。

## 四、完整流程

1. 在沙箱 `/workspace` 里按上面的结构建好目录，写好 `plugin.json`
   （要带技能就再写 `skills/*/SKILL.md`，要带工具就再写 `mcp.json`）。
2. **自检**（务必做，结构不对导入会失败）：
   ```bash
   python3 scripts/validate_plugin.py /workspace/my-plugin
   ```
   退出码 0 才继续。
3. 打包：
   ```bash
   tar -czf /workspace/plugin.tgz -C /workspace/my-plugin .
   ```
4. 调框架自带的 `sandbox_get_artifact("/workspace/plugin.tgz")` 取得 `artifact_id`。
5. 调 `import_plugin(artifact_id)` 落库。
6. 拿到 ✅ 后，把"装进来了哪些技能和工具"讲给用户听。

## 五、从 web 链接安装

用户给一个下载链接时，同一条路：

1. 沙箱里 `curl -L -o /workspace/pkg.zip "<链接>"`
2. 解压到一个目录，**先看清楚里面是什么**（有没有 `plugin.json`）
3. 跑一遍 `validate_plugin.py` 自检
4. 重新打包 → `sandbox_get_artifact` → `import_plugin`

**安全提醒**：来路不明的包不要闭眼导入。至少确认 `plugin.json` 里的 `name`、
`description` 与用户的预期一致，`mcp.json` 里的 url 指向的是可信地址。
发现可疑内容就停下来问用户，不要替用户承担这个风险。

## 六、交付前自检

- [ ] `plugin.json` 在包根，`name` 是合法 slug
- [ ] 每个 `skills/*/` 下都有 `SKILL.md`
- [ ] `mcp.json` 的服务名与扩展段里的 mcp 键一一对应
- [ ] 每个工具的 description 写清了"何时该调 + 参数从哪来 + 红线"
- [ ] `validate_plugin.py` 退出码为 0
- [ ] 是从插件目录**内部**打的包（`tar -C <dir> .`）

拿到 ✅ 之前不要声称已经导入成功。
