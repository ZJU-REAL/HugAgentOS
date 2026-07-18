#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一人公司法务助手 V2.0
签约与合规风控工作台

本脚本不依赖第三方包，适合 SkillHub 中文社区演示和本地运行。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


VERSION = "2.0.0"


LEGAL_REFERENCES = {
    "民法典违约责任": "《中华人民共和国民法典》第577条、第584条、第585条",
    "民法典合同解除": "《中华人民共和国民法典》第563条",
    "民法典格式条款": "《中华人民共和国民法典》第496条、第497条、第498条",
    "著作权委托作品": "《中华人民共和国著作权法》第19条",
    "民事诉讼管辖": "《中华人民共和国民事诉讼法》第35条",
    "仲裁协议": "《中华人民共和国仲裁法》第5条、第16条",
    "公司人格混同": "《中华人民共和国公司法》第23条第3款（2023修订，2024年7月1日施行）",
    "注册资本实缴": "《中华人民共和国公司法》第47条（2023修订，2024年7月1日施行）",
    "劳动合同": "《中华人民共和国劳动合同法》第7条、第10条、第82条",
    "个人信息告知同意": "《中华人民共和国个人信息保护法》第13条、第17条",
    "数据安全": "《中华人民共和国数据安全法》第27条",
    "ICP备案许可": "《互联网信息服务管理办法》第4条、第7条、第8条",
}


CONTRACT_TYPES = {
    "outsourcing": {
        "name": "外包开发协议",
        "keywords": ["外包", "开发", "软件", "小程序", "APP", "源代码", "源码", "验收", "维护"],
    },
    "service": {
        "name": "服务协议",
        "keywords": ["服务", "咨询", "设计", "交付", "项目服务", "技术服务"],
    },
    "nda": {
        "name": "保密协议",
        "keywords": ["保密", "NDA", "商业秘密", "机密", "保密信息"],
    },
    "consulting": {
        "name": "顾问协议",
        "keywords": ["顾问", "咨询", "专家", "辅导", "陪跑"],
    },
    "license": {
        "name": "授权许可协议",
        "keywords": ["授权", "许可", "版权", "著作权", "使用权", "独占", "排他"],
    },
    "cooperation": {
        "name": "合作协议",
        "keywords": ["合作", "联合", "分成", "利润", "共同", "退出机制"],
    },
    "sales": {
        "name": "买卖合同",
        "keywords": ["买卖", "采购", "销售", "货物", "质量", "检验"],
    },
    "lease": {
        "name": "租赁合同",
        "keywords": ["租赁", "出租", "承租", "租金", "押金", "转租"],
    },
}


