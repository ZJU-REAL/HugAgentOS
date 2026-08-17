# plugin.json / mcp.json 字段速查

## 兼容的三种包格式

导入器会自动识别，你只需按其中一种组织：

| 格式 | 清单位置 | 说明 |
| --- | --- | --- |
| **原生**（推荐） | 包根 `plugin.json` | Agent Plugins 标准包，平台字段放 `extensions["org.hugagent"]` |
| Claude Code | `.claude-plugin/plugin.json` | 兼容布局 |
| Codex | 其约定的清单文件 | 兼容布局，图标可从 `interface.composerIcon` 读 |

新做的包一律用原生格式。

## plugin.json 字段

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `name` | ✅ | 就是 slug。小写字母/数字/下划线/连字符，1–100 字符。安装 id 由它生成 |
| `version` | 建议 | 缺省按 `1.0.0` |
| `description` | ✅ | 用户在插件市场看到的说明。空着等于没写 |
| `author` | 建议 | `{"name": "……"}` |
| `extensions["org.hugagent"]` | 视情况 | 平台专有字段都放这里，见下 |

**注意**：标准清单**不携带展示字段**（`display_name` / `category` / `icon`）——
这些属于界面配置，由平台侧维护和覆盖。导入的老包若在顶层写了这些，仍会被兼容读取。

## extensions["org.hugagent"] 里能放什么

```json
"extensions": {
  "org.hugagent": {
    "mcp": {
      "<和 mcp.json 里完全一致的服务名>": {
        "display_name": "界面显示名",
        "description": "这个工具服务是干什么的",
        "tools": [
          { "name": "工具名", "description": "何时该调 + 参数从哪来 + 红线" }
        ]
      }
    },
    "required_secrets": ["SOME_API_KEY"],
    "admin_config": { "fields": [ ... ] },
    "connection": "……"
  }
}
```

- **`mcp` 的键必须和 `mcp.json` 里的服务名一一对应**，对不上就贴不上去，
  而且不会报错——只是静默失效。自检脚本会抓这个。
- `required_secrets`：安装时向用户索要的凭据 key 列表。
- `admin_config.fields`：需要管理员在后台统一配置时用（普通插件通常不需要）。

## mcp.json

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "my_server": {
      "type": "streamable-http",
      "url": "http://mcp:9199/mcp/"
    }
  }
}
```

- 标准的 `mcp.json` **只放连接配置**，展示元数据走扩展段（上一节）。
  两边冲突时连接配置永远优先。
- 每个服务要么给 `url`（远程/容器内服务），要么给 `command`（stdio 本地进程）。
  两个都没有等于连不上。
- **stdio 类型的服务安装后默认是停用的**——它需要运行时环境齐备才能启用，
  这是有意的保守默认。做包时要意识到用户装完不会立刻可用，需在 description 里说明。

## skills/ 目录

```
skills/
└── <技能目录名>/
    ├── SKILL.md          # 必需
    ├── scripts/          # 可选
    ├── references/       # 可选
    └── assets/           # 可选
```

`SKILL.md` 的 frontmatter 必须有 `name`（小写字母/数字/下划线/连字符，1–63 字符）
和 `description`（非空，它是技能被唤起的主要依据）。

没有 `SKILL.md` 的子目录会被**直接忽略**，不报错——所以做完一定要跑自检脚本，
否则你以为带了三个技能，实际只导入了两个。

## 常见失败原因

| 现象 | 原因 |
| --- | --- |
| 导入报"没有 plugin.json" | 打包时把外层目录裹进去了。要用 `tar -C <插件目录> .` |
| 装完少了技能 | 某个 skills 子目录没有 SKILL.md，被静默忽略 |
| 工具有了但描述是空的 | 扩展段的 mcp 服务名和 mcp.json 对不上 |
| 装完工具不可用 | stdio 类型 MCP 默认停用，需要运行时齐备后手动启用 |
| name 被改掉了 | name 含大写或特殊字符，被 slug 化了 |
