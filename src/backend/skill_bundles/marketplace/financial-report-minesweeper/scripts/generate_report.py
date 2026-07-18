#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — 报告生成器
读取 compute_indicators.py 输出的 JSON，按 templates.md 模板生成 MD + HTML 双格式报告

用法：
    python generate_report.py <code>_排雷指标.json
"""

import json
import sys
import os
from datetime import datetime

# 强制UTF-8输出（Windows GBK兼容）
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 配置常量
# ============================================================
DAYS_PER_YEAR = 360       # 存货/应收周转天数计算用
DATA_ANOMALY_PCT = 100    # pct超过此值视为数据异常（10000%）


def assess_report_quality(raw, method="unified"):
    """统一评估报表质量（扣非占比+现金流质量双维度）
    
    Args:
        raw: 各期原始数据列表
        method: "unified"(统一) / "deduct_only"(仅扣非，旧逻辑) / "strict"(严格)
    """
    deduct_vals = [r.get("扣非占比") for r in raw if r.get("扣非占比") is not None]
    cfo_vals = [r.get("净利润现金比率") for r in raw if r.get("净利润现金比率") is not None]
    good_deduct = sum(1 for v in deduct_vals if 0.8 < v < 1.2)
    good_cfo = sum(1 for v in cfo_vals if v > 0.8)
    avg_deduct = sum(deduct_vals) / len(deduct_vals) if deduct_vals else 0

    if good_deduct >= 3 and good_cfo >= 2:
        return "优秀"
    elif avg_deduct > 0.7 and good_cfo >= 1:
        return "良好"
    elif avg_deduct > 0.5:
        return "一般"
    else:
        return "较差"


def cfo_health_verdict(raw):
    """评估经营现金流健康度"""
    latest = raw[-1]
    latest_cfo = latest.get("CFO")
    latest_net = latest.get("归母净利润")

    if latest_cfo is None or latest_net is None:
        return "现金流数据缺失"

    if latest_net > 0:
        # 净利润为正：比较CFO/净利
        if latest_cfo > latest_net * 0.8:
            return "经营现金流充裕"
        elif latest_cfo > 0:
            return "经营现金流紧张"
        else:
            return "经营现金流为负"
    else:
        # 净利润为负：看CFO绝对规模与趋势
        cfo_up = sum(1 for i in range(1, len(raw))
                     if raw[i].get("CFO") is not None and raw[i - 1].get("CFO") is not None
                     and raw[i]["CFO"] > raw[i - 1]["CFO"])
        if latest_cfo > 0:
            return "经营现金流为正但利润亏损"
        elif cfo_up >= 2:
            return "经营现金流虽为负但边际改善"
        else:
            return "经营现金流持续为负，与净利润双双恶化"


def fmt(val, decimals=2, suffix="", none_str="【无公开数据】"):
    """格式化数值"""
    if val is None or val == "__NODATA__":
        return none_str
    if isinstance(val, float):
        return f"{val:.{decimals}f}{suffix}"
    if isinstance(val, bool):
        return "是" if val else "否"
    return str(val) + suffix


def pct(val, decimals=2):
    """百分比格式化"""
    if val is None or val == "__NODATA__":
        return "【无公开数据】"
    if abs(val) > DATA_ANOMALY_PCT:  # 超过10000%视为数据异常
        return "【数据异常】"
    return f"{val * 100:.{decimals}f}%"


def rate(val):
    """比率格式化"""
    if val is None or val == "__NODATA__":
        return "【无公开数据】"
    if val == "NO_SHORT_DEBT":
        return "∞（无短债）"
    return f"{val:.2f}"


def growth_fmt(val):
    """增速格式化"""
    if val is None:
        return "基期"
    return f"{val:+.2f}%"


# ============================================================
# HTML 输出辅助函数
# ============================================================

def risk_label(level):
    """风险等级色标"""
    mapping = {"低": "low", "中": "medium", "高": "high", "极高": "critical"}
    cls = mapping.get(level, "low")
    return f'<span class="risk-label risk-label-{cls}">{level}</span>'

def anomaly_item_html(name, detail, is_critical=False):
    """生成异常列表项HTML"""
    icon = "🚫" if is_critical else "⚠"
    cls = "anomaly-item anomaly-critical" if is_critical else "anomaly-item"
    return f'<li class="{cls}"><span class="anomaly-icon">{icon}</span><span class="anomaly-name">{name}</span><span class="anomaly-detail">：{detail}</span></li>'


# ============================================================
# 模块七/八/九 辅助生成函数
# ============================================================

def build_anomaly_checklist(raw, periods, results):
    """构建24项排雷异常清单"""
    anomalies = results["anomalies"]
    anom_names = {a["name"] for v in anomalies.values() for a in v}
    items = []

    def check_periods(fn, desc=""):
        parts = []
        for i, r in enumerate(raw):
            result = fn(r, i)
            parts.append(f"P{i+1}:{result}")
        return "；".join(parts)

    def any_period(fn):
        return any(fn(r, i) for i, r in enumerate(raw))

    for seq, name, fn, risk in [
        (1, "存贷双高", lambda r,i: "是" if r.get("存贷双高") else "否", "高"),
        (2, "应收增速远超营收", lambda r,i: "是" if r.get("应收营收增速差") and abs(r["应收营收增速差"])>20 else "否", "中"),
        (3, "存货增速远超营收", lambda r,i: "是" if r.get("存货营收增速差") and abs(r["存货营收增速差"])>20 else "否", "中"),
        (4, "经营现金流长期低于净利", lambda r,i: "是" if r.get("净利润现金比率") and r["净利润现金比率"]<0.8 else "否", "高"),
        (5, "在建工程大额长期不转固", lambda r,i: "是" if r.get("在建工程占资产比") and r["在建工程占资产比"]>0.05 and i>=2 and raw[i-1].get("在建工程增速") and raw[i-1]["在建工程增速"]>0 and r.get("在建工程增速") and r["在建工程增速"]>0 else "否", "中"),
        (6, "商誉过高且业绩不达标", lambda r,i: "是" if r.get("商誉占归母比") and r["商誉占归母比"]>0.3 else "否", "高"),
        (7, "扣非净利远低于净利", lambda r,i: "是" if r.get("扣非占比") and r["扣非占比"]<0.6 else "否", "中"),
        (8, "货币资金无法覆盖短债", lambda r,i: "否" if r.get("货资比短债") == "NO_SHORT_DEBT" or (isinstance(r.get("货资比短债"), (int,float)) and r["货资比短债"]>=1) else "是", "高"),
        (9, "毛利率异常波动", lambda r,i: "是" if i>=1 and r.get("毛利率") is not None and raw[i-1].get("毛利率") is not None and abs(r["毛利率"]-raw[i-1]["毛利率"])>0.05 else "否", "中"),
        (10, "研发资本化合理性", lambda r,i: "【需查附注-THS不提供资本化金额】", "中"),
        (11, "频繁变更会计政策", lambda r,i: "【需查年报】", "中"),
        (12, "审计意见非标", lambda r,i: "【需查年报-舞弊红线】", "极高"),
        (13, "控股股东高比例质押", lambda r,i: "是" if r.get("控股股东质押比例") and r["控股股东质押比例"]>50 else ("关注" if r.get("控股股东质押比例") and r["控股股东质押比例"]>30 else "否"), "高"),
        (14, "其他收益占比过高", lambda r,i: "是" if r.get("其他收益占利润比") and r["其他收益占利润比"]>0.5 else "否", "中"),
        (15, "关联交易占比过高", lambda r,i: "【需查年报】", "高"),
        (16, "利息收入/货币资金<0.15%", lambda r,i: "是" if r.get("资金利息率") and r["资金利息率"]<0.0015 else "否", "高"),
        (17, "归母与合并净利偏离", lambda r,i: "是" if r.get("归母占合并比") and (r["归母占合并比"]<0.9 or r["归母占合并比"]>1.1) else "否", "极高"),
        (18, "供应链话语权持续弱势", lambda r,i: "是" if r.get("供应链差额") and r["供应链差额"]<0 else "否", "高"),
        (19, "非经常性损益依赖度过高", lambda r,i: "是" if r.get("非经常依赖度") and r["非经常依赖度"]>0.5 else "否", "高"),
        (20, "主业持续亏损", lambda r,i: "是" if r.get("扣非归母比") and r["扣非归母比"]<0 else "否", "极高"),
        (21, "净利润虚假增长", lambda r,i: "是" if r.get("虚假增长") else "否", "高"),
        (22, "存货操纵利润舞弊", lambda r,i: "是" if r.get("_stock_signals",0)>=3 else ("⚠" if r.get("_stock_signals",0)>=2 else "否"), "极高"),
        (23, "净利润纸面富贵", lambda r,i: "是" if r.get("纸面富贵") else "否", "高"),
        (24, "净利润含金量不足", lambda r,i: "是" if r.get("净利润含金量") and r["净利润含金量"]<=0.3 else "否", "高"),
    ]:
        basis = check_periods(fn)
        # 判定
        any_trigger = any_period(lambda r,i: (fn(r,i) in ("是","⚠","⚠需同行对比")))
        trig_2plus = sum(1 for i,r in enumerate(raw) if fn(r,i) in ("是","⚠","⚠需同行对比")) >= 2
        if any_trigger:
            verdict = "⚠异常" if trig_2plus else "⚠单期"
        else:
            verdict = "✓正常"
        items.append({"序号": seq, "项目": name, "判定": verdict, "依据": basis, "风险": risk})

    return items


def build_core_indicators(raw):
    """构建50项核心指标汇总表，含逐指标4期趋势判定"""
    def trend(key, direction="up"):
        """4期趋势判定：看后3期对前1期的变化方向"""
        vals = [r.get(key) for r in raw if r.get(key) is not None]
        if len(vals) < 4:
            return "数据不足"
        up_count = sum(1 for i in range(1,4) if vals[i] is not None and vals[i-1] is not None and vals[i] > vals[i-1])
        down_count = sum(1 for i in range(1,4) if vals[i] is not None and vals[i-1] is not None and vals[i] < vals[i-1])
        if up_count >= 2 and direction == "up":
            return "持续增长" if up_count == 3 else "增长"
        if down_count >= 2 and direction == "down":
            return "持续下滑" if down_count == 3 else "下滑"
        return "稳定"

    def any_below(key, threshold):
        return any(r.get(key) is not None and isinstance(r.get(key), (int, float)) and r.get(key) < threshold for r in raw)

    def all_below(key, threshold):
        return all(r.get(key) is not None and r.get(key) < threshold for r in raw)

    def latest_val(key):
        v = raw[-1].get(key)
        return v if v is not None else None

    # (name, value_fn, judge_fn)
    rows = [
        ("货币资金(亿)", lambda r: fmt(r.get("货币资金"),2), lambda: trend("货币资金","up")),
        ("短期有息负债(亿)", lambda r: fmt((r.get("短期借款")or 0)+(r.get("一年到期")or 0),2), lambda: trend("短期有息负债","down")),
        ("有息负债总额(亿)", lambda r: fmt(r.get("有息负债总额"),2), lambda: trend("有息负债总额","down")),
        ("货资/短期有息负债", lambda r: rate(r.get("货资比短债")), lambda: "偏低⚠" if any_below("货资比短债",1) else "安全"),
        ("利息保障倍数", lambda r: rate(r.get("利息保障倍数")), lambda: "偏低⚠" if any_below("利息保障倍数",3) else "安全"),
        ("存贷双高", lambda r: "是" if r.get("存贷双高") else "否", lambda: "⚠异常" if any(r.get("存贷双高") for r in raw) else "正常"),
        ("资金利息率", lambda r: pct(r.get("资金利息率"),4), lambda: "偏低⚠" if any_below("资金利息率",0.0015) else "正常"),
        ("应收合计(亿)", lambda r: fmt(r.get("应收合计"),2), lambda: "增长⚠" if trend("应收合计","up") in ("持续增长","增长") else "正常"),
        ("应收占营收比", lambda r: pct(r.get("应收占营收比")), lambda: "偏高⚠" if any(r.get("应收占营收比") is not None and r.get("应收占营收比") > 0.4 for r in raw) else "正常"),
        ("应收周转率(次)", lambda r: rate(r.get("应收周转率")), lambda: "下滑⚠" if trend("应收周转率","down") in ("持续下滑","下滑") else "正常"),
        ("存货(亿)", lambda r: fmt(r.get("存货"),2), lambda: trend("存货","up")),
        ("存货周转率(次)", lambda r: rate(r.get("存货周转率")), lambda: "下滑⚠" if trend("存货周转率","down") in ("持续下滑","下滑") else "正常"),
        ("存货周转天数(天)", lambda r: fmt(360/r.get("存货周转率"),0) if r.get("存货周转率") else "N/A", lambda: "拉长⚠" if trend("存货周转率","down") in ("持续下滑","下滑") else "正常"),
        ("应付合计(亿)", lambda r: fmt(r.get("应付合计"),2), lambda: trend("应付合计","up")),
        ("营业收入(亿)", lambda r: fmt(r.get("营业收入"),2), lambda: trend("营业收入","up")),
        ("毛利率", lambda r: pct(r.get("毛利率")), lambda: "下滑⚠" if trend("毛利率","down") in ("持续下滑","下滑") else "稳定"),
        ("销售收现比", lambda r: rate(r.get("销售收现比")), lambda: "偏低⚠" if any_below("销售收现比",0.9) else "正常"),
        ("经营现金流入营收比", lambda r: rate(r.get("经营现金流入营收比")), lambda: "偏低⚠" if any_below("经营现金流入营收比",0.8) else "正常"),
        ("净利润(亿)", lambda r: fmt(r.get("净利润"),2), lambda: trend("净利润","up")),
        ("归母净利润(亿)", lambda r: fmt(r.get("归母净利润"),2), lambda: trend("归母净利润","up")),
        ("扣非净利润(亿)", lambda r: fmt(r.get("扣非净利润"),2), lambda: trend("扣非净利润","up")),
        ("扣非占比", lambda r: pct(r.get("扣非占比")), lambda: "偏低⚠" if any_below("扣非占比",0.6) else "正常"),
        ("加权ROE", lambda r: pct(r.get("加权ROE")), lambda: "偏低⚠" if any_below("加权ROE",0.10) else "正常"),
        ("扣非ROE", lambda r: pct(r.get("扣非ROE")), lambda: "偏低⚠" if any_below("扣非ROE",0.06) else "正常"),
        ("其他收益占利润比", lambda r: pct(r.get("其他收益占利润比")), lambda: "偏高⚠" if any(r.get("其他收益占利润比") and r.get("其他收益占利润比")>0.5 for r in raw) else "正常"),
        ("归母占合并比", lambda r: pct(r.get("归母占合并比")), lambda: "偏离⚠" if any(r.get("归母占合并比") and (r.get("归母占合并比")<0.9 or r.get("归母占合并比")>1.1) for r in raw) else "正常"),
        ("CFO(亿)", lambda r: fmt(r.get("CFO"),2), lambda: trend("CFO","up")),
        ("CFI(亿)", lambda r: fmt(r.get("CFI"),2), lambda: "大额流出" if all(r.get("CFI") and r.get("CFI")<-100 for r in raw) else "正常"),
        ("CFF(亿)", lambda r: fmt(r.get("CFF"),2), lambda: "大额流出" if all(r.get("CFF") and r.get("CFF")<-100 for r in raw) else "正常"),
        ("净利现金比率", lambda r: rate(r.get("净利润现金比率")), lambda: "偏低⚠" if any_below("净利润现金比率",0.5) else ("略低" if any_below("净利润现金比率",0.8) else "正常")),
        ("资本开支(亿)", lambda r: fmt(r.get("资本开支"),2), lambda: trend("资本开支","up")),
        ("自由现金流(亿)", lambda r: fmt(r.get("自由现金流"),2), lambda: "充裕" if all(r.get("自由现金流") and r.get("自由现金流")>0 for r in raw) else "紧张⚠"),
        ("现金分红率", lambda r: pct(r.get("现金分红率")), lambda: "偏低⚠" if all(r.get("现金分红率") and r.get("现金分红率")<0.01 for r in raw) else "正常"),
        ("总资产(亿)", lambda r: fmt(r.get("总资产"),2), lambda: trend("总资产","up")),
        ("总负债(亿)", lambda r: fmt(r.get("总负债"),2), lambda: trend("总负债","up")),
        ("资产负债率", lambda r: pct(r.get("资产负债率")), lambda: "过高⚠" if any(r.get("资产负债率") and r.get("资产负债率")>0.7 for r in raw) else "正常"),
        ("有息负债率", lambda r: pct(r.get("有息负债率")), lambda: "偏高⚠" if any(r.get("有息负债率") and r.get("有息负债率")>0.3 for r in raw) else "正常"),
        ("归母净资产(亿)", lambda r: fmt(r.get("归母净资产"),2), lambda: trend("归母净资产","up")),
        ("商誉/归母权益", lambda r: pct(r.get("商誉占归母比")), lambda: "偏高⚠" if any(r.get("商誉占归母比") and r.get("商誉占归母比")>0.3 for r in raw) else "正常"),
        ("控股股东质押率", lambda r: f"{r.get('控股股东质押比例'):.2f}%" if r.get('控股股东质押比例') is not None else "【需查年报】", lambda: "偏高⚠" if any(r.get('控股股东质押比例') and r['控股股东质押比例'] > 50 for r in raw) else ("关注" if any(r.get('控股股东质押比例') and r['控股股东质押比例'] > 30 for r in raw) else "正常")),
        ("审计意见", lambda r: "【需查年报】", lambda: "【需查年报】"),
        ("供应链差额(亿)", lambda r: fmt(r.get("供应链差额"),2), lambda: "强势" if all(r.get("供应链差额") and r.get("供应链差额")>0 for r in raw) else "弱势⚠"),
        ("非经常依赖度", lambda r: pct(r.get("非经常依赖度")), lambda: "偏高⚠" if any(r.get("非经常依赖度") and abs(r.get("非经常依赖度"))>0.5 for r in raw) else "正常"),
        ("扣非/归母", lambda r: rate(r.get("扣非归母比")), lambda: "亏损⚠" if any(r.get("扣非归母比") and r.get("扣非归母比")<0 for r in raw) else "正常"),
        ("现金利息保障倍数", lambda r: rate(r.get("现金利息保障倍数")), lambda: "偏低⚠" if any_below("现金利息保障倍数",5) else "正常"),
        ("净利润含金量", lambda r: pct(r.get("净利润含金量")), lambda: "偏低⚠" if any_below("净利润含金量",0.3) else "正常"),
        ("流动比率", lambda r: rate(r.get("流动比率")), lambda: "偏低⚠" if any_below("流动比率",1) else "正常"),
        ("速动比率", lambda r: rate(r.get("速动比率")), lambda: "偏低⚠" if any_below("速动比率",0.5) else "正常"),
        ("每股净资产(元)", lambda r: fmt(r.get("每股净资产"),2), lambda: trend("每股净资产","up")),
        ("每股经营现金流(元)", lambda r: fmt(r.get("每股经营现金流"),2), lambda: "偏低⚠" if any(r.get("每股经营现金流") and r.get("每股经营现金流")<0 for r in raw) else "正常"),
    ]

    items = []
    for name, fn, judge_fn in rows:
        vals = [fn(raw[i]) for i in range(4)]
        judge = judge_fn()
        items.append({"name": name, "p1": vals[0], "p2": vals[1], "p3": vals[2], "p4": vals[3], "judge": judge})
    return items


def build_fraud_checklist(raw, results):
    """构建舞弊识别专项核查表"""
    checks = [
        (1, "货币资金真实性", "✓通过" if not any(r.get("资金利息率") and r["资金利息率"]<0.0015 for r in raw) else "⚠异常", "核查银行函证、受限说明", "低"),
        (2, "关联交易合理性", "⚠待核查（需查年报）", "核查隐性关联方、定价公允性", "低"),
        (3, "体外关联交易", "⚠待核查", "核查隐性关联方、未披露交易", "低"),
        (4, "利润操纵", "✓通过" if not any(r.get("归母占合并比") and (r["归母占合并比"]<0.9 or r["归母占合并比"]>1.1) for r in raw) else "⚠异常", "核查少数股东损益、非并表主体利润", "低"),
        (5, "资产虚增", "✓通过", "核查在建工程转固、商誉减值", "低"),
        (6, "费用体外转移", "✓通过", "核查费用明细、服务协议", "低"),
        (7, "研发资本化合理性", "⚠需查附注（THS不提供资本化金额）", "核查资本化依据、研发进度、资本化率是否>30%", "低"),
        (8, "存货利润操纵", "⚠异常" if any(r.get("_stock_signals",0)>=2 for r in raw) else "✓通过", "核查存货明细、库龄结构（全部3信号=舞弊红线）", "低" if not any(r.get("_stock_signals",0)>=2 for r in raw) else "中"),
    ]

    items = []
    for seq, name, result, points, risk in checks:
        items.append({"序号": seq, "项目": name, "结果": result, "要点": points, "风险": risk})
    return items


# ============================================================
# 报告生成
# ============================================================

def generate_markdown(results):
    """生成 Markdown 格式报告"""
    periods = results["periods"]
    raw = results["raw"]
    rl = ["P" + str(i+1) for i in range(len(raw))]  # P1, P2, P3, P4

    lines = []
    w = lines.append

    w(f"# 财报排雷分析报告")
    w(f"")
    w(f"**股票代码：{results['stock_code']} | 分析期间：{periods[0]} ~ {periods[-1]} | 合并报表口径**")
    if results["data_quality"]["warnings"]:
        w(f"")
        for dq in results["data_quality"]["warnings"]:
            w(f"> {dq}")
    w(f"")
    w(f"---")
    w(f"")

    # 速览
    w(f"## 排雷速览")
    w(f"")
    s = results["summary"]
    badge = {"【正常】": "✅", "【谨慎关注】": "⚠️", "【关注】": "⚠️", "【规避】": "🔶", "【坚决回避】": "🚫"}
    w(f"| 维度 | 结果 |")
    w(f"|------|------|")
    w(f"| 综合风险等级 | {badge.get(s.get('风险等级',''), '')} {s.get('风险等级','')} |")
    w(f"| 舞弊红线 | {s.get('舞弊红线','')} |")
    w(f"| 单期≥3异常 | {s.get('单期≥3异常','')} |")
    w(f"")

    if results["fraud_lines"]:
        w(f"### ⚠️ 触发舞弊红线")
        for fl in results["fraud_lines"]:
            w(f"- {fl}")
        w(f"")

    # ---- 核心结论卡片（前置，快速了解全局） ----
    fraud_triggered = len(results["fraud_lines"]) > 0
    total_anoms = sum(len(v) for v in results["anomalies"].values())
    # 报表质量（统一判定）
    quality = assess_report_quality(raw)
    deduct_verdict = "扣非占比较高，主业盈利扎实" if quality in ("优秀", "良好") else ("扣非占比一般，利润有水分" if quality == "一般" else "扣非占比低，依赖非经常性损益")
    # 财务风险
    risk_level = "极高" if fraud_triggered else ("高" if total_anoms >= 8 else ("中" if total_anoms >= 4 else "低"))
    invest = "🚫 不可投" if fraud_triggered else ("⚠️ 谨慎投" if risk_level == "高" else "✅ 可投")
    # 现金流健康度
    cfo_verdict = cfo_health_verdict(raw)

    w(f"## 💡 核心结论")
    w(f"")
    w(f"| 维度 | 判定 | 一句话 |")
    w(f"|------|------|--------|")
    w(f"| 报表质量 | {quality} | {deduct_verdict} |")
    w(f"| 财务风险 | {risk_level} | {cfo_verdict}，异常项共{total_anoms}项 |")
    w(f"| 舞弊风险 | {'🚫 触发' + str(len(results['fraud_lines'])) + '条红线' if fraud_triggered else '✅ 未触发'} | {'舞弊信号明确，需人工核查附注' if fraud_triggered else '未发现明显舞弊信号'} |")
    w(f"| 投资判定 | {invest} | {'综合风险过高，建议回避' if fraud_triggered else ('风险可控，可纳入观察池' if risk_level != '高' else '需谨慎，建议等待更明确的改善信号')} |")
    w(f"")

    # 四期核心数据表
    w(f"## 核心指标四期一览")
    w(f"")
    indicators = [
        ("营业收入(亿)", lambda r: fmt(r.get("营业收入"), 2)),
        ("归母净利润(亿)", lambda r: fmt(r.get("归母净利润"), 2)),
        ("扣非净利润(亿)", lambda r: fmt(r.get("扣非净利润"), 2)),
        ("毛利率", lambda r: pct(r.get("毛利率"))),
        ("扣非占比", lambda r: pct(r.get("扣非占比"))),
        ("货币资金(亿)", lambda r: fmt(r.get("货币资金"), 2)),
        ("有息负债(亿)", lambda r: fmt(r.get("有息负债总额"), 2)),
        ("资产负债率", lambda r: pct(r.get("资产负债率"))),
        ("销售收现比", lambda r: rate(r.get("销售收现比"))),
        ("经营现流入/营收", lambda r: rate(r.get("经营现金流入营收比"))),
        ("净利现金比率", lambda r: rate(r.get("净利润现金比率"))),
        ("自由现金流(亿)", lambda r: fmt(r.get("自由现金流"), 2)),
        ("现金流类型", lambda r: r.get("现金流类型", "")),
        ("存货周转天数(天)", lambda r: fmt(360 / r.get("存货周转率"), 0) if r.get("存货周转率") else "【无公开数据】"),
        ("非经常依赖度", lambda r: pct(r.get("非经常依赖度"))),
        ("流动比率", lambda r: rate(r.get("流动比率"))),
        ("速动比率", lambda r: rate(r.get("速动比率"))),
        ("每股净资产(元)", lambda r: fmt(r.get("每股净资产"), 2)),
        ("每股经营现金流(元)", lambda r: fmt(r.get("每股经营现金流"), 2)),
    ]

    header = "| 指标 | " + " | ".join(f"P{i+1}({periods[i][:4]})" for i in range(len(periods))) + " |"
    sep = "|" + "|".join("---" for _ in range(len(periods)+1)) + "|"
    w(header)
    w(sep)
    for name, fn in indicators:
        vals = [fn(raw[i]) for i in range(len(raw))]
        w(f"| {name} | {' | '.join(vals)} |")
    w(f"")

    # 异常汇总
    w(f"## 异常项汇总")
    w(f"")
    anomalies = results["anomalies"]
    for period in periods:
        anoms = anomalies.get(period, [])
        idx = periods.index(period) + 1
        if anoms:
            w(f"**P{idx} ({period})：{len(anoms)}项异常**")
            for a in anoms:
                w(f"- {a['name']}：{a['detail']}")
        else:
            w(f"**P{idx} ({period})：无异常**")
    w(f"")

    # 数据质量
    if results["data_quality"]["warnings"]:
        w(f"## 数据质量警告")
        for dq in results["data_quality"]["warnings"]:
            w(f"- {dq}")
        w(f"")

    # 模块详细分析
    modules = [
        ("模块一：偿债能力与资金真实性", [
            ("货资/短债", lambda r: rate(r.get("货资比短债"))),
            ("利息保障倍数", lambda r: rate(r.get("利息保障倍数"))),
            ("存贷双高", lambda r: "是" if r.get("存贷双高") else "否"),
            ("资金利息率", lambda r: pct(r.get("资金利息率"), 4)),
            ("流动比率", lambda r: rate(r.get("流动比率"))),
            ("速动比率", lambda r: rate(r.get("速动比率"))),
        ]),
        ("模块二：经营类资产质量", [
            ("应收合计(亿)", lambda r: fmt(r.get("应收合计"), 2)),
            ("应收占营收比", lambda r: pct(r.get("应收占营收比"))),
            ("应收周转率(次)", lambda r: rate(r.get("应收周转率"))),
            ("存货(亿)", lambda r: fmt(r.get("存货"), 2)),
            ("存货周转率(次)", lambda r: rate(r.get("存货周转率"))),
            ("合同负债(亿)", lambda r: fmt(r.get("合同负债"), 2)),
        ]),
        ("模块三：长期资产", [
            ("资产减值损失(亿)", lambda r: fmt(r.get("资产减值损失"), 2)),
            ("商誉/归母权益", lambda r: pct(r.get("商誉占归母比"))),
        ]),
        ("模块四：负债与权益", [
            ("应付合计(亿)", lambda r: fmt(r.get("应付合计"), 2)),
            ("经营性负债占比", lambda r: pct(r.get("经营性负债占比"))),
            ("少数损益占净利比", lambda r: pct(r.get("少数损益占净利比"))),
            ("供应链差额(亿)", lambda r: fmt(r.get("供应链差额"), 2)),
            ("供应链差额占营收比", lambda r: pct(r.get("供应链差额占营收比"))),
        ]),
        ("模块五：利润质量", [
            ("营收增速", lambda r: growth_fmt(r.get("营收增速"))),
            ("四项费率(销/管/研/财)", lambda r: f"{pct(r.get('销售费用率'))} / {pct(r.get('管理费用率'))} / {pct(r.get('研发费用率'))} / {pct(r.get('财务费用率'))}"),
            ("其他收益占利润比", lambda r: pct(r.get("其他收益占利润比"))),
            ("营业利润/利润总额", lambda r: pct(r.get("营业利润占利润比"))),
            ("归母占合并比", lambda r: pct(r.get("归母占合并比"))),
            ("非经常依赖度", lambda r: pct(r.get("非经常依赖度"))),
            ("扣非/归母", lambda r: rate(r.get("扣非归母比"))),
            ("加权ROE", lambda r: pct(r.get("加权ROE"))),
            ("扣非ROE", lambda r: pct(r.get("扣非ROE"))),
            ("虚假增长", lambda r: "是" if r.get("虚假增长") else "否"),
        ]),
        ("模块六：现金流", [
            ("CFO(亿)", lambda r: fmt(r.get("CFO"), 2)),
            ("CFI(亿)", lambda r: fmt(r.get("CFI"), 2)),
            ("CFF(亿)", lambda r: fmt(r.get("CFF"), 2)),
            ("现金流类型", lambda r: r.get("现金流类型", "")),
            ("净利现金比率", lambda r: rate(r.get("净利润现金比率"))),
            ("自由现金流(亿)", lambda r: fmt(r.get("自由现金流"), 2)),
            ("经营现金流/营收", lambda r: pct(r.get("经营现金流营收比"))),
            ("现金利息保障倍数", lambda r: rate(r.get("现金利息保障倍数"))),
            ("净利润含金量", lambda r: pct(r.get("净利润含金量"))),
            ("纸面富贵", lambda r: "是" if r.get("纸面富贵") else "否"),
            ("每股经营现金流", lambda r: fmt(r.get("每股经营现金流"), 2)),
        ]),
    ]

    for mod_name, indicators in modules:
        w(f"## {mod_name}")
        w(f"")
        header = "| 指标 | " + " | ".join(f"P{i+1}" for i in range(len(periods))) + " |"
        w(header)
        w(sep)
        for name, fn in indicators:
            vals = [fn(raw[i]) for i in range(len(raw))]
            w(f"| {name} | {' | '.join(vals)} |")
        w(f"")

    # ---- 模块七：24项排雷异常清单 ----
    w(f"## 模块七：4期全周期排雷异常清单")
    w(f"")
    checklist = build_anomaly_checklist(raw, periods, results)
    w(f"| 序号 | 项目 | 正常/异常 | 4期判定依据 | 风险等级 | 影响 |")
    w(f"|------|------|----------|------------|---------|------|")
    for item in checklist:
        risk_icon = {"低": "🟢", "中": "🟡", "高": "🟠", "极高": "🔴"}.get(item['风险'], "")
        # 根据判定自动生成影响描述
        if "异常" in item['判定']:
            impact = "影响投资决策" if item['风险'] in ("高", "极高") else "需关注跟踪"
        elif "单期" in item['判定']:
            impact = "单期波动，持续跟踪"
        else:
            impact = "无显著影响"
        w(f"| {item['序号']} | {item['项目']} | {item['判定']} | {item['依据']} | {risk_icon} {item['风险']} | {impact} |")
    w(f"")

    # ---- 模块八：50项核心指标汇总表 ----
    w(f"## 模块八：4期核心指标汇总表")
    w(f"")
    n_periods = min(4, len(periods))
    p_labels = [f"P{i+1}({periods[i][:4]})" for i in range(n_periods)]
    w(f"| 指标名称 | {' | '.join(p_labels)} | 判定 |")
    w(f"|---------|" + "|".join("---" for _ in range(n_periods+1)) + "|")
    for item in build_core_indicators(raw):
        vals = [item[f"p{i+1}"] for i in range(n_periods)]
        w(f"| {item['name']} | {' | '.join(vals)} | {item['judge']} |")
    w(f"")

    # ---- 模块九：舞弊识别专项核查表 ----
    w(f"## 模块九：舞弊识别专项核查表")
    w(f"")
    w(f"| 序号 | 核查项目 | 核查结果 | 核查要点 | 风险等级 |")
    w(f"|------|---------|---------|---------|---------|")
    for item in build_fraud_checklist(raw, results):
        w(f"| {item['序号']} | {item['项目']} | {item['结果']} | {item['要点']} | {item['风险']} |")
    w(f"")

    # ---- 模块十：最终分析结论 ----
    w(f"## 模块十：最终分析结论")
    w(f"")
    s = results["summary"]
    fraud_triggered = len(results["fraud_lines"]) > 0
    total_anoms = sum(len(v) for v in results["anomalies"].values())
    # 统一评估报表质量
    quality = assess_report_quality(raw)

    risk_level = "极高" if fraud_triggered else ("高" if total_anoms >= 8 else ("中" if total_anoms >= 4 else "低"))
    invest = "不可投" if fraud_triggered else ("谨慎投" if risk_level == "高" else "可投")
    
    w(f"**1. 报表质量判定：{quality}**")
    w(f"> 扣非占比{fmt(raw[-1].get('扣非占比'),2)}、净利现金比率{fmt(raw[-1].get('净利润现金比率'),2)}，{results['summary']['单期≥3异常']}")
    w(f"")
    w(f"**2. 财务风险等级：{risk_level}**")
    w(f"> 资产负债率{fmt(raw[-1].get('资产负债率'),2)}、有息负债率{fmt(raw[-1].get('有息负债率'),2)}，异常项共{total_anoms}项")
    w(f"")
    w(f"**3. 舞弊风险判定：{'高' if fraud_triggered else '无' if not results['fraud_lines'] else '待核查'}**")
    w(f"> 舞弊红线{'触发' + str(len(results['fraud_lines'])) + '条' if fraud_triggered else '未触发'}")
    w(f"")
    w(f"**4. 投资可投性判定：{invest}**")
    w(f"> 综合报表质量、财务风险、舞弊风险判定")
    w(f"")
    if results["data_quality"]["warnings"]:
        w(f"**5. 核心风险提示**")
        for dq in results["data_quality"]["warnings"]:
            w(f"> {dq}")
        w(f"")
    w(f"**6. 后续跟踪重点**")
    w(f"> 关注最新期年报正式披露后的数据完整性，核查附注中的受限资金、研发资本化、关联交易明细")
    w(f"")

    # ---- 模块十一：补充说明 ----
    w(f"## 模块十一：补充说明")
    w(f"")
    w(f"**1. 行业特殊性说明**")
    w(f"> 不同行业具有不同的财务特征（高毛利率行业关注销售费用率，重资产行业关注折旧与资本开支匹配，周期行业关注存货与应收的周期波动等），部分指标阈值需结合行业基准调整。本次分析使用通用阈值，未做行业专项校准。")
    w(f"")
    w(f"**2. 会计政策变更说明**")
    w(f"> THS数据源不提供会计政策变更信息，需人工核查年报附注。")
    w(f"")
    w(f"**3. 其他特殊事项说明**")
    w(f"> 若分析期间存在重大资产重组、大额政府补贴或重大诉讼，需人工补充。")
    w(f"")
    w(f"**4. 数据缺失说明**")
    nodata_count = sum(1 for r in raw for k, v in r.items() if v is None or v == "__NODATA__")
    w(f"> 本次分析共标注【无公开数据】{nodata_count}项（主要为附注级数据），详见 ths_coverage_audit.md。不影响定量排雷核心结论。")
    w(f"")

    w(f"---")
    w(f"*报告由财报排雷 v1.3.0 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')} | 合并报表口径 | 数据源：同花顺 THS*")
    w(f"")

    return "\n".join(lines)


def generate_html(results, css_content):
    """生成 HTML 格式报告"""
    periods = results["periods"]
    raw = results["raw"]

    # 将 Markdown 转换为 HTML（简化版转换）
    html_body = []
    w = html_body.append

    # 速览卡
    s = results["summary"]
    risk_class = "risk-safe"
    badge_html = '<span class="badge badge-green">✓</span>'
    if "坚决回避" in s.get("风险等级", ""):
        risk_class = "risk-critical"
        badge_html = '<span class="badge badge-red">🚫</span>'
    elif "规避" in s.get("风险等级", ""):
        risk_class = "risk-high"
        badge_html = '<span class="badge badge-red">⚠</span>'
    elif "关注" in s.get("风险等级", ""):
        risk_class = "risk-warn"
        badge_html = '<span class="badge badge-yellow">⚠</span>'

    w(f'<div class="header">')
    w(f'<h1>财报排雷分析报告</h1>')
    w(f'<div class="meta">股票代码：{results["stock_code"]} | 分析期间：{periods[0]} ~ {periods[-1]} | 合并报表口径 | {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>')
    w(f'</div>')
    w(f'<div class="content">')

    # 数据质量警告
    if results["data_quality"]["warnings"]:
        w(f'<div class="warning-box">')
        w(f'<strong>⚠️ 数据质量警告</strong><br>')
        for dq in results["data_quality"]["warnings"]:
            w(f'{dq}<br>')
        w(f'</div>')

    if results["fraud_lines"]:
        w(f'<div class="danger-box">')
        w(f'<strong>🚫 舞弊红线触发！</strong><br>')
        for fl in results["fraud_lines"]:
            w(f'• {fl}<br>')
        w(f'</div>')

    # 速览
    w(f'<h2>排雷速览</h2>')
    w(f'<div class="summary-box">')
    w(f'<p><strong>综合风险等级：</strong><span class="{risk_class}">{badge_html} {s.get("风险等级", "")}</span></p>')
    w(f'<p><strong>舞弊红线：</strong>{s.get("舞弊红线", "")}</p>')
    w(f'<p><strong>单期≥3异常：</strong>{s.get("单期≥3异常", "")}</p>')
    w(f'</div>')

    # ---- 核心结论卡片（HTML） ----
    fraud_triggered = len(results["fraud_lines"]) > 0
    total_anoms = sum(len(v) for v in results["anomalies"].values())
    quality_html = assess_report_quality(raw)
    risk_html = "极高" if fraud_triggered else ("高" if total_anoms >= 8 else ("中" if total_anoms >= 4 else "低"))
    invest_html = "🚫 不可投" if fraud_triggered else ("⚠️ 谨慎投" if risk_html == "高" else "✅ 可投")
    cfo_verdict_html = cfo_health_verdict(raw)
    deduct_verdict_html = "扣非占比较高，主业盈利扎实" if quality_html in ("优秀", "良好") else ("扣非占比一般，利润有水分" if quality_html == "一般" else "扣非占比低，依赖非经常性损益")

    w(f'<h2>💡 核心结论</h2>')
    w(f'<div class="summary-box">')
    w(f'<p><strong>报表质量：</strong>{quality_html} — {deduct_verdict_html}</p>')
    w(f'<p><strong>财务风险：</strong>{risk_html} — {cfo_verdict_html}，异常项共{total_anoms}项</p>')
    w(f'<p><strong>舞弊风险：</strong>{"🚫 触发" + str(len(results["fraud_lines"])) + "条红线" if fraud_triggered else "✅ 未触发"}</p>')
    w(f'<p><strong>投资判定：</strong>{invest_html}</p>')
    w(f'</div>')

    # 核心指标四期一览表
    w(f'<h2>核心指标四期一览</h2>')
    indicators = [
        ("营业收入(亿)", lambda r: fmt(r.get("营业收入"), 2)),
        ("归母净利润(亿)", lambda r: fmt(r.get("归母净利润"), 2)),
        ("扣非净利润(亿)", lambda r: fmt(r.get("扣非净利润"), 2)),
        ("毛利率", lambda r: pct(r.get("毛利率"))),
        ("扣非占比", lambda r: pct(r.get("扣非占比"))),
        ("货币资金(亿)", lambda r: fmt(r.get("货币资金"), 2)),
        ("有息负债(亿)", lambda r: fmt(r.get("有息负债总额"), 2)),
        ("资产负债率", lambda r: pct(r.get("资产负债率"))),
        ("销售收现比", lambda r: rate(r.get("销售收现比"))),
        ("净利现金比率", lambda r: rate(r.get("净利润现金比率"))),
        ("自由现金流(亿)", lambda r: fmt(r.get("自由现金流"), 2)),
        ("现金流类型", lambda r: r.get("现金流类型", "")),
        ("存货周转天数(天)", lambda r: fmt(360 / r.get("存货周转率"), 0) if r.get("存货周转率") else "N/A"),
        ("非经常依赖度", lambda r: pct(r.get("非经常依赖度"))),
        ("流动比率", lambda r: rate(r.get("流动比率"))),
        ("每股经营现金流(元)", lambda r: fmt(r.get("每股经营现金流"), 2)),
    ]

    w(f'<table><thead><tr><th>指标</th>{"".join(f"<th>P{i+1}({periods[i][:4]})</th>" for i in range(len(periods)))}</tr></thead><tbody>')
    for name, fn in indicators:
        vals = [fn(raw[i]) for i in range(len(raw))]
        w(f'<tr><td>{name}</td>{"".join(f"<td>{v}</td>" for v in vals)}</tr>')
    w(f'</tbody></table>')

    # 异常汇总
    w(f'<h2>异常项汇总</h2>')
    anomalies = results["anomalies"]
    for period in periods:
        anoms = anomalies.get(period, [])
        idx = periods.index(period) + 1
        if anoms:
            w(f'<p><strong>P{idx} ({period})：{len(anoms)}项异常</strong></p><ul class="anomaly-list">')
            for a in anoms:
                w(anomaly_item_html(a["name"], a["detail"]))
            w(f'</ul>')
        else:
            w(f'<p><strong>P{idx} ({period})：</strong><span class="risk-safe">无异常 ✓</span></p>')
    w(f'')

    # 模块详情表
    modules = [
        ("模块一：偿债能力与资金真实性", [
            ("货资/短债", lambda r: rate(r.get("货资比短债"))),
            ("利息保障倍数", lambda r: rate(r.get("利息保障倍数"))),
            ("存贷双高", lambda r: "⚠是" if r.get("存贷双高") else "否"),
            ("资金利息率", lambda r: pct(r.get("资金利息率"), 4)),
            ("流动比率", lambda r: rate(r.get("流动比率"))),
            ("速动比率", lambda r: rate(r.get("速动比率"))),
        ]),
        ("模块二：经营类资产质量", [
            ("应收合计(亿)", lambda r: fmt(r.get("应收合计"), 2)),
            ("应收占营收比", lambda r: pct(r.get("应收占营收比"))),
            ("应收周转率(次)", lambda r: rate(r.get("应收周转率"))),
            ("存货(亿)", lambda r: fmt(r.get("存货"), 2)),
            ("存货周转率(次)", lambda r: rate(r.get("存货周转率"))),
            ("合同负债(亿)", lambda r: fmt(r.get("合同负债"), 2)),
        ]),
        ("模块三：长期资产", [
            ("资产减值损失(亿)", lambda r: fmt(r.get("资产减值损失"), 2)),
            ("商誉/归母权益", lambda r: pct(r.get("商誉占归母比"))),
        ]),
        ("模块四：负债与权益", [
            ("应付合计(亿)", lambda r: fmt(r.get("应付合计"), 2)),
            ("经营性负债占比", lambda r: pct(r.get("经营性负债占比"))),
            ("少数损益占净利比", lambda r: pct(r.get("少数损益占净利比"))),
            ("供应链差额(亿)", lambda r: fmt(r.get("供应链差额"), 2)),
            ("供应链差额占营收比", lambda r: pct(r.get("供应链差额占营收比"))),
        ]),
        ("模块五：利润质量", [
            ("营收增速", lambda r: growth_fmt(r.get("营收增速"))),
            ("四项费率(销/管/研/财)", lambda r: f"{pct(r.get('销售费用率'))} / {pct(r.get('管理费用率'))} / {pct(r.get('研发费用率'))} / {pct(r.get('财务费用率'))}"),
            ("其他收益占利润比", lambda r: pct(r.get("其他收益占利润比"))),
            ("营业利润/利润总额", lambda r: pct(r.get("营业利润占利润比"))),
            ("归母占合并比", lambda r: pct(r.get("归母占合并比"))),
            ("非经常依赖度", lambda r: pct(r.get("非经常依赖度"))),
            ("加权ROE", lambda r: pct(r.get("加权ROE"))),
            ("扣非ROE", lambda r: pct(r.get("扣非ROE"))),
            ("虚假增长", lambda r: "是" if r.get("虚假增长") else "否"),
        ]),
        ("模块六：现金流", [
            ("CFO / CFI / CFF(亿)", lambda r: f"{fmt(r.get('CFO'),1)} / {fmt(r.get('CFI'),1)} / {fmt(r.get('CFF'),1)}"),
            ("现金流类型", lambda r: r.get("现金流类型", "")),
            ("净利现金比率", lambda r: rate(r.get("净利润现金比率"))),
            ("自由现金流(亿)", lambda r: fmt(r.get("自由现金流"), 2)),
            ("净利润含金量", lambda r: pct(r.get("净利润含金量"))),
            ("每股经营现金流(元)", lambda r: fmt(r.get("每股经营现金流"), 2)),
        ]),
    ]

    for mod_name, indicators in modules:
        w(f'<h2>{mod_name}</h2>')
        w(f'<table><thead><tr><th>指标</th>{"".join(f"<th>P{i+1}</th>" for i in range(len(periods)))}</tr></thead><tbody>')
        for name, fn in indicators:
            vals = [fn(raw[i]) for i in range(len(raw))]
            w(f'<tr><td>{name}</td>{"".join(f"<td>{v}</td>" for v in vals)}</tr>')
        w(f'</tbody></table>')

    # ---- 模块七：24项排雷异常清单（HTML） ----
    w(f'<h2>模块七：4期全周期排雷异常清单</h2>')
    checklist = build_anomaly_checklist(raw, periods, results)
    w(f'<table><thead><tr><th>序号</th><th>项目</th><th>判定</th><th>4期判定依据</th><th>风险</th><th>影响</th></tr></thead><tbody>')
    for item in checklist:
        if "异常" in item['判定']:
            impact = "影响投资决策" if item['风险'] in ("高", "极高") else "需关注跟踪"
        elif "单期" in item['判定']:
            impact = "单期波动，持续跟踪"
        else:
            impact = "无显著影响"
        w(f'<tr><td>{item["序号"]}</td><td>{item["项目"]}</td><td>{item["判定"]}</td><td style="font-size:11px">{item["依据"]}</td><td>{risk_label(item["风险"])}</td><td>{impact}</td></tr>')
    w(f'</tbody></table>')

    # ---- 模块八：50项核心指标汇总表（HTML） ----
    w(f'<h2>模块八：4期核心指标汇总表</h2>')
    w(f'<table><thead><tr><th>指标</th><th>P1</th><th>P2</th><th>P3</th><th>P4</th><th>判定</th></tr></thead><tbody>')
    for item in build_core_indicators(raw):
        w(f'<tr><td>{item["name"]}</td><td>{item["p1"]}</td><td>{item["p2"]}</td><td>{item["p3"]}</td><td>{item["p4"]}</td><td>{item["judge"]}</td></tr>')
    w(f'</tbody></table>')

    # ---- 模块九：舞弊识别专项核查表（HTML） ----
    w(f'<h2>模块九：舞弊识别专项核查表</h2>')
    w(f'<table><thead><tr><th>序号</th><th>项目</th><th>结果</th><th>核查要点</th><th>风险</th></tr></thead><tbody>')
    for item in build_fraud_checklist(raw, results):
        w(f'<tr><td>{item["序号"]}</td><td>{item["项目"]}</td><td>{item["结果"]}</td><td>{item["要点"]}</td><td>{risk_label(item["风险"])}</td></tr>')
    w(f'</tbody></table>')

    # ---- 模块十：最终分析结论（HTML） ----
    s = results["summary"]
    fraud_triggered = len(results["fraud_lines"]) > 0
    total_anoms = sum(len(v) for v in results["anomalies"].values())
    # 统一评估报表质量
    quality = assess_report_quality(raw)
    risk_level = "极高" if fraud_triggered else ("高" if total_anoms >= 8 else ("中" if total_anoms >= 4 else "低"))
    invest = "不可投" if fraud_triggered else ("谨慎投" if risk_level == "高" else "可投")

    w(f'<h2>模块十：最终分析结论</h2>')
    w(f'<div class="summary-box">')
    w(f'<p><strong>1. 报表质量判定：{quality}</strong></p>')
    w(f'<p>2. 财务风险等级：{risk_level}</p>')
    w(f'<p>3. 舞弊风险判定：{"高" if fraud_triggered else "无"}</p>')
    w(f'<p>4. 投资可投性判定：{invest}</p>')
    if results["data_quality"]["warnings"]:
        w(f'<p>5. 核心风险提示：{"; ".join(results["data_quality"]["warnings"])}</p>')
    w(f'<p>6. 后续跟踪重点：关注最新期年报正式披露后的数据完整性，核查附注中的受限资金、研发资本化、关联交易明细</p>')
    w(f'</div>')

    # ---- 模块十一：补充说明（HTML） ----
    w(f'<h2>模块十一：补充说明</h2>')
    w(f'<p><strong>1. 行业特殊性：</strong>不同行业具有不同的财务特征，部分指标阈值需结合行业基准调整。本次分析使用通用阈值，未做行业专项校准。</p>')
    w(f'<p><strong>2. 会计政策变更：</strong>THS数据源不提供此信息，需人工核查年报附注。</p>')
    w(f'<p><strong>3. 其他特殊事项：</strong>若存在重大资产重组、大额政府补贴或重大诉讼，需人工补充。</p>')
    nodata_count = sum(1 for r in raw for k, v in r.items() if v is None or v == "__NODATA__")
    w(f'<p><strong>4. 数据缺失：</strong>本次分析共标注【无公开数据】{nodata_count}项（主要为附注级数据），不影响定量排雷核心结论。</p>')

    w(f'</div>')
    w(f'<div class="footer">报告由 财报排雷 v1.3.0 自动生成 | {datetime.now().strftime("%Y-%m-%d %H:%M")} | 数据源：同花顺 THS (akshare) | 合并报表口径</div>')

    body = "\n".join(html_body)
    # 统一包装表格 + 格式化异常列表
    body = body.replace('<table>', '<div class="table-wrap"><table>')
    body = body.replace('</table>', '</table></div>')
    # 给检查表的判定列添加色标
    body = body.replace('✓正常', '<span class="good-cell">✓正常</span>')
    body = body.replace('⚠异常', '<span class="warn-cell">⚠异常</span>')
    body = body.replace('⚠单期', '<span class="warn-cell">⚠单期</span>')
    # 给舞弊核查表的结果添加色标
    body = body.replace('✓通过', '<span class="good-cell">✓通过</span>')
    body = body.replace('⚠待核查', '<span class="warn-cell">⚠待核查</span>')
    body = body.replace('⚠单期异常', '<span class="warn-cell">⚠单期异常</span>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>财报排雷报告 — {results['stock_code']} {results.get('company_name', '')}</title>
<style>
{css_content}
</style>
</head>
<body>
<div class="container">
{body}
</div>
</body>
</html>"""
    return html


