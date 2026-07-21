# 快速构建领域本体

> 最后更新：2026-07-21 ｜
> [English](../../en/getting-started/domain-ontology-quickstart.md)

本指南帮助你在 20 分钟内构建并发布第一个 Domain Pack，让 HugAgentOS 能够用
受控概念、关系、约束和工作流校验领域任务。示例不依赖外部 MCP，适合先验证
完整闭环，再逐步绑定真实工具和技能。

## Domain Pack 包含什么

HugAgentOS 的领域本体是可执行的业务契约，而不是只用于检索的术语表。一个
Domain Pack 由以下部分组成：

- **基本信息与配置**：领域包 ID、版本、注入预算、熔断阈值等运行参数；
- **概念**：领域对象、别名、定义、层级、受控取值和风险等级；
- **关系**：概念之间允许或禁止的连接，以及基数要求；
- **约束**：工具参数或最终输出必须满足的 JSON Schema、证据和前置工具要求；
- **工作流**：文本或资产触发条件、必需/禁止工具、输出标签和评审级别。

> [!TIP]
> 第一个领域包只覆盖一个高价值任务。先用 2–5 个概念、1 个工作流和 1 条
> 可执行约束跑通闭环，再扩展词表和规则。

## 第一步：选择一个可验收的任务

先把业务目标写成一句可以测试的话。例如：“分析一次采购申请中的供应商风险，
并给出结构完整的风险摘要。”随后明确以下三类内容：

1. 列出任务必须理解的对象，例如采购申请、供应商风险；
2. 写出必须满足的铁律，例如风险摘要不得少于必要的说明；
3. 选择评审级别：`none` 不额外评审，`checkpoint` 使用单评审员，
   `committee` 用于高风险、多评审员场景。

## 第二步：导入最小 Domain Pack

使用管理员账号登录后，打开「设置 → 本体治理」，选择「导入 Domain Pack」，
粘贴下面的 JSON。这个示例用文本和技能标签触发采购风险工作流，并要求最终摘要
至少包含 120 个字符。

```json
{
  "schema_version": "1.0",
  "pack_id": "procurement_risk",
  "name": "采购风险领域包",
  "version": "1.0.0",
  "domain": "procurement-risk",
  "description": "约束采购申请中的供应商风险识别与摘要交付。",
  "config": {
    "injection_enabled": true,
    "max_concepts": 8,
    "token_budget": 1600,
    "committee_size": 3,
    "repeated_denial_threshold": 2,
    "circuit_breaker_threshold": 5,
    "allow_unresolved_tools": false
  },
  "concepts": [
    {
      "id": "ProcurementRequest",
      "name": "采购申请",
      "aliases": ["采购单"],
      "definition": "包含采购标的、预算、申请部门和候选供应商的业务申请。",
      "tags": ["采购"],
      "risk": "low"
    },
    {
      "id": "SupplierRisk",
      "name": "供应商风险",
      "aliases": ["供应风险"],
      "definition": "可能影响供应商履约、合规或持续经营能力的风险。",
      "closed_values": ["低", "中", "高", "待核验"],
      "tags": ["风险"],
      "risk": "medium"
    }
  ],
  "relations": [
    {
      "id": "request_has_supplier_risk",
      "subject": "ProcurementRequest",
      "predicate": "包含",
      "object": "SupplierRisk",
      "description": "采购申请可以关联一个或多个供应商风险。",
      "min_cardinality": 0,
      "forbidden": false
    }
  ],
  "constraints": [
    {
      "id": "procurement_summary_complete",
      "name": "采购风险摘要必须完整",
      "target": {
        "kind": "output",
        "output_tag": "procurement_risk_summary"
      },
      "schema": {
        "type": "string",
        "minLength": 120
      },
      "concept_id": "SupplierRisk",
      "requires_citations": false,
      "prerequisite_tools": [],
      "mode": "enforce",
      "risk": "medium",
      "message": "采购风险摘要过短，无法支持业务复核。",
      "suggestion": "补充风险事实、影响、未知项和建议的核验动作。",
      "enabled": true
    }
  ],
  "workflows": [
    {
      "id": "procurement_risk_review",
      "name": "采购风险评审",
      "triggers": [
        "采购风险",
        "供应商风险",
        "procurement risk",
        "supplier risk"
      ],
      "asset_triggers": [
        {
          "kind": "skill",
          "tags_any": ["ontology:SupplierRisk"]
        }
      ],
      "required_tools": [],
      "forbidden_tools": [],
      "output_tags": ["procurement_risk_summary"],
      "review_level": "checkpoint",
      "risk": "medium"
    }
  ]
}
```