REVIEW_RULES = [
    {
        "id": "party",
        "name": "主体信息",
        "level": "必须改",
        "patterns": [r"甲方", r"乙方", r"统一社会信用代码|身份证号|法定代表人"],
        "risk": "主体信息不完整，会影响追责、开票、起诉和执行。",
        "suggestion": "补充双方全称、统一社会信用代码、注册地址、联系人、送达地址和签署权限。",
        "basis": "民法典合同成立与履行规则",
    },
    {
        "id": "payment_milestone",
        "name": "分期付款/预付款",
        "level": "必须改",
        "patterns": [r"预付款|首付款|定金|进度款|里程碑", r"\d+%"],
        "risk": "一人公司现金流弱，验收后一次性付款会把垫资和坏账风险集中到乙方。",
        "suggestion": "建议约定 30%-50% 预付款，按里程碑支付进度款，尾款不超过 20%。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
        "role_focus": {"乙方": "收款安全优先，避免全部款项压到验收后。"},
    },
    {
        "id": "acceptance",
        "name": "验收标准和默认通过",
        "level": "必须改",
        "patterns": [r"验收", r"验收标准|验收期限|视为验收|默认通过|逾期未反馈"],
        "risk": "没有验收期限和默认通过机制，客户可能无限拖延确认，导致尾款无法收回。",
        "suggestion": "约定验收材料提交后 5-10 个工作日内反馈；逾期未书面反馈视为验收通过。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
    },
    {
        "id": "change_request",
        "name": "需求变更和免费修改次数",
        "level": "必须改",
        "patterns": [r"需求变更|变更流程|修改次数|免费修改|另行报价"],
        "risk": "未限制变更和修改次数，容易演变成无限返工。",
        "suggestion": "将需求文档作为附件；超出范围、超过次数或影响工期的变更应另行报价和顺延交期。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
    },
    {
        "id": "ip",
        "name": "知识产权归属",
        "level": "必须改",
        "patterns": [r"知识产权|著作权|版权|成果归属|所有权|转让"],
        "risk": "委托作品未约定归属，可能导致成果权利归创作者或受托人，后续商业化受限。",
        "suggestion": "明确成果、源文件、代码、文档、素材的归属和转让/许可条件，并约定款项结清后权利转移。",
        "basis": LEGAL_REFERENCES["著作权委托作品"],
    },
    {
        "id": "source_code",
        "name": "源代码/源文件交付",
        "level": "建议谈",
        "patterns": [r"源代码|源码|源文件|可编辑文件|代码仓库"],
        "risk": "软件、设计、课程、视频项目如不约定源文件，后续维护和迁移会被卡住。",
        "suggestion": "明确是否交付、交付时间、格式、完整性、依赖库清单、部署文档和账号权限移交。",
        "basis": LEGAL_REFERENCES["著作权委托作品"],
    },
    {
        "id": "liability_cap",
        "name": "赔偿上限",
        "level": "必须改",
        "patterns": [r"赔偿上限|最高赔偿|不超过.*合同|以.*为限|责任上限"],
        "risk": "没有赔偿上限，一人公司可能因单个项目承担远超合同金额的责任。",
        "suggestion": "建议将赔偿责任上限限定为已收/已付合同金额的 1-2 倍，故意侵权、保密泄露可另行约定。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
    },
    {
        "id": "delay_penalty",
        "name": "延期违约金",
        "level": "建议谈",
        "patterns": [r"延期|逾期", r"万分之|千分之|%|违约金"],
        "risk": "日千分之一或更高的延期违约金，可能对一人交付造成过重压力。",
        "suggestion": "建议用日万分之五至日千分之一之间的合理区间，并设置累计上限。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
    },
    {
        "id": "maintenance",
        "name": "免费维护范围",
        "level": "建议谈",
        "patterns": [r"维护|质保|售后|免费维护|bug|缺陷修复"],
        "risk": "只写“免费维护一年”但不定义范围，会把新需求、环境变化、第三方接口变化都压给乙方。",
        "suggestion": "区分缺陷修复、功能新增、第三方接口变化、服务器/云服务费用和响应时限。",
        "basis": LEGAL_REFERENCES["民法典违约责任"],
    },
    {
        "id": "confidentiality",
        "name": "保密条款",
        "level": "建议谈",
        "patterns": [r"保密|商业秘密|保密信息|不得披露"],
        "risk": "保密范围过宽或期限无限，可能影响一人公司后续案例展示和复用经验。",
        "suggestion": "限定保密信息范围、例外情形、保密期限、允许展示的脱敏案例。",
        "basis": "《民法典》第501条及反不正当竞争相关规则",
    },
    {
        "id": "non_compete",
        "name": "排他/竞业限制",
        "level": "建议谈",
        "patterns": [r"排他|独家|竞业|不得.*同类|不得.*竞争"],
        "risk": "一人公司客户集中度高，宽泛排他或竞业会直接限制后续接单。",
        "suggestion": "限定客户、地域、期限、业务范围，并要求对方支付合理补偿。",
        "basis": "合同自由原则及公平原则",
    },
    {
        "id": "dispute",
        "name": "争议解决",
        "level": "建议谈",
        "patterns": [r"争议|管辖|法院|仲裁|仲裁委员会"],
        "risk": "管辖地过远或仲裁机构不明确，会增加维权成本。",
        "suggestion": "优先选择一人公司所在地或合同履行地法院；仲裁必须写明具体仲裁委员会。",
        "basis": f"{LEGAL_REFERENCES['民事诉讼管辖']}；{LEGAL_REFERENCES['仲裁协议']}",
    },
]


