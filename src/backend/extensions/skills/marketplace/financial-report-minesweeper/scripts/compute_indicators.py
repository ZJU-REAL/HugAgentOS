#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — 指标计算引擎
读取 download_statements.py 输出的JSON，按模块一至六规则计算全部指标

用法：
    python compute_indicators.py <code>_合并财报数据.json
"""

import json
import sys
from datetime import datetime

# 强制UTF-8输出（Windows GBK兼容）
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# 配置常量（可调阈值）
# ============================================================
CHG_WARNING = 30             # 营收/利润/CFO 邻期突变告警阈值 (%)
SHORT_DEBT_ASSET_HIGH = 0.2  # 短债/资产 > 此值视为偏高
RECEIVABLE_REVENUE_HIGH = 0.4  # 应收/营收 > 此值视为高风险
RECEIVABLE_MIN_MATERIAL = 0.05  # 应收/营收 < 此值跳过增速检查
INVENTORY_REVENUE_HIGH = 0.10  # 存货/营收 > 此值视为偏高
TURNOVER_DECLINE = 0.3       # 周转率下降 > 30% 告警
PPE_LONG_TERM = 0.05         # 在建工程/资产 > 5% 且增长告警
PPE_HIGH = 0.10              # 在建工程/资产 > 10% 告警
DEBT_RATIO_HIGH = 0.7        # 资产负债率 > 70% 高危
DEBT_RATIO_SURGE = 0.1       # 资产负债率单期上升 > 10pp 告警
INTEREST_DEBT_HIGH = 0.5     # 有息负债/资产 > 50% 且利息保障<2
SILVER_NOTE_RATIO = 0.8      # 应收票据/应收合计 > 80% 判定银票
SILVER_NOTE_AR_MAX = 0.05    # 应收账款/营收 < 5% 判定银票
MARGIN_SURGE = 0.05          # 毛利率单期波动 > 5pp 告警
OTHER_INCOME_HIGH = 0.5      # 其他收益/利润总额 > 50% 主业失效
NONRECUR_HIGH = 0.5          # 非经常依赖度 > 50% 高风险
ROE_HIGH = 0.15              # 加权ROE > 15% 优秀
ROE_LOW = 0.10               # 扣非ROE < 10% 偏低
LOAN_RATIO_HIGH = 0.8        # 借款/筹资流入 > 80% 告警
OVERDIVIDEND = 0.5           # 每股经营现金流<0 且分红率>50% 透支
CFO_MIN = 0.01               # CFO 绝对值低于此跳过验证（亿）
CFO_DIFF = 0.05              # 间接法 vs 直接法 CFO 差异 > 5% 告警
MONEY_YIELD_LOW = 0.0015     # 资金收益率 < 0.15% 舞弊红线
CASH_COVER_LOW = 0.5         # 货资/短债 < 0.5 资金链脆弱
CASH_COVER_WEAK = 0.8        # 净利现金比率 < 0.8 现金流偏低
CASH_COVER_CRIT = 0.5        # 净利现金比率 < 0.5 极差
ASSET_IMPAIR_SURGE = 1.0     # 资产减值损失同比 > 100% 告警
MIN_SIGNAL_AMOUNT = 0.01     # 绝对值低于此的科目忽略（亿）

# ============================================================
# 工具函数
# ============================================================

def get(d, field, p):
    """安全取值，支持多别名回退。返回 None 表示数据缺失。
    field 可以是字符串或字符串列表，按顺序尝试取值。
    """
    fields = field if isinstance(field, list) else [field]
    for f in fields:
        v = d.get(f, {}).get(p)
        if v is None or (isinstance(v, float) and v != v):  # NaN
            continue
        return v
    return None

def safe_div(a, b):
    """安全除法，None/0 除数为 None"""
    if a is None or b is None or b == 0:
        return None
    return a / b

def safe_ratio(numerator, denominator,
               min_denom=0.001,      # 分母最小阈值（亿），低于此值视为无效
               lower=-1000.0,        # 比值下限
               upper=1000.0):        # 比值上限
    """
    安全比值计算，带异常值截断。
    返回 (value, is_valid)
    - is_valid=True: value 是正常计算结果
    - is_valid=False: value=None，需标记【数据异常】
    """
    if numerator is None or denominator is None or abs(denominator) < min_denom:
        return None, False
    raw = numerator / denominator
    if lower <= raw <= upper:
        return raw, True
    return None, False


def growth(new, old):
    """计算增速(%)，第一期返回 None"""
    if old is None or new is None or old == 0:
        return None
    return (new - old) / abs(old) * 100


def max_consecutive_run(raw, field, condition_fn):
    """找到所有期中满足条件的最长连续期数"""
    max_run = 0
    current = 0
    for r in raw:
        val = r.get(field)
        if val is not None and condition_fn(val):
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


def consecutive_run_periods(raw, field, condition_fn):
    """返回所有满足条件的连续期区间列表 [(start_idx, end_idx, length), ...]"""
    runs = []
    start = None
    for i, r in enumerate(raw):
        val = r.get(field)
        if val is not None and condition_fn(val):
            if start is None:
                start = i
        else:
            if start is not None and (i - start) >= 2:
                runs.append((start, i - 1, i - start))
            start = None
    if start is not None and (len(raw) - start) >= 2:
        runs.append((start, len(raw) - 1, len(raw) - start))
    return runs

def count_total(raw, field, condition_fn):
    """统计所有期中满足条件的总期数（不要求连续）"""
    return sum(1 for r in raw if r.get(field) is not None and condition_fn(r[field]))


# ============================================================
# 主计算函数
# ============================================================

def compute_all(data):
    bs = data['balance_sheet']
    pl = data['profit_statement']
    cf = data['cash_flow_statement']
    periods = data['periods']
    stock_code = data['stock_code']
    mode = data.get('mode', 'annual')  # 年报/季报模式，用于利息年化

    results = {"stock_code": stock_code, "company_name": data.get("company_name", stock_code),
               "periods": periods,
               "period_labels": ["第一期", "第二期", "第三期", "第四期"],
               "anomalies": {}, "fraud_lines": [],
               "data_quality": {}, "summary": {}}

    # ---- 第一期：提取原始数据 ----
    raw = []
    for i, period in enumerate(periods):
        r = {"_period": period}
        r["货币资金"] = get(bs, "货币资金", period)
        r["利息收入"] = get(pl, "其中：利息收入", period)
        r["利息费用"] = get(pl, "其中：利息费用", period)
        r["短期借款"] = get(bs, "短期借款", period)
        r["一年到期"] = get(bs, "一年内到期的非流动负债", period)
        r["长期借款"] = get(bs, "长期借款", period)
        r["应付债券"] = get(bs, "应付债券", period)
        r["应收票据"] = get(bs, "应收票据", period)
        r["应收账款"] = get(bs, "应收账款", period)
        r["预付款项"] = get(bs, "预付款项", period)
        r["其他应收款"] = get(bs, "其他应收款", period)
        r["存货"] = get(bs, "存货", period)
        r["合同负债"] = get(bs, "合同负债", period)
        r["应收款项融资"] = get(bs, "应收款项融资", period)
        r["合同资产"] = get(bs, "合同资产", period)
        r["租赁负债"] = get(bs, "租赁负债", period)
        r["应付票据"] = get(bs, "应付票据", period)
        r["应付账款"] = get(bs, "应付账款", period)
        r["总资产"] = get(bs, ["资产总计", "资产合计"], period)
        r["总负债"] = get(bs, ["负债总计", "负债合计"], period)
        r["归母净资产"] = get(bs, "归属于母公司所有者权益合计", period)
        r["少数股东权益"] = get(bs, "少数股东权益", period)
        r["营业收入"] = get(pl, "营业收入", period)
        r["营业成本"] = get(pl, "营业成本", period)
        r["净利润"] = get(pl, ["净利润", "四、净利润"], period)
        r["归母净利润"] = get(pl, ["归母净利润", "归属于母公司所有者的净利润"], period)
        r["扣非净利润"] = get(pl, ["扣非净利润", "扣除非经常性损益后的净利润"], period)
        r["少数股东损益"] = get(pl, "少数股东损益", period)
        r["所得税费用"] = get(pl, ["所得税费用", "减：所得税费用"], period)
        r["销售费用"] = get(pl, "销售费用", period)
        r["管理费用"] = get(pl, "管理费用", period)
        r["研发费用"] = get(pl, "研发费用", period)
        r["财务费用"] = get(pl, "财务费用", period)
        r["其他收益"] = get(pl, ["其他收益", "加：其他收益"], period)
        r["利润总额"] = get(pl, ["利润总额", "三、利润总额"], period)
        r["营业利润"] = get(pl, ["营业利润", "二、营业利润"], period)
        r["资产减值损失"] = get(pl, "资产减值损失", period)
        r["资产处置收益"] = get(pl, "资产处置收益", period)
        r["在建工程"] = get(bs, "在建工程", period)
        r["CFO"] = get(cf, "经营活动产生的现金流量净额", period)
        r["CFI"] = get(cf, "投资活动产生的现金流量净额", period)
        r["CFF"] = get(cf, "筹资活动产生的现金流量净额", period)
        r["销售收现"] = get(cf, "销售商品、提供劳务收到的现金", period)
        r["经营现金流入小计"] = get(cf, "经营活动现金流入小计", period)
        r["资本开支"] = get(cf, "购建固定资产、无形资产和其他长期资产支付的现金", period)
        r["支付的各项税费"] = get(cf, "支付的各项税费", period)
        r["分配股利付息"] = get(cf, "分配股利、利润或偿付利息支付的现金", period)
        r["投资收益收现"] = get(cf, "取得投资收益收到的现金", period)
        r["取得借款收现"] = get(cf, "取得借款收到的现金", period)
        r["筹资现金流入小计"] = get(cf, "筹资活动现金流入小计", period)
        r["处置资产收回现金"] = get(cf, "处置固定资产、无形资产和其他长期资产收回的现金净额", period)
        # 间接法（钩稽验证用）
        r["间接净利"] = get(cf, "间接法-净利润", period)
        r["间接减值准备"] = get(cf, "间接法-资产减值准备", period)
        r["间接折旧"] = get(cf, "间接法-固定资产折旧", period)
        r["间接无形资产摊销"] = get(cf, "间接法-无形资产摊销", period)
        r["间接长期摊销"] = get(cf, "间接法-长期待摊费用摊销", period)
        r["间接财务费用"] = get(cf, "间接法-财务费用", period)
        r["间接投资损失"] = get(cf, "间接法-投资损失", period)
        r["间接递延所得税减少"] = get(cf, "间接法-递延所得税资产减少", period)
        r["间接递延负债增加"] = get(cf, "间接法-递延所得税负债增加", period)
        r["间接存货减少"] = get(cf, "间接法-存货的减少", period)
        r["间接应收减少"] = get(cf, "间接法-经营性应收项目的减少", period)
        r["间接应付增加"] = get(cf, "间接法-经营性应付项目的增加", period)

        raw.append(r)

    # ---- 补入质押数据 ----
    pledge_ratio = data.get('pledge_ratio')
    for r in raw:
        r["控股股东质押比例"] = pledge_ratio

    # ---- 补入财务摘要数据（ROE等）----
    abstract = data.get('abstract', {})
    for r in raw:
        period = r["_period"]
        if period in abstract:
            abs_row = abstract[period]
            r["加权ROE"] = abs_row.get("净资产收益率")
            r["流动比率"] = abs_row.get("流动比率")
            r["速动比率"] = abs_row.get("速动比率")
            r["保守速动比率"] = abs_row.get("保守速动比率")
            r["每股净资产"] = abs_row.get("每股净资产")
            r["每股经营现金流"] = abs_row.get("每股经营现金流")
            r["每股未分配利润"] = abs_row.get("每股未分配利润")
            r["每股资本公积金"] = abs_row.get("每股资本公积金")
            r["总资产周转率"] = abs_row.get("总资产周转率")

    # ---- 补入分红数据 ----
    dividend_list = data.get('dividend', [])
    # 构建分红映射：报告期 → 分红总额（同年有多笔则累加）
    dividend_map = {}
    for d in dividend_list:
        period = d["期间"]
        div_amt = d.get("分红总额") or 0
        dividend_map[period] = dividend_map.get(period, 0) + div_amt
    # 如果目标期间无分红，尝试匹配同年年度分红
    for r in raw:
        period = r["_period"]
        r["分红总额"] = dividend_map.get(period)  # 可能为 None
        if r["分红总额"] is None and period.endswith("-12-31"):
            # 年报模式：直接用该年分红
            r["分红总额"] = dividend_map.get(period)
        elif r["分红总额"] is None:
            # 季报模式：匹配该年度的年报分红
            year = period[:4]
            annual_key = f"{year}-12-31"
            r["分红总额"] = dividend_map.get(annual_key)

    # ---- 第二期：计算派生指标 ----
    for i, r in enumerate(raw):
        period = r["_period"]
        # 短期有息负债
        r["短期有息负债"] = (r["短期借款"] or 0) + (r["一年到期"] or 0)
        # 有息负债总额
        r["有息负债总额"] = r["短期有息负债"] + (r["长期借款"] or 0) + (r["应付债券"] or 0) + (r.get("租赁负债") or 0)
        # 应收合计
        r["应收合计"] = (r["应收票据"] or 0) + (r["应收账款"] or 0)
        # 应付合计
        r["应付合计"] = (r["应付票据"] or 0) + (r["应付账款"] or 0)
        # 1.4 货资比短债
        r["货资比短债"] = safe_div(r["货币资金"], r["短期有息负债"])
        if r["货资比短债"] is None and r.get("货币资金") and r.get("货币资金") > 0 and (r["短期有息负债"] == 0):
            # 无短期有息负债 → 用特殊字符串表示
            r["货资比短债"] = "NO_SHORT_DEBT"

        # 1.5 利息保障倍数
        if r["利息费用"] and r["利息费用"] > 0:
            r["利息保障倍数"] = ((r["净利润"] or 0) + (r["所得税费用"] or 0) + r["利息费用"]) / r["利息费用"]
        else:
            r["利息保障倍数"] = None

        # 1.6 存贷双高
        if r["总资产"] is not None and r["总资产"] > 0:
            cond1 = (r["货币资金"] or 0) / r["总资产"] >= 0.1
            cond2 = r["有息负债总额"] / r["总资产"] >= 0.2
        else:
            cond1 = False
            cond2 = False
        if r["利息收入"] and r["利息收入"] > 0:
            ratio = safe_div(r["利息费用"], r["利息收入"])
            cond3 = (ratio or 0) > 3
        else:
            cond3 = False
        r["存贷双高"] = bool(cond1 and cond2 and cond3)

        # 1.7 资金利息率（需按 SKILL.md 1.7 规则年化处理）
        if i > 0:
            prev_mf = raw[i-1].get("货币资金")
        else:
            prev_mf = r["货币资金"]
        avg_mf = ((r["货币资金"] or 0) + (prev_mf or 0)) / 2
        r["平均货币资金"] = avg_mf
        # 年报模式：利息收入为全年发生额，无需调整
        # 季报模式：利息收入为单季数据，需年化 = 利息收入 × (12/3) = ×4
        annual_multiplier = 4 if mode == "quarterly" else 1
        annual_interest = (r["利息收入"] or 0) * annual_multiplier
        r["资金利息率"] = safe_div(annual_interest, avg_mf)

        # 增速（第一期基期）
        if i == 0:
            r["营收增速"] = None; r["应收增速"] = None; r["存货增速"] = None
            r["有息负债增速"] = None; r["净利增速"] = None; r["扣非增速"] = None
            r["应付增速"] = None; r["营业成本增速"] = None
        else:
            prev = raw[i-1]
            r["营收增速"] = growth(r["营业收入"], prev["营业收入"])
            r["应收增速"] = growth(r["应收合计"], prev["应收合计"])
            r["存货增速"] = growth(r["存货"], prev["存货"])
            r["有息负债增速"] = growth(r["有息负债总额"], prev["有息负债总额"])
            r["净利增速"] = growth(r["净利润"], prev["净利润"])
            r["扣非增速"] = growth(r["扣非净利润"], prev["扣非净利润"])
            r["应付增速"] = growth(r["应付合计"], prev["应付合计"])
            r["营业成本增速"] = growth(r["营业成本"], prev["营业成本"])

        # 2.2 应收增速差
        if i >= 1:
            r["应收营收增速差"] = (r["应收增速"] or 0) - (r["营收增速"] or 0)
            r["存货营收增速差"] = (r["存货增速"] or 0) - (r["营收增速"] or 0)

        # 2.3 应收周转率
        prev_rec = raw[i-1]["应收合计"] if i > 0 else r["应收合计"]
        avg_rec = ((r["应收合计"] or 0) + (prev_rec or 0)) / 2
        r["应收周转率"] = safe_div(r["营业收入"], avg_rec)

        # 2.6 存货周转率
        prev_inv = raw[i-1]["存货"] if i > 0 else r["存货"]
        avg_inv = ((r["存货"] or 0) + (prev_inv or 0)) / 2
        r["存货周转率"] = safe_div(r["营业成本"], avg_inv)

        # 2.7 合同负债占营收比
        r["合同负债占营收比"] = safe_div(r["合同负债"], r["营业收入"])
        r["应收占营收比"] = safe_div(r["应收合计"], r["营业收入"])
        r["存货占营收比"] = safe_div(r["存货"], r["营业收入"])

        # 3.1 在建工程分析
        r["在建工程占资产比"] = safe_div(r["在建工程"], r["总资产"])
        if i > 0:
            r["在建工程增速"] = growth(r["在建工程"], raw[i-1]["在建工程"])
        else:
            r["在建工程增速"] = None

        # 3.2 商誉占比
        r["商誉"] = get(bs, "商誉", period)
        r["商誉占归母比"] = safe_div(r["商誉"] or 0, r["归母净资产"])  # None=数据缺失 → 视为0（保守处理）

        # 4.1 资产负债率
        r["资产负债率"] = safe_div(r["总负债"], r["总资产"])
        r["有息负债率"] = safe_div(r["有息负债总额"], r["总资产"])

        # 4.3 经营性负债占比
        r["经营性负债"] = (r["应付票据"] or 0) + (r["应付账款"] or 0) + (r["合同负债"] or 0)
        r["经营性负债占比"] = safe_div(r["经营性负债"], r["总负债"])

        # 4.5 应付周转率
        prev_pay = raw[i-1]["应付合计"] if i > 0 else r["应付合计"]
        avg_pay = ((r["应付合计"] or 0) + (prev_pay or 0)) / 2
        r["应付周转率"] = safe_div(r["营业成本"], avg_pay)

        # 4.6 少数损益占净利比
        r["少数损益占净利比"] = safe_div(r["少数股东损益"], r["净利润"])

        # 4.7 供应链差额
        r["供应链差额"] = ((r["应付票据"] or 0) + (r["应付账款"] or 0) + (r["合同负债"] or 0)) - \
                          ((r["应收票据"] or 0) + (r["应收账款"] or 0) + (r["应收款项融资"] or 0) + (r["预付款项"] or 0) + (r["合同资产"] or 0))
        r["供应链差额占营收比"] = safe_div(r["供应链差额"], r["营业收入"])

        # 5.1.2 毛利率
        r["毛利率"] = safe_div((r["营业收入"] or 0) - (r["营业成本"] or 0), r["营业收入"])

        # 5.1.3 销售收现比
        r["销售收现比"] = safe_div(r["销售收现"], r["营业收入"])

        # 5.2.1 四项费用率
        r["销售费用率"] = safe_div(r["销售费用"], r["营业收入"])
        r["管理费用率"] = safe_div(r["管理费用"], r["营业收入"])
        r["研发费用率"] = safe_div(r["研发费用"], r["营业收入"])
        r["财务费用率"] = safe_div(r["财务费用"], r["营业收入"])

        # 5.2.3 其他收益占利润比
        r["其他收益占利润比"] = safe_div(r["其他收益"], r["利润总额"])

        # 5.2.4 营业利润/利润总额
        r["营业利润占利润比"] = safe_div(r["营业利润"], r["利润总额"])

        # 5.2.5 扣非
        r["扣非占比"], _ = safe_ratio(r["扣非净利润"], r["净利润"], lower=-5.0, upper=5.0)
        # 5.2.8 归母占合并比
        r["合并净利润"] = (r["归母净利润"] or 0) + (r["少数股东损益"] or 0)
        if r["合并净利润"] and r["合并净利润"] != 0:
            r["归母占合并比"] = safe_div(r["归母净利润"], r["合并净利润"])
        else:
            r["归母占合并比"] = None
        # 5.2.7 扣非ROE（扣非净利润 / 平均归母净资产）
        prev_eq = raw[i-1]["归母净资产"] if i > 0 else r["归母净资产"]
        avg_equity = ((r["归母净资产"] or 0) + (prev_eq or 0)) / 2
        r["扣非ROE"] = safe_div(r["扣非净利润"], avg_equity)

        # 5.2.9 非经常依赖度
        r["非经常依赖度"] = safe_div((r["归母净利润"] or 0) - (r["扣非净利润"] or 0), r["归母净利润"])
        # 5.2.10 扣非归母比
        r["扣非归母比"] = safe_div(r["扣非净利润"], r["归母净利润"])

        # 5.2.11 虚假增长
        if i >= 1:
            prev = raw[i-1]
            net_up = r["净利增速"] is not None and r["净利增速"] >= 20
            deduct_down = r["扣非增速"] is not None and r["扣非增速"] <= -10
            r["虚假增长"] = bool(net_up and deduct_down)
        else:
            r["虚假增长"] = False

        # 6.1 现金流类型
        cfo_pos = (r["CFO"] or 0) > 0
        cfi_pos = (r["CFI"] or 0) > 0
        cff_pos = (r["CFF"] or 0) > 0
        if cfo_pos and not cfi_pos and not cff_pos:
            r["现金流类型"] = "优质现金牛"
        elif cfo_pos and not cfi_pos and cff_pos:
            r["现金流类型"] = "扩张成长型"
        elif not cfo_pos and not cfi_pos and cff_pos:
            r["现金流类型"] = "烧钱续命型"
        elif not cfo_pos and cfi_pos and not cff_pos:
            r["现金流类型"] = "困境变卖型"
        else:
            types = {
                (True, True, True):   "全面扩张型",
                (True, True, False):  "投资扩张型",
                (True, False, True):  "融资扩张型",
                (False, False, False):"全面收缩型",
                (False, True, True):  "变卖+融资型",
                (False, True, False): "困境变卖型",
            }
            r["现金流类型"] = types.get((cfo_pos, cfi_pos, cff_pos), "其他")

        r["净利润现金比率"], _ = safe_ratio(r["CFO"], r["净利润"], lower=-50.0, upper=50.0)
        r["经营现金流入营收比"] = safe_div(r["经营现金流入小计"], r["营业收入"])
        r["自由现金流"] = (r["CFO"] or 0) - (r["资本开支"] or 0)
        r["经营现金流营收比"] = safe_div(r["CFO"], r["营业收入"])

        # 6.2.4 现金分红率
        r["现金分红率"] = safe_div(r["分红总额"], r["归母净利润"])

        # 6.2.6 纸面富贵
        r["纸面富贵"] = bool((r["净利润"] or 0) > 0 and (r["CFO"] or 0) < 0)

        # 6.2.7 现金利息保障倍数
        # 注意："分配股利付息"含股利+利息，无法拆分 → 系统性高估
        if r["利息费用"] and r["利息费用"] > 0:
            r["现金利息保障倍数"] = ((r["CFO"] or 0) + (r["支付的各项税费"] or 0) + abs(r["分配股利付息"] or 0)) / r["利息费用"]
        else:
            r["现金利息保障倍数"] = None

        # 6.2.8 净利润含金量（优先用CF处置现金，无则用PL近似）
        if r["归母净利润"] and r["归母净利润"] != 0:
            处置现金 = r.get("处置资产收回现金") or r.get("资产处置收益") or 0
            numerator = (r["CFO"] or 0) + (r["投资收益收现"] or 0) - (r["财务费用"] or 0) + 处置现金
            r["净利润含金量"], _ = safe_ratio(numerator, r["归母净利润"], lower=-1000.0, upper=1000.0)
        else:
            r["净利润含金量"] = None

        # 6.3 借款占筹资流入比
        r["借款占筹资比"] = safe_div(r["取得借款收现"], r["筹资现金流入小计"])

    # ---- 豁免标志预计算（用于后续异常判定的场景过滤） ----
    for r in raw:
        # 现金充裕豁免：货资 > 2倍有息负债 → 借钱不是问题
        r["_cash_rich"] = (r.get("货币资金") is not None and 
                           r.get("有息负债总额") is not None and 
                           r["有息负债总额"] > 0 and
                           r["货币资金"] / r["有息负债总额"] > 2)
        # 高速成长期豁免：营收增速 > 50% → 应收/存货暴增可能正常
        r["_high_growth"] = (r.get("营收增速") is not None and r["营收增速"] > 50)
        # 白酒特征：合同负债/营收 > 10% → 预收款模式，合同负债波动是经营常态
        r["_baijiu_mode"] = (r.get("合同负债") is not None and 
                             r.get("营业收入") is not None and 
                             r["营业收入"] > 0 and
                             r["合同负债"] / r["营业收入"] > 0.1)

    # ---- 存货操纵信号预计算（供异常判定和舞弊红线使用） ----
    # 信号说明（SKILL.md 2.8）：
    #   信号1: 存货增速 > 营收增速 30%以上
    #   信号2: 毛利率逆势上涨（营收同比下滑时毛利率反而上升）
    #   信号3: 存货周转天数环比拉长10%以上（周转率下降>10%）
    #   原信号4（存货跌价准备计提比例）已移除：THS不提供该数据
    for i, r in enumerate(raw):
        signals = 0
        # 信号1: 存货增速 vs 营收增速差异 > 30%
        if i >= 1 and r.get("存货营收增速差") is not None and r["存货营收增速差"] > 30:
            signals += 1
        # 信号2: 毛利率逆势上涨（营收增速放缓时毛利率反而上升，且涨幅≥0.5pp）
        if i >= 1 and r.get("毛利率") is not None and raw[i-1].get("毛利率") is not None:
            prev_rev_g = raw[i-1].get("营收增速")
            cur_rev_g = r.get("营收增速")
            margin_up = r["毛利率"] - raw[i-1]["毛利率"]
            if margin_up >= 0.005:
                # P2时prev_rev_g为None(基期)，用绝对判定；P3+用增速对比
                if prev_rev_g is None or (cur_rev_g is not None and cur_rev_g < prev_rev_g):
                    signals += 1
        # 信号3: 存货周转率环比下降 > 10%
        if i >= 1 and r.get("存货周转率") is not None and raw[i-1].get("存货周转率") is not None:
            if r["存货周转率"] < raw[i-1]["存货周转率"] * 0.9:
                signals += 1
        r["_stock_signals"] = signals

    # ---- 第三期：异常判定 ----
    for i, r in enumerate(raw):
        period = r["_period"]
        anoms = []

        # 模块一
        if r["利息保障倍数"] is not None and r["利息保障倍数"] < 1:
            anoms.append(("债务违约高风险", "利息保障倍数<1"))
        if r["存贷双高"]:
            anoms.append(("存贷双高", "三项条件同时满足"))
        if r["资金利息率"] is not None and r["资金利息率"] < 0.0015:  # 0.15%
            anoms.append(("货币资金真实性存疑", f"资金利息率={r['资金利息率']*100:.4f}%"))
        # 1.7 资金利息率趋势标注
        if i >= 2 and r.get("资金利息率") is not None and raw[i-1].get("资金利息率") is not None and raw[i-2].get("资金利息率") is not None:
            if r["资金利息率"] < raw[i-1]["资金利息率"] < raw[i-2]["资金利息率"]:
                anoms.append(("资金利息持续恶化", f"资金利息率连续3期下降：{raw[i-2]['资金利息率']*100:.4f}%→{raw[i-1]['资金利息率']*100:.4f}%→{r['资金利息率']*100:.4f}%"))
        # 控股股东质押（stock_gpzy_pledge_ratio_em API 提供）
        if r.get("控股股东质押比例") is not None:
            if r["控股股东质押比例"] > 50:
                anoms.append(("控股股东高比例质押", f"质押比例={r['控股股东质押比例']:.2f}%"))
            elif r["控股股东质押比例"] > 30:
                anoms.append(("控股股东质押关注", f"质押比例={r['控股股东质押比例']:.2f}%"))
        # 1.2 短期有息负债/资产>20%且货币资金不足覆盖
        if r.get("短期有息负债") and r.get("总资产") and r["总资产"] > 0:
            if r["短期有息负债"] / r["总资产"] > SHORT_DEBT_ASSET_HIGH and r.get("货币资金") is not None and r["货币资金"] < r["短期有息负债"]:
                anoms.append(("短期流动性危机", f"短债占资产{r['短期有息负债']/r['总资产']*100:.1f}%，货币资金不足覆盖"))
        # 1.3 有息负债增速 > 营收增速30%（现金充裕公司豁免）
        if i >= 1 and r.get("有息负债总额") and raw[i-1].get("有息负债总额"):
            债增 = growth(r["有息负债总额"], raw[i-1]["有息负债总额"])
            收增 = r.get("营收增速")
            if 债增 is not None and 收增 is not None and 债增 > 收增 + 30:
                if not r.get("_cash_rich"):
                    anoms.append(("杠杆扩张过快", f"有息负债增速{债增:.1f}%远超营收增速{收增:.1f}%"))
        # 1.4 货资/短债 < 0.5
        if r.get("货资比短债") is not None and isinstance(r["货资比短债"], (int, float)) and r["货资比短债"] < 0.5:
            anoms.append(("资金链脆弱", f"货资/短债仅{r['货资比短债']:.2f}"))
        # 1.8 流动比率（THS摘要提供）
        if r.get("流动比率") is not None:
            if r["流动比率"] < 1:
                anoms.append(("短期偿债危机", f"流动比率仅{r['流动比率']:.2f}"))
        # 1.9 速动比率
        if r.get("速动比率") is not None:
            if r["速动比率"] < 0.5:
                anoms.append(("流动性极差", f"速动比率仅{r['速动比率']:.2f}"))
        # 1.10 流动比率持续恶化
        if i >= 2 and r.get("流动比率") is not None and raw[i-1].get("流动比率") is not None and raw[i-2].get("流动比率") is not None:
            if r["流动比率"] < raw[i-1]["流动比率"] < raw[i-2]["流动比率"]:
                anoms.append(("偿债能力持续恶化", f"流动比率连续3期下降：{raw[i-2]['流动比率']:.2f}→{raw[i-1]['流动比率']:.2f}→{r['流动比率']:.2f}"))

        # 模块二：应收/存货质量排雷
        # 应收占比过高（高速成长豁免）
        if r["应收占营收比"] is not None and r["应收占营收比"] > RECEIVABLE_REVENUE_HIGH:
            if not r.get("_high_growth"):
                anoms.append(("应收占比过高", f"应收占营收{r['应收占营收比']*100:.2f}%"))
        # 应收增速差异常（高速成长豁免+小分母过滤）
        if r["应收占营收比"] is not None and r["应收占营收比"] > RECEIVABLE_MIN_MATERIAL:
            if i >= 1 and r["应收营收增速差"] is not None and abs(r["应收营收增速差"]) > 20:
                if not r.get("_high_growth"):
                    anoms.append(("收入质量异常", f"应收营收增速差{r['应收营收增速差']:.2f}%"))
        # 存货增速差异常（加小分母过滤：存货/营收 < 10% 时跳过）
        if r["存货占营收比"] is not None and r["存货占营收比"] > INVENTORY_REVENUE_HIGH:
            if i >= 1 and r["存货营收增速差"] is not None and abs(r["存货营收增速差"]) > 20:
                anoms.append(("存货滞销风险", f"存货营收增速差{r['存货营收增速差']:.2f}%"))
        # 2.1 应收占比偏高且增速远超营收 → 信用政策异常（高速成长豁免）
        if r["应收占营收比"] is not None and r["应收占营收比"] > 0.2:
            if i >= 1 and r.get("应收营收增速差") is not None and r["应收营收增速差"] > 20:
                if not r.get("_high_growth"):
                    anoms.append(("信用政策异常", f"应收占营收{r['应收占营收比']*100:.1f}%，增速差{r['应收营收增速差']:.1f}%"))
        # 2.3 应收周转率同比下降>30% → 坏账风险激增（高速成长豁免）
        if i >= 1 and r.get("应收周转率") is not None and raw[i-1].get("应收周转率") is not None:
            if raw[i-1]["应收周转率"] > 0:
                decline = (raw[i-1]["应收周转率"] - r["应收周转率"]) / raw[i-1]["应收周转率"]
                if decline > TURNOVER_DECLINE:
                    if not r.get("_high_growth"):
                        anoms.append(("坏账风险激增", f"应收周转率下降{decline*100:.1f}%"))
        # 2.4 存货暴增但营收未同步增长 → 存货积压异常
        if i >= 1 and r.get("存货增速") is not None and r.get("营收增速") is not None:
            if r["存货增速"] > 50 and r["营收增速"] < 10:
                anoms.append(("存货积压异常", f"存货暴增{r['存货增速']:.1f}%但营收仅增{r['营收增速']:.1f}%"))
        # 2.7 合同负债大降但营收增长 → 收入虚增嫌疑（白酒/预收款模式豁免）
        if i >= 1:
            prev_ct = raw[i-1].get("合同负债")
            cur_ct = r.get("合同负债")
            if prev_ct is not None and cur_ct is not None and prev_ct > 0:
                ct_change = (cur_ct - prev_ct) / prev_ct
                if ct_change < -0.3 and r.get("营收增速") is not None and r["营收增速"] > 0:
                    if not r.get("_baijiu_mode"):
                        anoms.append(("合同负债异常", f"合同负债大降{abs(ct_change)*100:.1f}%但营收增长{r['营收增速']:.1f}%"))

        # 模块三：在建工程异常（余额+增速替代转固金额判定）
        if r.get("在建工程占资产比") is not None and r["在建工程占资产比"] > PPE_LONG_TERM:
            if i >= 2:  # 至少需要3期数据判断趋势
                g1 = raw[i-1].get("在建工程增速")
                g2 = r.get("在建工程增速")
                if g1 is not None and g2 is not None and g1 > 0 and g2 > 0:
                    anoms.append(("在建工程持续增长", "可能长期不转固，需核查附注"))
        if r.get("在建工程占资产比") is not None and r["在建工程占资产比"] > PPE_HIGH:
            anoms.append(("在建工程占比过高", f"占资产{r['在建工程占资产比']*100:.2f}%"))
        # 3.4 资产减值损失大额波动 → 资产质量恶化/前期造假暴露
        if i >= 1 and r.get("资产减值损失") is not None and raw[i-1].get("资产减值损失") is not None:
            prev_loss = raw[i-1]["资产减值损失"]
            cur_loss = r["资产减值损失"]
            if prev_loss != 0:
                change = abs(cur_loss - prev_loss) / abs(prev_loss)
                if change > 1.0:
                    anoms.append(("资产减值异常", f"资产减值损失变动{change*100:.1f}%（{prev_loss:.2f}→{cur_loss:.2f}亿）"))

        # 模块四
        if r["资产负债率"] is not None and r["资产负债率"] > DEBT_RATIO_HIGH:
            anoms.append(("资产负债率过高", f"{r['资产负债率']*100:.2f}%"))
        # 4.1 资产负债率同比上升>10个百分点 → 负债扩张过快
        if i >= 1 and r.get("资产负债率") is not None and raw[i-1].get("资产负债率") is not None:
            increase = r["资产负债率"] - raw[i-1]["资产负债率"]
            if increase > DEBT_RATIO_SURGE:
                anoms.append(("负债扩张过快", f"资产负债率上升{increase*100:.1f}个百分点"))
        # 4.2 有息负债率>50%且利息保障倍数<2 → 利润吞噬风险
        if r.get("有息负债率") is not None and r.get("利息保障倍数") is not None:
            if r["有息负债率"] > INTEREST_DEBT_HIGH and r["利息保障倍数"] < 2:
                anoms.append(("利润吞噬风险", f"有息负债率{r['有息负债率']*100:.1f}%且利息保障倍数仅{r['利息保障倍数']:.2f}"))
        # 4.3 经营性负债占比持续下降 → 产业链地位下滑
        if i >= 1 and r.get("经营性负债占比") is not None and raw[i-1].get("经营性负债占比") is not None:
            if r["经营性负债占比"] < raw[i-1]["经营性负债占比"]:
                # 检查是否持续3期下降
                declining = 1
                for j in range(i-1, 0, -1):
                    if raw[j].get("经营性负债占比") is not None and raw[j-1].get("经营性负债占比") is not None:
                        if raw[j]["经营性负债占比"] < raw[j-1]["经营性负债占比"]:
                            declining += 1
                        else:
                            break
                if declining >= 3:
                    anoms.append(("产业链地位下滑", f"经营性负债占比连续{declining}期下降"))
        # 4.5 应付账款周转率同比变动超30% → 成本/利润异常嫌疑
        if i >= 1 and r.get("应付周转率") is not None and raw[i-1].get("应付周转率") is not None:
            if raw[i-1]["应付周转率"] > 0:
                pay_turn_change = abs(r["应付周转率"] - raw[i-1]["应付周转率"]) / raw[i-1]["应付周转率"]
                if pay_turn_change > TURNOVER_DECLINE:
                    anoms.append(("成本利润异常嫌疑", f"应付周转率变动{pay_turn_change*100:.1f}%"))
        # 4.6 少数股东损益异常 → 体外藏亏/虚增归母净利润嫌疑
        if r.get("少数损益占净利比") is not None:
            # 少数损益为负且绝对值>2%（跳过极小额的持续异常）
            if r["少数损益占净利比"] < -0.02:
                anoms.append(("少数股东疑藏亏损", f"少数损益占净利{r['少数损益占净利比']*100:.1f}%（体外藏亏嫌疑）"))
            # 少数损益占比<5%但归母净利增速>30% → 虚增归母嫌疑
            elif 0 <= r["少数损益占净利比"] < 0.05:
                if i >= 1 and r.get("净利增速") is not None and r["净利增速"] > 30:
                    anoms.append(("虚增归母净利润嫌疑", f"少数损益占比仅{r['少数损益占净利比']*100:.1f}%但归母净利增速{r['净利增速']:.1f}%"))
        if r["供应链差额"] is not None and r["供应链差额"] < 0:
            # 检查是否为银票结算：应收票据>80%且应收账款/营收<5% → 仅提示，不升级异常
            应收合计 = (r.get("应收票据") or 0) + (r.get("应收账款") or 0)
            营收 = r.get("营业收入")
            if 应收合计 > 0 and 营收 and 营收 > 0:
                银票特征 = ((r.get("应收票据") or 0) / 应收合计 > SILVER_NOTE_RATIO) and ((r.get("应收账款") or 0) / 营收 < SILVER_NOTE_AR_MAX)
            else:
                银票特征 = False
            if 银票特征:
                anoms.append(("应收票据占比较高", f"应收票据{(r.get('应收票据') or 0):.1f}亿（可能为银票结算）"))
            # 非银票场景：单期差额为负不报警，仅作数据提示
        # 4.7 供应链差额扩大（持续2期为负且绝对值扩大 → 才升级为异常）
        if i >= 2 and r.get("供应链差额") is not None and raw[i-1].get("供应链差额") is not None:
            prev_diff = raw[i-1]["供应链差额"]
            if r["供应链差额"] < 0 and prev_diff < 0 and r["供应链差额"] < prev_diff:
                anoms.append(("供应链资金被占用恶化", f"差额从{prev_diff:.2f}→{r['供应链差额']:.2f}亿"))
        # 4.4 应付增速 vs 营业成本增速 — 连续2期背离才报警
        if i >= 2 and r.get("应付增速") is not None and r.get("营业成本增速") is not None:
            prev_pay = raw[i-1].get("应付增速")
            prev_cost = raw[i-1].get("营业成本增速")
            cur_pay_diff = r["应付增速"] - (r["营业成本增速"] or 0)
            if prev_pay is not None and prev_cost is not None:
                prev_pay_diff = prev_pay - prev_cost
                if abs(cur_pay_diff) > 30 and abs(prev_pay_diff) > 30:
                    anoms.append(("应付成本增速持续背离", f"连续2期应付与成本增速差>{30}%"))

        # 模块五
        if r["销售收现比"] is not None and r["销售收现比"] < 0.9:
            anoms.append(("收入质量异常(收现比)", f"收现比={r['销售收现比']:.2f}"))
        # 5.1.1 营收暴增+收现比低 联合判定（高速成长豁免：营收翻倍时收现比短期偏低属正常）
        if i >= 1 and r.get("营收增速") is not None and r["营收增速"] > 50 and r.get("销售收现比") is not None and r["销售收现比"] < 0.9:
            if not r.get("_high_growth"):
                anoms.append(("收入虚增嫌疑", f"营收暴增{r['营收增速']:.2f}%但收现比仅{r['销售收现比']:.2f}"))
        # 5.1.1 营收持续下滑 → 主业衰退嫌疑
        if i >= 2 and r.get("营收增速") is not None and raw[i-1].get("营收增速") is not None:
            if r["营收增速"] < -30 and raw[i-1]["营收增速"] < -30:
                anoms.append(("主业衰退嫌疑", f"连续2期营收下滑超30%（P{i}：{r['营收增速']:.1f}%，P{i+1}：{raw[i-1]['营收增速']:.1f}%）"))
        # 5.1.2 毛利率单期下跌>5pp（上涨不告警，属于经营改善）
        if i >= 1 and r.get("毛利率") is not None and raw[i-1].get("毛利率") is not None:
            margin_drop = raw[i-1]["毛利率"] - r["毛利率"]
            if margin_drop > MARGIN_SURGE:
                anoms.append(("毛利率异常下跌", f"下跌{margin_drop*100:.2f}个百分点（{raw[i-1]['毛利率']*100:.1f}%→{r['毛利率']*100:.1f}%）"))
        if r["其他收益占利润比"] is not None and r["其他收益占利润比"] > OTHER_INCOME_HIGH:
            anoms.append(("政府补助依赖", f"其他收益占利润{r['其他收益占利润比']*100:.2f}%"))
            # 5.2.3 占比>50%且扣非净利润持续为负 → 主业盈利丧失
            if r.get("扣非净利润") is not None and r["扣非净利润"] < 0:
                anoms.append(("主业盈利丧失", "其他收益占比>50%且扣非净利润为负，主业完全依赖政府补助"))
        # 5.2.4 营业利润/利润总额<50%且营收持续下滑 → 核心盈利能力丧失
        if r.get("营业利润占利润比") is not None and r["营业利润占利润比"] < 0.5:
            if i >= 1 and r.get("营收增速") is not None and r["营收增速"] < 0:
                anoms.append(("核心盈利能力丧失", f"营业利润仅占利润{r['营业利润占利润比']*100:.1f}%且营收下滑{r['营收增速']:.1f}%"))
        if r["扣非占比"] is not None and r["扣非占比"] < 0.6:
            anoms.append(("利润质量差", f"扣非占比{r['扣非占比']*100:.2f}%"))
        # 5.2.9 非经常依赖度两档判定
        if r["非经常依赖度"] is not None:
            if r["非经常依赖度"] > 1.0:
                anoms.append(("非经常依赖度极高", f"依赖度={r['非经常依赖度']*100:.2f}%，主业完全亏损"))
            elif r["非经常依赖度"] > NONRECUR_HIGH:
                anoms.append(("非经常依赖度过高", f"{r['非经常依赖度']*100:.2f}%"))
        if r["扣非归母比"] is not None and r["扣非归母比"] < 0:
            anoms.append(("主业亏损", "扣非净利润<0"))
        if r.get("虚假增长"):
            anoms.append(("净利润虚假增长", "净利增速≥20%且扣非增速下降≥10%"))
        # 5.2.5 净利润增长但扣非下降 → 利润不可持续
        if i >= 1 and r.get("净利增速") is not None and r.get("扣非增速") is not None:
            if r["净利增速"] > 0 and r["扣非增速"] < 0:
                anoms.append(("利润不可持续", f"净利润增{r['净利增速']:.1f}%但扣非降{r['扣非增速']:.1f}%"))
        # 5.2.7 加权ROE>15%但扣非ROE<10% → 盈利质量虚高
        if r.get("加权ROE") is not None and r.get("扣非ROE") is not None:
            if r["加权ROE"] > ROE_HIGH and r["扣非ROE"] < ROE_LOW:
                anoms.append(("盈利质量虚高", f"加权ROE {r['加权ROE']*100:.1f}%但扣非ROE仅{r['扣非ROE']*100:.1f}%"))

        # 存货操纵信号
        if r.get("_stock_signals", 0) >= 3:
            anoms.append(("存货异常高风险", f"触发{r['_stock_signals']}/3个存货操纵信号"))
        elif r.get("_stock_signals", 0) == 2:
            anoms.append(("存货异常关注", "触发2/3个存货操纵信号"))

        # 模块六
        if r["净利润现金比率"] is not None and r["净利润现金比率"] < 0.5 and (r.get("净利润") or 0) > 0:
            anoms.append(("利润现金极差", f"净利现金比率={r['净利润现金比率']:.2f}"))
        # 现金流偏低改为连续2期判定，单期不做异常
        if r["经营现金流入营收比"] is not None and r["经营现金流入营收比"] < 0.8:
            anoms.append(("收入真实性异常", f"经营现流/营收={r['经营现金流入营收比']:.2f}"))
        if r["纸面富贵"]:
            anoms.append(("纸面富贵", "净利润为正但CFO为负"))
        if r["净利润含金量"] is not None and r["净利润含金量"] <= 0.3:
            anoms.append(("利润水分极大", f"含金量={r['净利润含金量']*100:.2f}%"))
        # 6.2.5 经营现金流/营收 < 3%
        if r["经营现金流营收比"] is not None and r["经营现金流营收比"] < 0.03:
            if r.get("营收增速") is not None and r["营收增速"] > 0:
                anoms.append(("收入质量极低", f"经营现金流仅占营收{r['经营现金流营收比']*100:.2f}%"))
        # 6.3 变卖资产补现金流（CFI为正且>50%净利润）
        if r.get("CFI") is not None and r["CFI"] > 0 and r.get("净利润") is not None and r["净利润"] > 0:
            if r["CFI"] > r["净利润"] * 0.5:
                anoms.append(("变卖资产补现金流", f"CFI={r['CFI']:.2f}亿，占净利润{r['CFI']/r['净利润']*100:.1f}%"))
        # 6.3 筹资流入中借款占比>80% 且 利息保障倍数<2（现金充裕豁免）
        if r.get("借款占筹资比") is not None and r["借款占筹资比"] > LOAN_RATIO_HIGH:
            if not r.get("_cash_rich") and (r.get("利息保障倍数") is None or r.get("利息保障倍数", 999) < 2):
                anoms.append(("过度依赖借款", f"借款占筹资流入{r['借款占筹资比']*100:.1f}%，利息保障倍数{r.get('利息保障倍数', 'N/A')}"))
        # 6.4 透支分红：经营现金流为负仍大额分红
        if r.get("每股经营现金流") is not None and r.get("现金分红率") is not None:
            if r["每股经营现金流"] < 0 and r["现金分红率"] > OVERDIVIDEND:
                anoms.append(("透支分红", f"经营现金流为负，分红率仍达{r['现金分红率']*100:.1f}%"))
        # 6.4 分红不可持续：靠老本或借钱分红
        # SKILL.md 6.4.3: 分红率＞100%且经营现金流/净利＜0.3
        if r.get("现金分红率") is not None and r.get("CFO") is not None and r.get("归母净利润") is not None and r["归母净利润"] > 0:
            cfo_to_net = safe_div(r["CFO"], r["归母净利润"])
            if r["现金分红率"] > 1.0 and cfo_to_net is not None and cfo_to_net < 0.3:
                anoms.append(("分红不可持续", f"分红率{r['现金分红率']*100:.1f}%但经营现金流/净利仅{cfo_to_net:.2f}"))

        results["anomalies"][period] = [{"name": n, "detail": d} for n, d in anoms]

    # ---- 跨期异常升级判定 ----
    # 对单期已标记的异常，按SKILL.md规则进行跨期升级
    # 使用 consecutive_run_periods 精确标记实际触发异常的期数，不再广播到所有期
    
    # 1.5 利息保障倍数：持续2期及以上<1 → 极高风险异常（SKILL.md 1.5）
    for start, end, length in consecutive_run_periods(raw, "利息保障倍数", lambda v: v is not None and v < 1):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "债务违约高风险(跨期)", "detail": f"连续{length}期利息保障倍数<1（极高风险异常）"})
    
    # 6.2.1 净利润现金比率：连续2期<0.5 → 极高舞弊
    for start, end, length in consecutive_run_periods(raw, "净利润现金比率", lambda v: v < 0.5):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "利润现金极差(跨期)", "detail": f"连续{length}期净利现金比率<0.5（极高舞弊风险）"})
    
    # 6.2.1b 净利润现金比率：连续2期<0.8（但未触发<0.5）→ 高风险
    for start, end, length in consecutive_run_periods(raw, "净利润现金比率", lambda v: 0.5 <= v < 0.8):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "现金流偏低(跨期)", "detail": f"连续{length}期净利现金比率<0.8（高风险）"})
    
    # 6.2.2 经营现金流入营收比：连续2期<0.8 → 极高舞弊
    for start, end, length in consecutive_run_periods(raw, "经营现金流入营收比", lambda v: v < 0.8):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "收入真实性异常(跨期)", "detail": f"连续{length}期经营现流/营收<0.8（极高舞弊风险）"})
    
    # 6.2.5 经营现金流/营收：连续2期<5% → 高风险
    for start, end, length in consecutive_run_periods(raw, "经营现金流营收比", lambda v: v < 0.05):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "收入质量极低(跨期)", "detail": f"连续{length}期经营现金流/营收<5%（高风险）"})
    
    # 6.2.6 纸面富贵：所有期均触发 → 极高舞弊；连续2期 → 高风险
    paper_wealth_run = max_consecutive_run(raw, "纸面富贵", lambda v: v == True)
    if paper_wealth_run >= 4:
        for start, end, length in consecutive_run_periods(raw, "纸面富贵", lambda v: v == True):
            for i in range(start, end + 1):
                results["anomalies"][raw[i]["_period"]].append({"name": "纸面富贵(全周期)", "detail": f"连续{length}期净利润为正但CFO为负（极高舞弊风险）"})
    else:
        for start, end, length in consecutive_run_periods(raw, "纸面富贵", lambda v: v == True):
            for i in range(start, end + 1):
                results["anomalies"][raw[i]["_period"]].append({"name": "纸面富贵(跨期)", "detail": f"连续{length}期净利润为正但CFO为负（高风险）"})
    
    # 6.2.7 现金利息保障倍数：持续2期<5 → 高风险（优先判定<1的极高风险）
    cash_int_lt1 = count_total(raw, "现金利息保障倍数", lambda v: v < 1)
    if cash_int_lt1 >= 1:
        for r in raw:
            if r.get("现金利息保障倍数") is not None and r["现金利息保障倍数"] < 1:
                results["anomalies"][r["_period"]].append({"name": "利息偿付能力极弱", "detail": "现金利息保障倍数<1（债务违约极高风险）"})
    else:
        for start, end, length in consecutive_run_periods(raw, "现金利息保障倍数", lambda v: v < 5):
            for i in range(start, end + 1):
                results["anomalies"][raw[i]["_period"]].append({"name": "利息偿付能力不足(跨期)", "detail": f"连续{length}期现金利息保障倍数<5（高风险）"})
    
    # 6.2.8 净利润含金量：持续2期≤30% → 极高风险
    for start, end, length in consecutive_run_periods(raw, "净利润含金量", lambda v: v <= 0.3):
        for i in range(start, end + 1):
            results["anomalies"][raw[i]["_period"]].append({"name": "利润水分极大(跨期)", "detail": f"连续{length}期净利润含金量≤30%（极高风险）"})
    
    # 6.2.3 自由现金流全周期为负 → 高风险
    for start, end, length in consecutive_run_periods(raw, "自由现金流", lambda v: v < 0):
        if length >= 4:
            for i in range(start, end + 1):
                results["anomalies"][raw[i]["_period"]].append({"name": "自由现金流持续为负", "detail": "4期全周期自由现金流为负（高风险）"})
    
    # 6.2.4 现金分红率异常：货币资金充足+净利为正，但连续2期0分红
    # 逐期检测，只在满足条件的期标记
    dividend_low_periods = []
    for r in raw:
        has_cash = r.get("货币资金") is not None and r["货币资金"] > 0
        has_profit = r.get("归母净利润") is not None and r["归母净利润"] > 0
        payout = r.get("现金分红率")
        if has_cash and has_profit and (payout is None or payout < 0.01):
            dividend_low_periods.append(r["_period"])
    if len(dividend_low_periods) >= 2:
        for period in dividend_low_periods:
            results["anomalies"][period].append({"name": "货币资金真实性存疑", "detail": f"货币资金充足+净利润为正，但连续{len(dividend_low_periods)}期几乎0分红"})

    # ---- 舞弊红线检测 ----
    fraud_lines = []
    data_warnings = []  # 提前定义，供红线检测中使用
    # 红线4: 归母与合并净利偏离（加少数股东损益前置筛选，避免大集团正常结构被误判）
    #  条件A: 归母占合并比超出[90%,110%]
    #  条件B: 少数股东损益为负（绝对值>2%净利，即体外藏亏）
    #  A+B同时满足 → 舞弊红线；仅A → 普通异常
    deviations = []
    minority_hiding = False
    for r in raw:
        if r["归母占合并比"] is not None:
            deviations.append(r["归母占合并比"])
        if r.get("少数损益占净利比") is not None and r["少数损益占净利比"] < -0.02:
            minority_hiding = True
    if deviations and any(d < 0.9 or d > 1.1 for d in deviations):
        if minority_hiding:
            fraud_lines.append("归母与合并净利润偏离度超出90%-110%区间（且少数股东体外藏亏）")
        else:
            # 仅偏离但少数股东正常 → 降级为普通异常，标记为"关注"而非舞弊红线
            for period in periods:
                results["anomalies"][period].append({"name": "归母合并偏离(关注)", "detail": "归母占合并比偏离90%-110%，但少数股东损益正常，非舞弊信号，注意核实合并范围"})

    # 红线5: 存货操纵（3信号=全部触发，或≥2信号持续2期+，才判定为舞弊红线）
    for r in raw:
        if r.get("_stock_signals", 0) >= 3:
            fraud_lines.append(f"{r['_period']} 触发存货操纵利润舞弊全部3信号")
    high_sig_count = sum(1 for r in raw if r.get("_stock_signals", 0) >= 2)
    if high_sig_count >= 2:
        fraud_lines.append(f"存货操纵舞弊风险(≥2/3信号，持续{high_sig_count}期)")

    # 红线6: 扣非净利连续3期及以上为负
    deduct_count = sum(1 for r in raw if r["扣非净利润"] is not None and r["扣非净利润"] < 0)
    if deduct_count >= 3:
        fraud_lines.append("扣非净利润连续3期及以上为负")

    # 红线7: 货币资金收益率<0.15%且持续2期
    low_rate = sum(1 for r in raw if r["资金利息率"] is not None and r["资金利息率"] < 0.0015)
    if low_rate >= 2:
        fraud_lines.append("货币资金收益率<0.15%且持续2期及以上")

    results["fraud_lines"] = fraud_lines
    results["raw"] = raw

    # ---- 数据质量异常检测 ----
    # 检查核心字段是否缺失（仅检查通用必备字段，跳过可选负债类科目）
    critical_fields = [
        ("货币资金", "货币资金"),
        ("营业收入", "营业收入"),
        ("营业成本", "营业成本"),
        ("净利润", "净利润"),
        ("归母净利润", "归母净利润"),
        ("总资产", "资产总计"),
        ("总负债", "负债总计"),
    ]
    
    for r in raw:
        period = r["_period"]
        for field, name in critical_fields:
            if r.get(field) is None:
                data_warnings.append(f"⚠️ {period} {name}数据缺失，可能影响指标计算准确性")
    
    # 检查所有相邻期之间的数据突变（仅保留恶化方向，向好变化不告警）
    for i in range(1, len(raw)):
        cur = raw[i]
        prev = raw[i-1]
        for field, name in [("营业收入", "营收"), ("净利润", "净利润"), ("CFO", "CFO")]:
            if cur[field] is not None and prev[field] is not None:
                chg = growth(cur[field], prev[field])
                if chg is not None and chg < -CHG_WARNING:  # 仅骤降告警，骤增不告警（向好）
                    data_warnings.append(f"⚠️ {name} P{i+1}较P{i}骤降{abs(chg):.1f}%（{prev[field]:.2f}→{cur[field]:.2f}亿），需核实数据完整性")
    # ---- 现金流钩稽验证（仅内部计算用，不对外告警） ----
    # 间接法 vs 直接法 CFO 差异受多因素影响（非现金项目口径、合并范围变动等），
    # 非数据质量问题，不再产生用户可见告警
    # 年报未发布检测
    if len(raw) > 0:
        latest_date = raw[-1]["_period"]
        if latest_date.endswith("-12-31"):
            year = int(latest_date[:4])
            now = datetime.now()
            if now.year > year and now.month <= 4:
                data_warnings.append(f"⚠️ {latest_date} 年报可能在{year+1}年4月后才发布，当前数据可能不完整")

    # ---- 财报重述检测（来自 download 阶段） ----
    if data.get("has_restated"):
        restated_list = ", ".join(data.get("restated_periods", []))
        data_warnings.append(f"⚠️ 财报重述警告：以下报告期存在多版本数据（{restated_list}），当前使用最新版，历史更正可能影响趋势分析")

    results["data_quality"] = {"warnings": data_warnings}

    # ---- 总异常统计 ----
    summary = {}
    total_anom_periods = sum(1 for a in results["anomalies"].values() if len(a) >= 3)
    total_anom_5per = sum(1 for a in results["anomalies"].values() if len(a) >= 5)

    # 持续2期≥3异常判定
    consecutive_anom = 0
    max_consecutive = 0
    for a in results["anomalies"].values():
        if len(a) >= 3:
            consecutive_anom += 1
            max_consecutive = max(max_consecutive, consecutive_anom)
        else:
            consecutive_anom = 0

    if fraud_lines:
        summary["风险等级"] = "【坚决回避】— 触发舞弊红线"
    elif max_consecutive >= 3 and total_anom_5per >= 3:
        summary["风险等级"] = "【坚决回避】— 持续3期触发≥5项异常"
    elif max_consecutive >= 2 and total_anom_5per >= 2:
        summary["风险等级"] = "【规避】— 持续2期触发≥5项异常"
    elif (total_anom_periods >= 1 and total_anom_5per >= 1) or max_consecutive >= 2:
        summary["风险等级"] = "【关注】— 存在需关注的异常项"
    elif total_anom_periods >= 1:
        summary["风险等级"] = "【谨慎关注】— 存在少量异常项"
    else:
        summary["风险等级"] = "【正常】— 未触发排雷阈值"

    summary["舞弊红线"] = f"触发{len(fraud_lines)}条" if fraud_lines else "未触发"
    summary["单期≥3异常"] = f"{total_anom_periods}/{len(periods)}期"
    results["summary"] = summary

    return results


def main():
    if len(sys.argv) < 2:
        print("用法: python compute_indicators.py <code>_合并财报数据.json")
        sys.exit(1)

    input_file = sys.argv[1]
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"计算 {data.get('company_name', data['stock_code'])} 排雷指标...")
    results = compute_all(data)

    output_file = input_file.replace("_合并财报数据.json", "_排雷指标.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    print(f"✓ 指标计算完成 → {output_file}")
    print(f"  风险等级: {results['summary']['风险等级']}")
    print(f"  舞弊红线: {results['summary']['舞弊红线']}")
    print(f"  异常统计: {results['summary']['单期≥3异常']}")

    if results["data_quality"]["warnings"]:
        print(f"  数据警告: {len(results['data_quality']['warnings'])}条")
        for w in results["data_quality"]["warnings"]:
            print(f"    {w}")
    if results["fraud_lines"]:
        print(f"  ⚠️ 舞弊红线触发!")
        for fl in results["fraud_lines"]:
            print(f"    • {fl}")


if __name__ == "__main__":
    main()