保留「导入后立即发布」为关闭状态，然后选择「校验并导入」。系统会先检查 ID、
版本、概念引用、JSON Schema 和工具引用；有错误时会显示精确字段路径。

## 第三步：检查并发布版本

导入成功后，先检查工作草稿，再把它发布到运行时。按以下顺序操作：

1. 在领域包列表中打开「详情」，核对总览、概念、关系、约束和工作流；
2. 打开「版本管理」，发布 `1.0.0` 工作草稿；
3. 回到领域包列表，打开「启用」开关；
4. 需要让它成为默认领域包时，再打开「默认」开关。

正式版本发布后会变为只读。后续修改会创建新的工作草稿，不会覆盖正在运行的
版本。

## 第四步：启用并测试本体校验

在「设置 → 本体治理」打开「使用领域本体校验」，然后新建会话并输入下面的测试
问题：

```text
请分析这次采购申请的供应商风险，并输出一份风险摘要。
```

任务命中 `采购风险` 或 `供应商风险` 后，聊天界面会显示工作流激活事件，并在
回答完成后执行 `checkpoint` 评审。你可以在本体治理页面查看门禁事件和评审记录。

再执行两组负向测试，确认规则只在正确范围内生效：

- 输入与采购无关的问题，确认不会加载该工作流；
- 要求极短的采购风险结论，确认系统能够指出输出约束或生成优化稿。

## 第五步：绑定真实工具和技能

最小示例跑通后，把本体从“输出检查”扩展到“行动门控”。开始前先在 MCP 管理中
确认真实工具 ID 和参数 Schema，再进行以下配置：

1. 在工作流的 `required_tools` 中加入完成任务必须调用的工具；
2. 在约束的 `target.tool` 中填写真实工具 ID，并用 `schema` 约束输入参数；
3. 需要固定执行顺序时，在 `prerequisite_tools` 中声明前置工具；
4. 在 `asset_triggers` 中用工具 ID 或 `ontology:ConceptId` 标签触发工作流；
5. 编辑技能或子智能体时，从「本体治理标签」选择器绑定相同标签。

如果领域包引用了尚未注册的工具，默认校验会失败。仅在迁移或分阶段接入时临时将
`allow_unresolved_tools` 设为 `true`，并在生产发布前恢复严格校验。

## 发布前验收清单

一个可投入使用的领域包至少应通过以下检查：

- 每个概念都有清晰定义，别名不会与其他概念冲突；
- 每条关系引用的起点和终点概念都存在，层级没有循环；
- 工具约束使用运行环境中的真实工具 ID 和参数名；
- 工作流至少有一个文本触发器或资产触发器；
- 必需工具和禁止工具没有交集；
- 普通问题不会误触发，高风险问题能进入预期评审级别；
- 违规消息说明“为什么失败”，修正建议说明“下一步怎么做”；
- 新版本先以工作草稿验证，再由管理员显式发布。

## 常见问题

下面列出第一次构建 Domain Pack 时最常见的失败原因和处理方式。

| 现象 | 处理方式 |
|---|---|
| 提示 `unknown tool reference` | 核对 MCP 已发现的真实工具 ID，或先移除工具依赖完成最小闭环 |
| 标签没有出现在技能选择器 | 同时定义对应概念，并在工作流 `asset_triggers.tags_any` 中使用该标签 |
| 对话没有命中工作流 | 确认领域包已发布、启用且设为默认，并检查 `triggers` 是否出现在用户问题中 |
| 修改后运行时没有变化 | 正式版本只读；编辑工作草稿并发布新版本 |
| 规则频繁误拦截 | 收窄触发词、降低规则模式或风险级别，并用审计记录定位误命中来源 |

## 下一步

完成第一个领域包后，继续阅读[领域本体治理与校验](../modules/ontology-harness.md)，
了解工具门禁、资产标签、委员会评审、版本治理和本体演进闭环。要绑定外部能力，
再阅读 [MCP 工具系统](../modules/mcp-tools.md)和
[技能系统](../modules/agent-skills.md)。