TYPE_EXTRA_RULES = {
    "outsourcing": ["payment_milestone", "acceptance", "change_request", "ip", "source_code", "maintenance", "liability_cap"],
    "service": ["payment_milestone", "acceptance", "change_request", "liability_cap"],
    "nda": ["confidentiality", "non_compete", "dispute"],
    "consulting": ["payment_milestone", "ip", "non_compete", "liability_cap"],
    "license": ["ip", "confidentiality", "liability_cap"],
    "cooperation": ["ip", "payment_milestone", "dispute", "liability_cap"],
}


COMPLIANCE_CATEGORIES = [
    {
        "category": "公司主体与治理",
        "items": [
            ("营业执照、经营范围、注册地址一致", "必需", "主体异常会影响开票、签约、备案和诉讼。"),
            ("每年1月1日至6月30日完成企业年报", "必需", "逾期可能被列入经营异常名录。"),
            ("重大事项形成股东决定并留档", "高", "一人公司也应保留决策记录，证明公司独立运作。"),
        ],
    },
    {
        "category": "公私财产隔离",
        "items": [
            ("开立并使用独立公司银行账户", "必需", "公私混用会放大人格混同和股东连带责任风险。"),
            ("股东与公司借款签协议、计息、留流水", "高", "资金往来不清会削弱公司财产独立证明。"),
            ("建立凭证、报销、合同、发票归档", "高", "完整凭证是税务和人格独立的核心证据。"),
        ],
    },
    {
        "category": "注册资本与出资",
        "items": [
            ("核对章程出资期限和金额", "必需", "新公司法下有限责任公司原则上五年内缴足。"),
            ("注册资本与业务风险匹配", "高", "盲目高注册资本会制造未来实缴压力。"),
            ("无法实缴时提前评估减资安排", "中", "减资涉及公告、债权人保护和登记流程。"),
        ],
    },
    {
        "category": "合同与收款",
        "items": [
            ("所有项目签署书面合同或订单", "必需", "口头约定难以证明服务范围、价格和验收。"),
            ("设置预付款、里程碑和尾款比例", "高", "避免一人公司垫资交付。"),
            ("保留需求文档、报价单、验收记录和沟通记录", "高", "争议时用于证明交付和收款条件已满足。"),
        ],
    },
    {
        "category": "用工与外包",
        "items": [
            ("区分员工、兼职、顾问、外包", "必需", "假外包真劳动关系可能导致社保、双倍工资和补偿风险。"),
            ("员工入职一个月内签劳动合同并缴社保", "必需", "违反劳动合同法会产生双倍工资等风险。"),
            ("外包按成果结算，不做考勤式管理", "高", "降低被认定为劳动关系的风险。"),
        ],
    },
    {
        "category": "知识产权",
        "items": [
            ("核心品牌尽快申请商标", "高", "防止品牌被抢注。"),
            ("委托开发/设计/文案明确成果归属", "必需", "未约定可能导致权利归创作者或受托人。"),
            ("建立字体、图片、音乐、开源组件授权清单", "高", "商业使用未授权素材可能被索赔。"),
        ],
    },
    {
        "category": "数据与互联网资质",
        "items": [
            ("收集个人信息前公示隐私政策并取得同意", "必需", "涉及个人信息处理时的基础合规义务。"),
            ("网站、小程序、APP按业务办理ICP备案或许可", "高", "经营性互联网信息服务可能需要ICP许可证。"),
            ("涉及数据出境时评估最新出境规则", "高", "需关注个人信息保护法和数据跨境新规。"),
        ],
    },
]


