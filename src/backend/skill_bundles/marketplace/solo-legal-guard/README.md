# 一人公司法务助手 V2.0

一人公司签约与合规风控工作台。

这版不再只是"合同模板库"，而是围绕独立创业者最常见的真实风险设计：收款、验收、无限修改、知识产权、源代码交付、外包用工、数据合规、人格混同和注册资本实缴。

## 适用场景

- 客户发来合同，想知道能不能签。
- 需要起草服务协议、外包开发协议、NDA、顾问协议、授权许可协议。
- 准备接商单、外包项目、咨询服务或内容授权。
- 想检查一人公司的基础合规：年报、税务、发票、用工、知识产权、数据合规、ICP备案。
- 担心一人有限公司公私财产混同、注册资本五年实缴、股东连带责任。

## V2.0 重点升级

- 修复 Windows 控制台编码问题，脚本可直接运行。
- 移除对 PyYAML 等第三方依赖，核心数据内置于脚本，避免配置读取失败。
- 增加 `selftest` 自检，确保模板、合规清单和审查规则可用。
- 新增一人公司专属分诊：先判断用户立场，再输出"必须改 / 建议谈 / 可接受"。
- 强化签约实操：提供可复制修改条款、谈判话术和签约前证据清单。
- 强化 OPC 风险：人格混同、注册资本实缴、劳动关系误判、知识产权归属、数据合规。

## 快速开始

```bash
python scripts/main.py demo
```

审查合同：

```bash
python scripts/main.py review --text "甲方要求验收后一次性付款，延期每天赔1%，免费维护一年" --role 乙方 --type outsourcing
```

生成外包开发协议：

```bash
python scripts/main.py template outsourcing 甲方名称=客户公司 乙方名称=我的公司 项目名称=电商小程序 合同金额=80000
```

合规体检：

```bash
python scripts/main.py compliance --business 软件开发
```

运行自检：

```bash
python scripts/main.py selftest
```

## 文件结构

```text
solo-legal-guard/
├── SKILL.md
├── README.md
├── _meta.json
├── agents/
│   └── openai.yaml
├── references/
│   └── opc-legal-playbook.md
└── scripts/
    └── main.py
```

## 免责声明

本技能输出仅用于法律风险识别和合同文本辅助，不构成正式法律意见或律师服务。重大交易、争议、诉讼、仲裁、融资、股权、行政处罚和高风险合规事项，请咨询持牌律师或主管机关。