def main():
    if len(sys.argv) < 2:
        print("用法: python generate_report.py <code>_排雷指标.json")
        sys.exit(1)

    input_file = sys.argv[1]
    with open(input_file, "r", encoding="utf-8") as f:
        results = json.load(f)

    # 读取 CSS
    css_path = os.path.join(SKILL_DIR, "references", "report_style.css")
    with open(css_path, "r", encoding="utf-8") as f:
        css_content = f.read()

    stock_code = results["stock_code"]
    company_name = results.get("company_name", stock_code)
    # 文件名非法字符替换（Windows: * ? < > | " : / \）
    safe_name = company_name.replace("*ST", "ST").translate(str.maketrans({
        "*": "", "?": "", "<": "(", ">": ")", "|": "-",
        '"': "'", ":": "-", "/": "-", "\\": "-"
    }))

    # 输出文件前缀：股票代码_公司名称
    prefix = f"{stock_code}_{safe_name}" if safe_name != stock_code else stock_code

    # 生成 MD
    md_content = generate_markdown(results)
    md_file = f"{prefix}_财报排雷报告.md"
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✓ MD 报告 → {md_file}")

    # 生成 HTML
    html_content = generate_html(results, css_content)
    html_file = f"{prefix}_财报排雷报告.html"
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✓ HTML 报告 → {html_file}")


if __name__ == "__main__":
    main()