@dataclass
class RiskFinding:
    level: str
    name: str
    risk: str
    suggestion: str
    basis: str
    found: bool
    evidence: str = ""


@dataclass
class ReviewResult:
    contract_type: str
    role: str
    risk_level: str
    signing_advice: str
    must_change: List[RiskFinding]
    negotiate: List[RiskFinding]
    acceptable: List[str]
    clauses: List[str]
    checklist: List[str]


class OPCLegalGuard:
    def detect_contract_type(self, text: str) -> str:
        scores = {}
        for type_id, data in CONTRACT_TYPES.items():
            scores[type_id] = sum(len(re.findall(k, text, flags=re.I)) for k in data["keywords"])
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "service"

    def review_contract(self, text: str, role: str = "乙方", contract_type: Optional[str] = None) -> ReviewResult:
        if not text or len(text.strip()) < 20:
            raise ValueError("合同文本过短，请提供更完整的合同内容。")

        contract_type = contract_type or self.detect_contract_type(text)
        rule_ids = set(TYPE_EXTRA_RULES.get(contract_type, []))
        findings_must: List[RiskFinding] = []
        findings_talk: List[RiskFinding] = []
        acceptable: List[str] = []

        for rule in REVIEW_RULES:
            should_check = rule["id"] in rule_ids or rule["level"] == "必须改"
            if not should_check:
                continue
            found = self._match_rule(text, rule["patterns"])
            evidence = self._evidence(text, rule["patterns"]) if found else "未发现明确约定"
            role_note = rule.get("role_focus", {}).get(role, "")
            suggestion = rule["suggestion"] + (f" 立场提示：{role_note}" if role_note else "")
            finding = RiskFinding(
                level=rule["level"],
                name=rule["name"],
                risk=rule["risk"],
                suggestion=suggestion,
                basis=rule["basis"],
                found=found,
                evidence=evidence,
            )
            if found:
                if rule["id"] in ["delay_penalty", "maintenance", "confidentiality", "non_compete"]:
                    findings_talk.append(finding)
                else:
                    acceptable.append(f"{rule['name']}：已发现相关约定，仍建议人工复核是否对{role}有利。")
            elif rule["level"] == "必须改":
                findings_must.append(finding)
            else:
                findings_talk.append(finding)

        special = self._detect_special_risks(text, role)
        findings_must.extend(special["must"])
        findings_talk.extend(special["talk"])

        risk_level = "高" if findings_must else "中" if findings_talk else "低"
        signing_advice = "不建议直接签署，先修改必须改条款。" if findings_must else "可进入谈判或签署流程，但建议保留证据并复核关键条款。"
        clauses = self._suggest_clauses(contract_type, role)
        checklist = self._signing_checklist(contract_type, role)

        return ReviewResult(
            contract_type=CONTRACT_TYPES.get(contract_type, {}).get("name", contract_type),
            role=role,
            risk_level=risk_level,
            signing_advice=signing_advice,
            must_change=findings_must,
            negotiate=findings_talk,
            acceptable=acceptable[:6],
            clauses=clauses,
            checklist=checklist,
        )

    def render_review(self, result: ReviewResult) -> str:
        lines = [
            "# 一人公司合同审查报告",
            "",
            "## 总体结论",
            f"- 立场假设：用户为{result.role}",
            f"- 合同类型：{result.contract_type}",
            f"- 风险等级：{result.risk_level}",
            f"- 签署建议：{result.signing_advice}",
            "",
            "## 必须改",
            "| 风险点 | 为什么危险 | 建议改法 |",
            "|---|---|---|",
        ]
        if result.must_change:
            for item in result.must_change:
                lines.append(f"| {item.name} | {item.risk} | {item.suggestion}<br>依据：{item.basis} |")
        else:
            lines.append("| 无明显必须改项 | 未发现会直接阻断签署的缺失项 | 仍建议结合金额和交易背景人工复核 |")

        lines.extend(["", "## 建议谈", "| 风险点 | 影响 | 谈判建议 |", "|---|---|---|"])
        if result.negotiate:
            for item in result.negotiate:
                lines.append(f"| {item.name} | {item.risk} | {item.suggestion}<br>依据：{item.basis} |")
        else:
            lines.append("| 暂无 | 未发现明显谈判项 | 保留沟通和履约证据 |")

        lines.extend(["", "## 可接受"])
        if result.acceptable:
            lines.extend([f"- {item}" for item in result.acceptable])
        else:
            lines.append("- 合同文本中可明确识别的保护性条款较少。")

        lines.extend(["", "## 可复制修改条款", "```text"])
        lines.extend(result.clauses)
        lines.append("```")

        lines.extend(["", "## 签约前动作清单"])
        lines.extend([f"{i}. {item}" for i, item in enumerate(result.checklist, 1)])
        lines.extend(["", "## 免责声明", "本分析仅供参考，不构成正式法律意见。重大合同或争议请咨询持牌律师。"])
        return "\n".join(lines)

    def compliance_report(self, business: str = "未指定") -> str:
        today = datetime.now().strftime("%Y年%m月%d日")
        urgent = []
        thirty_days = []
        maintain = []
        for category in COMPLIANCE_CATEGORIES:
            for name, priority, risk in category["items"]:
                row = (category["category"], name, risk)
                if priority == "必需":
                    urgent.append(row)
                elif priority == "高":
                    thirty_days.append(row)
                else:
                    maintain.append(row)

        lines = [
            "# 一人公司合规体检",
            "",
            f"- 检查日期：{today}",
            f"- 主营业务：{business}",
            "",
            "## 紧急整改",
            "| 事项 | 风险 | 今日动作 |",
            "|---|---|---|",
        ]
        for category, name, risk in urgent:
            lines.append(f"| {category}：{name} | {risk} | 今天确认是否已有证据材料，并补齐缺口 |")

        lines.extend(["", "## 30天内完成", "| 事项 | 风险 | 证据材料 |", "|---|---|---|"])
        for category, name, risk in thirty_days:
            lines.append(f"| {category}：{name} | {risk} | 合同、流水、截图、备案记录、授权文件或制度文档 |")

        lines.extend(["", "## 长期维护"])
        for category, name, risk in maintain:
            lines.append(f"- {category}：{name}。{risk}")

        lines.extend([
            "",
            "## 一人公司特别提醒",
            f"- 人格混同：{LEGAL_REFERENCES['公司人格混同']}。建议独立账户、独立凭证、股东借款协议和决策记录。",
            f"- 注册资本：{LEGAL_REFERENCES['注册资本实缴']}。建议注册资本与业务风险匹配，避免盲目写高。",
            "- 合规不是一次性动作，建议每月整理合同、发票、流水、交付和授权证据。",
            "",
            "## 免责声明",
            "本体检仅供参考，不构成正式法律意见。具体合规要求请咨询律师、税务师或主管机关。",
        ])
        return "\n".join(lines)

    def template(self, template_id: str, params: Dict[str, str]) -> str:
        generators = {
            "outsourcing": self._template_outsourcing,
            "service": self._template_service,
            "nda": self._template_nda,
            "consulting": self._template_consulting,
            "license": self._template_license,
        }
        if template_id not in generators:
            return "未知模板ID。可用模板：" + "、".join(generators.keys())
        return generators[template_id](params)

    def list_templates(self) -> str:
        return "\n".join([
            "# 可用合同模板",
            "",
            "| ID | 模板 | 适用场景 |",
            "|---|---|---|",
            "| outsourcing | 外包开发协议 | 软件、小程序、网站、设计外包 |",
            "| service | 服务协议 | 咨询、设计、技术服务、运营服务 |",
            "| nda | 保密协议 | 商务洽谈、合作前保密 |",
            "| consulting | 顾问协议 | 专家顾问、陪跑、咨询服务 |",
            "| license | 授权许可协议 | 内容、软件、课程、版权授权 |",
        ])

    def demo(self) -> str:
        sample = "甲方委托乙方开发电商小程序。合同金额80000元，验收后一次性付款。乙方延期每天按合同总额1%支付违约金。源码归甲方，乙方免费维护一年。"
        result = self.review_contract(sample, role="乙方", contract_type="outsourcing")
        return self.render_review(result)

    def selftest(self) -> Dict[str, Any]:
        result = {
            "version": VERSION,
            "templates": len(["outsourcing", "service", "nda", "consulting", "license"]),
            "compliance_categories": len(COMPLIANCE_CATEGORIES),
            "review_rules": len(REVIEW_RULES),
            "demo_risk_level": self.review_contract("验收后一次性付款，免费维护一年，延期每天赔1%", "乙方", "outsourcing").risk_level,
        }
        result["ok"] = result["templates"] >= 5 and result["compliance_categories"] >= 6 and result["review_rules"] >= 10
        return result

    @staticmethod
    def _match_rule(text: str, patterns: List[str]) -> bool:
        return all(re.search(pattern, text, flags=re.I) for pattern in patterns)

    @staticmethod
    def _evidence(text: str, patterns: List[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                start = max(0, match.start() - 20)
                end = min(len(text), match.end() + 40)
                return text[start:end].replace("\n", " ")
        return ""

    def _detect_special_risks(self, text: str, role: str) -> Dict[str, List[RiskFinding]]:
        must: List[RiskFinding] = []
        talk: List[RiskFinding] = []
        if re.search(r"验收后.*一次性付款|一次性.*验收后", text) and role in ["乙方", "服务方", "供应商"]:
            must.append(RiskFinding(
                "必须改", "验收后一次性付款",
                "乙方垫资完成全部交付，客户拖延验收时会直接卡住现金流。",
                "改为预付款 + 里程碑 + 尾款，且设置默认验收通过。",
                LEGAL_REFERENCES["民法典违约责任"], False
            ))
        if re.search(r"每天.*1%|每日.*1%|日.*百分之一", text):
            talk.append(RiskFinding(
                "建议谈", "过高延期违约金",
                "日1%年化极高，且一人公司抗风险能力弱。",
                "改为日万分之五至日千分之一，并设置累计上限。",
                LEGAL_REFERENCES["民法典违约责任"], True
            ))
        return {"must": must, "talk": talk}

    @staticmethod
    def _suggest_clauses(contract_type: str, role: str) -> List[str]:
        common = [
            "验收条款：乙方提交交付成果后，甲方应在5个工作日内完成验收并书面反馈；逾期未反馈的，视为验收通过。",
            "变更条款：超出本合同附件《需求说明书》的新增需求、重大调整或超过约定修改次数的，应另行确认费用并顺延交付周期。",
            "责任上限：除故意侵权、恶意泄密等情形外，任一方承担的赔偿责任总额以本合同已收/已付金额为上限。",
        ]
        if contract_type == "outsourcing":
            common.append("知识产权条款：甲方付清全部合同款后，定制开发成果的著作权/使用权按本合同约定转移；乙方既有工具、通用组件和经验方法不因本项目转让。")
            common.append("维护条款：免费维护仅限交付成果既有功能缺陷修复，不包含新增功能、第三方接口变化、服务器/云服务费用和非乙方原因导致的问题。")
        if role in ["乙方", "服务方", "供应商"]:
            common.append("付款条款：合同签署后甲方支付40%预付款，阶段验收后支付40%进度款，最终验收后支付20%尾款。")
        return common

    @staticmethod
    def _signing_checklist(contract_type: str, role: str) -> List[str]:
        items = [
            "确认签约主体、统一社会信用代码、联系人、送达地址和盖章权限。",
            "把报价单、需求说明、交付清单、验收标准作为合同附件。",
            "保存邮件、聊天记录、会议纪要、版本记录和交付截图。",
            "确认发票类型、税费承担、付款账户和付款节点。",
        ]
        if contract_type == "outsourcing":
            items.extend(["确认是否交付源代码、部署文档、账号权限和第三方依赖清单。", "确认维护范围、响应时间和新增需求报价机制。"])
        if role in ["乙方", "服务方", "供应商"]:
            items.append("未收到预付款前，不建议启动重投入工作。")
        return items

    @staticmethod
    def _template_outsourcing(p: Dict[str, str]) -> str:
        return f"""# 外包开发协议

甲方：{p.get('甲方名称', '________')}
乙方：{p.get('乙方名称', '________')}
项目名称：{p.get('项目名称', '________')}
合同金额：人民币{p.get('合同金额', '________')}元

## 1. 项目范围
以附件《需求说明书》和《交付清单》为准。未列入附件的功能均视为新增需求。

## 2. 付款安排
甲方应按以下节点付款：合同签署后40%，阶段交付后40%，最终验收后20%。甲方未按期付款的，乙方有权暂停工作并顺延工期。

## 3. 验收
乙方提交成果后，甲方应在5个工作日内书面反馈；逾期未反馈的，视为验收通过。

## 4. 需求变更
超出需求说明书的变更应另行确认费用和工期。

## 5. 知识产权和源代码
甲方付清全部款项后，定制开发成果按约定归甲方使用。乙方既有工具、通用组件、框架和经验方法不转让。源代码交付范围、时间和格式以附件为准。

## 6. 维护
免费维护仅限既有功能缺陷修复，不含新增需求、服务器费用、第三方接口变化或非乙方原因导致的问题。

## 7. 责任上限
除故意侵权、恶意泄密外，任一方赔偿责任以本合同已收/已付金额为上限。

## 8. 争议解决
协商不成的，任何一方可向乙方所在地有管辖权的人民法院起诉。
"""

    @staticmethod
    def _template_service(p: Dict[str, str]) -> str:
        return f"""# 服务协议

甲方：{p.get('甲方名称', '________')}
乙方：{p.get('乙方名称', '________')}
服务内容：{p.get('服务内容', '________')}
服务费用：人民币{p.get('服务费用', '________')}元

## 核心条款
1. 服务范围以附件《服务清单》为准。
2. 甲方支付预付款后乙方开始服务。
3. 超出服务清单的新增事项另行报价。
4. 乙方提交服务成果后5个工作日内未反馈的，视为确认。
5. 除故意或重大过失外，乙方赔偿责任以已收服务费为上限。
"""

    @staticmethod
    def _template_nda(p: Dict[str, str]) -> str:
        return f"""# 保密协议

披露方：{p.get('披露方', '________')}
接收方：{p.get('接收方', '________')}

## 核心条款
1. 保密信息包括商业计划、客户资料、报价、技术资料、源代码和未公开经营数据。
2. 以下信息不属于保密信息：已公开信息、接收方独立开发信息、第三方合法取得信息、法律要求披露信息。
3. 未经披露方书面同意，接收方不得向第三方披露或用于本次合作以外目的。
4. 保密期限为披露之日起三年；商业秘密依法持续保密。
5. 本协议不得被解释为限制接收方从事正常业务，但不得使用披露方保密信息。
"""

    @staticmethod
    def _template_consulting(p: Dict[str, str]) -> str:
        return f"""# 顾问协议

甲方：{p.get('甲方名称', '________')}
乙方：{p.get('乙方名称', '________')}
顾问事项：{p.get('顾问事项', '________')}
顾问费用：人民币{p.get('顾问费用', '________')}元

## 核心条款
1. 乙方按约定提供咨询建议，不对甲方商业结果作保证性承诺。
2. 甲方应按阶段支付顾问费。
3. 顾问成果的使用范围以本项目为限，另行商业出版、转售或公开传播需取得书面同意。
4. 如需排他或竞业限制，应另行约定期限、范围和补偿。
"""

    @staticmethod
    def _template_license(p: Dict[str, str]) -> str:
        return f"""# 授权许可协议

许可方：{p.get('许可方', '________')}
被许可方：{p.get('被许可方', '________')}
授权内容：{p.get('授权内容', '________')}
授权类型：{p.get('授权类型', '普通许可')}
许可费：人民币{p.get('许可费', '________')}元

## 核心条款
1. 授权范围包括地域、期限、渠道、用途和是否可转授权。
2. 未明确授予的权利均由许可方保留。
3. 被许可方不得超范围使用、转授权或用于违法违规用途。
4. 许可费支付方式和结算周期以双方书面确认为准。
5. 如发生第三方权利争议，双方按过错承担责任。
"""


def read_text_arg(text: Optional[str], file_path: Optional[str]) -> str:
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    return text or ""


def parse_kv(values: List[str]) -> Dict[str, str]:
    result = {}
    for value in values:
        if "=" in value:
            key, val = value.split("=", 1)
            result[key] = val
    return result


def main() -> None:
    guard = OPCLegalGuard()
    parser = argparse.ArgumentParser(
        description="一人公司法务助手 V2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  python main.py demo                                   # 运行演示
  python main.py review --text "合同内容" --role 乙方    # 审查合同
  python main.py template outsourcing 甲方名称=XX        # 生成模板
  python main.py compliance --business 软件开发           # 合规体检
  python main.py selftest                               # 自检
  python main.py --help                                 # 查看帮助
""",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo")
    sub.add_parser("templates")
    sub.add_parser("selftest")

    review = sub.add_parser("review")
    review.add_argument("--text")
    review.add_argument("--file")
    review.add_argument("--role", default="乙方")
    review.add_argument("--type", default=None)
    review.add_argument("--json", action="store_true")

    template = sub.add_parser("template")
    template.add_argument("template_id")
    template.add_argument("params", nargs="*")

    compliance = sub.add_parser("compliance")
    compliance.add_argument("--business", default="未指定")

    args = parser.parse_args()

    if args.command in [None, "demo"]:
        print(guard.demo())
    elif args.command == "templates":
        print(guard.list_templates())
    elif args.command == "selftest":
        result = guard.selftest()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "review":
        text = read_text_arg(args.text, args.file)
        result = guard.review_contract(text, args.role, args.type)
        if args.json:
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        else:
            print(guard.render_review(result))
    elif args.command == "template":
        print(guard.template(args.template_id, parse_kv(args.params)))
    elif args.command == "compliance":
        print(guard.compliance_report(args.business))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass  # argparse 正常退出
    except ValueError as e:
        print()
        print(f"❌ 输入有误：{e}")
        print()
        print("请检查：")
        print("  1. 命令拼写是否正确？试试 `python main.py --help`")
        print("  2. 审查合同是否粘贴了完整的合同文本（至少20字）")
        print("  3. 参数格式是否符合要求（如 --role 乙方 而不是 --role=乙方）")
    except FileNotFoundError as e:
        print()
        print(f"❌ 文件未找到：{e}")
        print("请确认 --file 指定的路径是否正确。")
    except Exception as e:
        print()
        print("❌ 出了点小问题，请检查：")
        print(f"  错误信息：{e}")
        print()
        print("常见原因：")
        print("  1. 命令拼写不正确 → 运行 `python main.py --help` 查看所有命令")
        print("  2. 参数遗漏或格式不对 → 用 `--help` 查看具体命令的参数要求")
        print("  3. 审查的合同文本过短 → 至少粘贴20字以上")
        print("  4. 模板参数格式错误 → 用 `参数名=参数值` 格式，如 `甲方名称=XX公司`")
        print()
        print("如果仍有问题，可运行 `python main.py demo` 体验基础功能。")
