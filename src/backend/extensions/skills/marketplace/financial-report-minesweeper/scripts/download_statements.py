#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — 合并三大报表数据下载脚本
数据源：同花顺（通过 akshare），免费、免API Key、纯Python
口径：合并报表（含少数股东权益等合并专属科目）

用法：
    python download_statements.py 601006                           # 默认取最近4期年报
    python download_statements.py 601006 --mode quarterly           # 最近4期季报
    python download_statements.py 601006 --mode annual --periods 3  # 最近3期年报
    python download_statements.py 601006 --start-year 2020 --end-year 2023  # 指定年份区间
"""

import akshare as ak
import pandas as pd
import json
import sys
import math

# 强制UTF-8输出（Windows GBK兼容）
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# 工具函数
# ============================================================

def parse_value(val):
    """将 THS 返回的带单位字符串转为浮点数（亿元）"""
    if val is None or pd.isna(val) or val is False or val == "False" or val == "":
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        # 防御：inf 和极值会被 JSON 拒绝
        if math.isinf(v) or math.isnan(v):
            return None
        return v
    s = str(val).replace(",", "").strip()
    # 百分比（净资产收益率等）
    if "%" in s:
        s = s.replace("%", "")
        try:
            return float(s) / 100.0
        except ValueError:
            return None
    # 处理复合单位：万亿 > 亿 > 万
    multiplier = 1.0
    if "万亿" in s:
        s = s.replace("万亿", "")
        multiplier = 10000.0  # 1万亿 = 10000亿
    elif "亿" in s:
        s = s.replace("亿", "")
        multiplier = 1.0  # 已经是亿元
    elif "万" in s:
        s = s.replace("万", "")
        multiplier = 1.0 / 10000  # 万元→亿元
    # 纯数字字符串
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def select_periods(df, mode="annual", periods=4, start_year=None, end_year=None):
    """
    从 THS 数据中选择目标报告期
    - mode='annual': 选年报（12-31）
    - mode='quarterly': 选最近 N 期全部季报
    - start_year/end_year: 指定年份区间（含边界），如2020-2023
    """
    if mode == "annual":
        # 筛选报告期为 12-31 的行
        mask = df["报告期"].astype(str).str.endswith("-12-31")
        candidates = df[mask].copy()
    else:
        candidates = df.copy()

    # 按日期降序排列
    candidates = candidates.sort_values("报告期", ascending=False)

    # 指定年份区间时：只保留区间内的年报
    if start_year is not None or end_year is not None:
        year_mask = pd.Series(True, index=candidates.index)
        if start_year is not None:
            year_mask &= candidates["报告期"].astype(str) >= f"{start_year}-01-01"
        if end_year is not None:
            year_mask &= candidates["报告期"].astype(str) <= f"{end_year}-12-31"
        candidates = candidates[year_mask]
        if len(candidates) == 0:
            print(f"⚠️ 警告: 未找到 {start_year}-{end_year} 区间内的年报数据")

    # 无区间限制时取最近 N 期；有区间限制时取区间内全部
    if start_year is None and end_year is None:
        result = candidates.head(periods).sort_values("报告期", ascending=True)
    else:
        result = candidates.sort_values("报告期", ascending=True)
    return result


# ============================================================
# 字段映射：THS API 字段名 → 模板字段名
# ============================================================

BS_MAPPING = {
    # 流动资产
    "货币资金": "货币资金",
    "交易性金融资产": "交易性金融资产",
    "其中：应收票据": "应收票据",
    "应收账款": "应收账款",
    "预付款项": "预付款项",
    "其他应收款": "其他应收款",
    "其中：应收利息": "其中：应收利息",
    "存货": "存货",
    "其他流动资产": "其他流动资产",
    "流动资产合计": "流动资产合计",
    # 非流动资产
    "长期股权投资": "长期股权投资",
    "其他权益工具投资": "其他权益工具投资",
    "投资性房地产": "投资性房地产",
    "其中：固定资产": "固定资产",
    "其中：在建工程": "在建工程",
    "无形资产": "无形资产",
    "商誉": "商誉",
    "长期待摊费用": "长期待摊费用",
    "递延所得税资产": "递延所得税资产",
    "其他非流动资产": "其他非流动资产",
    "非流动资产合计": "非流动资产合计",
    "资产合计": "资产总计",
    # 流动负债
    "短期借款": "短期借款",
    "其中：应付票据": "应付票据",
    "应付账款": "应付账款",
    "预收款项": "预收款项",
    "合同负债": "合同负债",
    "应付职工薪酬": "应付职工薪酬",
    "应交税费": "应交税费",
    "其他应付款": "其他应付款",
    "其中：应付利息": "其中：应付利息",
    "应付股利": "其中：应付股利",
    "一年内到期的非流动负债": "一年内到期的非流动负债",
    "其他流动负债": "其他流动负债",
    "流动负债合计": "流动负债合计",
    # 非流动负债
    "长期借款": "长期借款",
    "应付债券": "应付债券",
    "租赁负债": "租赁负债",
    "其中：长期应付款": "长期应付款",
    "预计负债": "预计负债",
    "递延所得税负债": "递延所得税负债",
    "递延收益-非流动负债": "递延收益",
    "其他非流动负债": "其他非流动负债",
    "非流动负债合计": "非流动负债合计",
    "负债合计": "负债总计",
    # 所有者权益
    "实收资本（或股本）": "实收资本（或股本）",
    "资本公积": "资本公积",
    "减：库存股": "减：库存股",
    "其他综合收益": "其他综合收益",
    "盈余公积": "盈余公积",
    "未分配利润": "未分配利润",
    "归属于母公司所有者权益合计": "归属于母公司所有者权益合计",
    "少数股东权益": "少数股东权益",
    "所有者权益（或股东权益）合计": "所有者权益合计",
}

PL_MAPPING = {
    "一、营业总收入": "一、营业总收入",
    "其中：营业收入": "营业收入",
    "其中：营业成本": "营业成本",
    "营业税金及附加": "税金及附加",
    "销售费用": "销售费用",
    "管理费用": "管理费用",
    "研发费用": "研发费用",
    "财务费用": "财务费用",
    "其中：利息费用": "其中：利息费用",
    "利息收入": "其中：利息收入",
    "其他收益": "加：其他收益",
    "投资收益": "投资收益",
    "其中：联营企业和合营企业的投资收益": "其中：对联营企业和合营企业的投资收益",
    "信用减值损失": "信用减值损失",
    "资产减值损失": "资产减值损失",
    "资产处置收益": "资产处置收益",
    "三、营业利润": "二、营业利润",
    "加：营业外收入": "加：营业外收入",
    "减：营业外支出": "减：营业外支出",
    "四、利润总额": "三、利润总额",
    "减：所得税费用": "减：所得税费用",
    "五、净利润": "四、净利润",
    "（一）持续经营净利润": "（一）持续经营净利润",
    "归属于母公司所有者的净利润": "归属于母公司所有者的净利润",
    "少数股东损益": "少数股东损益",
    "扣除非经常性损益后的净利润": "扣除非经常性损益后的净利润",
    "七、其他综合收益": "五、其他综合收益的税后净额",
    "八、综合收益总额": "六、综合收益总额",
    "归属于母公司股东的综合收益总额": "归属于母公司所有者的综合收益总额",
    "归属于少数股东的综合收益总额": "归属于少数股东的综合收益总额",
    "（一）基本每股收益": "（一）基本每股收益",
    "（二）稀释每股收益": "（二）稀释每股收益",
}

CF_MAPPING = {
    # 经营活动
    "销售商品、提供劳务收到的现金": "销售商品、提供劳务收到的现金",
    "收到的税费与返还": "收到的税费返还",
    "收到其他与经营活动有关的现金": "收到其他与经营活动有关的现金",
    "经营活动现金流入小计": "经营活动现金流入小计",
    "购买商品、接受劳务支付的现金": "购买商品、接受劳务支付的现金",
    "支付给职工以及为职工支付的现金": "支付给职工以及为职工支付的现金",
    "支付的各项税费": "支付的各项税费",
    "支付其他与经营活动有关的现金": "支付其他与经营活动有关的现金",
    "经营活动现金流出小计": "经营活动现金流出小计",
    "经营活动产生的现金流量净额": "经营活动产生的现金流量净额",
    # 投资活动
    "收回投资收到的现金": "收回投资收到的现金",
    "取得投资收益收到的现金": "取得投资收益收到的现金",
    "处置固定资产、无形资产和其他长期资产收回的现金净额": "处置固定资产、无形资产和其他长期资产收回的现金净额",
    "处置子公司及其他营业单位收到的现金净额": "处置子公司及其他营业单位收到的现金净额",
    "收到其他与投资活动有关的现金": "收到其他与投资活动有关的现金",
    "投资活动现金流入小计": "投资活动现金流入小计",
    "购建固定资产、无形资产和其他长期资产支付的现金": "购建固定资产、无形资产和其他长期资产支付的现金",
    "投资支付的现金": "投资支付的现金",
    "取得子公司及其他营业单位支付的现金净额": "取得子公司及其他营业单位支付的现金净额",
    "支付其他与投资活动有关的现金": "支付其他与投资活动有关的现金",
    "投资活动现金流出小计": "投资活动现金流出小计",
    "投资活动产生的现金流量净额": "投资活动产生的现金流量净额",
    # 筹资活动
    "吸收投资收到的现金": "吸收投资收到的现金",
    "其中：子公司吸收少数股东投资收到的现金": "其中：子公司吸收少数股东投资收到的现金",
    "取得借款收到的现金": "取得借款收到的现金",
    "收到其他与筹资活动有关的现金": "收到其他与筹资活动有关的现金",
    "筹资活动现金流入小计": "筹资活动现金流入小计",
    "偿还债务支付的现金": "偿还债务支付的现金",
    "分配股利、利润或偿付利息支付的现金": "分配股利、利润或偿付利息支付的现金",
    "其中：子公司支付给少数股东的股利、利润": "其中：子公司支付给少数股东的股利、利润",
    "支付其他与筹资活动有关的现金": "支付其他与筹资活动有关的现金",
    "筹资活动现金流出小计": "筹资活动现金流出小计",
    "筹资活动产生的现金流量净额": "筹资活动产生的现金流量净额",
    # 汇率与汇总
    "四、汇率变动对现金及现金等价物的影响": "四、汇率变动对现金及现金等价物的影响",
    "五、现金及现金等价物净增加额": "五、现金及现金等价物净增加额",
    "加：期初现金及现金等价物余额": "加：期初现金及现金等价物余额",
    "六、期末现金及现金等价物余额": "六、期末现金及现金等价物余额",
    # 间接法（钩稽验证用）
    "净利润": "间接法-净利润",
    "加：资产减值准备": "间接法-资产减值准备",
    "固定资产折旧、油气资产折耗、生产性生物资产折旧": "间接法-固定资产折旧",
    "无形资产摊销": "间接法-无形资产摊销",
    "长期待摊费用摊销": "间接法-长期待摊费用摊销",
    "财务费用": "间接法-财务费用",
    "投资损失": "间接法-投资损失",
    "递延所得税资产减少": "间接法-递延所得税资产减少",
    "存货的减少": "间接法-存货的减少",
    "经营性应收项目的减少": "间接法-经营性应收项目的减少",
    "经营性应付项目的增加": "间接法-经营性应付项目的增加",
}


def extract_mapped_data(df, mapping, periods_data):
    """
    从 THS DataFrame 中按 mapping 提取数据
    返回 dict: {template_field: {period_date: value}}
    
    THS 有两类列：`*` 前缀的核心指标列和普通详情列。
    本函数优先匹配普通列，若不存在则尝试 `*` 前缀列作为回退。
    """
    result = {}
    for ths_field, template_field in mapping.items():
        result[template_field] = {}
        # 确定实际使用的列名（优先普通列，回退 * 核心列）
        actual_field = None
        if ths_field in df.columns:
            actual_field = ths_field
        elif f"*{ths_field}" in df.columns:
            actual_field = f"*{ths_field}"
        
        for _, row in periods_data.iterrows():
            period = str(row["报告期"])
            if actual_field:
                val = parse_value(row[actual_field])
            else:
                val = None
            result[template_field][period] = val
    return result


def main():
    # 解析参数
    if len(sys.argv) < 2:
        print("用法: python download_statements.py <股票代码> [--mode annual|quarterly]")
        print("示例: python download_statements.py 601006")
        print("      python download_statements.py 601006 --mode quarterly")
        sys.exit(1)

    stock_code = sys.argv[1]
    mode = "annual"
    start_year = None
    end_year = None

    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            mode = sys.argv[idx + 1]

    if "--start-year" in sys.argv:
        idx = sys.argv.index("--start-year")
        if idx + 1 < len(sys.argv):
            start_year = int(sys.argv[idx + 1])

    if "--end-year" in sys.argv:
        idx = sys.argv.index("--end-year")
        if idx + 1 < len(sys.argv):
            end_year = int(sys.argv[idx + 1])

    year_desc = f"{start_year}-{end_year}" if start_year or end_year else "最近4期"

    print(f"\n{'='*60}")
    print(f"财报排雷 v1.3.0 — 合并报表数据下载")
    print(f"股票代码: {stock_code}")
    print(f"模式: {'年报' if mode == 'annual' else '季报'} · {year_desc}")
    print(f"数据源: 同花顺 (akshare)")
    print(f"口径: 合并报表")
    print(f"{'='*60}\n")

    # ========================================
    # Step 1: 下载三大报表
    # ========================================
    print("[1/7] 下载合并资产负债表...", end=" ", flush=True)
    df_bs = ak.stock_financial_debt_ths(symbol=stock_code, indicator="按报告期")
    print(f"✓ ({len(df_bs)}期)")

    print("[2/7] 下载合并利润表...", end=" ", flush=True)
    df_pl = ak.stock_financial_benefit_ths(symbol=stock_code, indicator="按报告期")
    print(f"✓ ({len(df_pl)}期)")

    print("[3/7] 下载合并现金流量表...", end=" ", flush=True)
    df_cf = ak.stock_financial_cash_ths(symbol=stock_code, indicator="按报告期")
    print(f"✓ ({len(df_cf)}期)")

    print("[4/7] 下载财务摘要(ROE等)...", end=" ", flush=True)
    df_abstract = ak.stock_financial_abstract_ths(symbol=stock_code, indicator="按报告期")
    print(f"✓ ({len(df_abstract)}期)")

    print("[5/7] 下载分红数据...", end=" ", flush=True)
    df_dividend = ak.stock_fhps_detail_ths(symbol=stock_code)
    print(f"✓ ({len(df_dividend)}条)")

    print("[6/7] 下载质押数据...", end=" ", flush=True)
    df_pledge = ak.stock_gpzy_pledge_ratio_em()
    质押比例 = None
    公司名称 = stock_code  # fallback
    pledge_stock = df_pledge[df_pledge['股票代码'] == stock_code]
    if len(pledge_stock) > 0:
        质押比例 = parse_value(pledge_stock.iloc[-1].get('质押比例'))
        公司名称 = str(pledge_stock.iloc[-1].get('股票简称', stock_code))
    # 备用：从交易所股票列表获取名称
    if 公司名称 == stock_code:
        try:
            if stock_code.startswith(('6','5','9')):
                df_names = ak.stock_info_sh_name_code()
                公司名称 = str(df_names[df_names['证券代码']==stock_code].iloc[0]['证券简称'])
            elif stock_code.startswith('8'):
                # 北交所 8 开头代码尝试专用API
                try:
                    df_names = ak.stock_info_bj_name_code()
                    公司名称 = str(df_names[df_names['证券代码']==stock_code].iloc[0]['证券简称'])
                except (ImportError, AttributeError, IndexError):
                    df_names = ak.stock_info_sz_name_code()
                    公司名称 = str(df_names[df_names['A股代码']==stock_code].iloc[0]['A股简称'])
            else:
                df_names = ak.stock_info_sz_name_code()
                公司名称 = str(df_names[df_names['A股代码']==stock_code].iloc[0]['A股简称'])
            公司名称 = 公司名称.replace(' ', '').replace('Ａ','A').replace('Ｂ','B')  # 清理全角空格
        except (IndexError, KeyError, AttributeError) as e:
            print(f"\n  ⚠ 股票名称获取失败({e.__class__.__name__})，使用代码 {stock_code}", flush=True)
            公司名称 = stock_code
    print(f"✓ (质押比例={'{:.2f}%'.format(质押比例) if 质押比例 else 'N/A'}, {公司名称})")

    print("[7/7] 提取并验证数据...", end=" ", flush=True)
    bs_periods = select_periods(df_bs, mode=mode, periods=4, start_year=start_year, end_year=end_year)
    pl_periods = select_periods(df_pl, mode=mode, periods=4, start_year=start_year, end_year=end_year)
    cf_periods = select_periods(df_cf, mode=mode, periods=4, start_year=start_year, end_year=end_year)

    # 三大报表期数一致性检查
    bs_pcnt = len(bs_periods)
    pl_pcnt = len(pl_periods)
    cf_pcnt = len(cf_periods)
    if not (bs_pcnt == pl_pcnt == cf_pcnt):
        print(f"\n  ⚠ 三大报表期数不一致！BS={bs_pcnt}期, PL={pl_pcnt}期, CF={cf_pcnt}期")
        print(f"    将以 BS 期数为准，缺失期数据将标注为【无公开数据】")

    # 统一使用资产负债表的时间范围
    period_labels = [str(d) for d in bs_periods["报告期"].tolist()]
    period_names = ["第一期", "第二期", "第三期", "第四期"]

    print(f"\n选定报告期:")
    for i, (name, label) in enumerate(zip(period_names, period_labels)):
        print(f"  {name}: {label}")

    # ========================================
    # Step 3: 提取并映射数据
    # ========================================
    bs_data = extract_mapped_data(df_bs, BS_MAPPING, bs_periods)
    pl_data = extract_mapped_data(df_pl, PL_MAPPING, pl_periods)
    cf_data = extract_mapped_data(df_cf, CF_MAPPING, cf_periods)

    # ========================================
    # Step 4: 提取财务摘要数据（ROE等）
    # ========================================
    abstract_data = {}
    for _, row in df_abstract.iterrows():
        period = str(row["报告期"])
        if period in period_labels:
            abstract_data[period] = {
                "净资产收益率": parse_value(row.get("净资产收益率")),
                "净资产收益率-摊薄": parse_value(row.get("净资产收益率-摊薄")),
                "销售净利率": parse_value(row.get("销售净利率")),
                # 偿债能力
                "流动比率": parse_value(row.get("流动比率")),
                "速动比率": parse_value(row.get("速动比率")),
                "保守速动比率": parse_value(row.get("保守速动比率")),
                # 每股指标
                "每股净资产": parse_value(row.get("每股净资产")),
                "每股经营现金流": parse_value(row.get("每股经营现金流")),
                "每股未分配利润": parse_value(row.get("每股未分配利润")),
                "每股资本公积金": parse_value(row.get("每股资本公积金")),
                # 周转天数（THS已算，用于交叉验证）
                "应收账款周转天数": parse_value(row.get("应收账款周转天数")),
                "存货周转天数": parse_value(row.get("存货周转天数")),
                "总资产周转率": parse_value(row.get("总资产周转率")),
                # 利润趋势
                "扣非净利润同比增长率": parse_value(row.get("扣非净利润同比增长率")),
            }

    # ========================================
    # Step 5: 提取分红数据
    # ========================================
    dividend_list = []
    for _, row in df_dividend.iterrows():
        rep = str(row["报告期"])
        分红总额 = parse_value(row.get("分红总额"))
        股利支付率 = parse_value(row.get("股利支付率"))
        # 分红报告期匹配："2024年报"→"2024-12-31", 处理变体如"2024年报(修订)"
        year = rep[:4]
        rep_clean = rep.replace("（", "(").replace("）", ")")
        if "年报" in rep_clean:
            mapped_period = f"{year}-12-31"
        elif "三季报" in rep_clean:
            mapped_period = f"{year}-09-30"
        elif "中报" in rep_clean or "半年报" in rep_clean:
            mapped_period = f"{year}-06-30"
        elif "一季报" in rep_clean:
            mapped_period = f"{year}-03-31"
        else:
            continue
        dividend_list.append({
            "报告期": rep,
            "期间": mapped_period,
            "分红总额": 分红总额,
            "股利支付率": 股利支付率,
        })
    print(f"✓ 完成")

    # ========================================
    # Step 4: 验证是否为合并报表 & 行业检测
    # ========================================
    is_consolidated = True
    consolidated_checks = []
    
    # 行业检测：银行/券商等金融企业
    is_financial = False
    cf_columns = list(df_cf.columns)
    bank_markers = ["客户存款", "同业存放", "向中央银行借款", "拆入资金", "收取利息、手续费及佣金的现金"]
    if any(m in " ".join(cf_columns) for m in bank_markers):
        is_financial = True
    
    # 检查资产负债表
    if "少数股东权益" in bs_data:
        vals = [v for v in bs_data["少数股东权益"].values() if v is not None]
        if vals:
            consolidated_checks.append(f"✓ 含少数股东权益（{'/'.join([str(v) for v in vals[:2]])}...）")
        else:
            # 少数股东权益为0（全资子公司）也是合并报表
            consolidated_checks.append("○ 少数股东权益为0（全资子公司，仍为合并报表）")
    else:
        consolidated_checks.append("✗ 无少数股东权益科目")

    # 检查利润表（归母净利润是合并报表的更强证据）
    if "归属于母公司所有者的净利润" in pl_data:
        vals = [v for v in pl_data["归属于母公司所有者的净利润"].values() if v is not None]
        if vals:
            consolidated_checks.append(f"✓ 含归母净利润（{'/'.join([str(v) for v in vals[:2]])}...）")
        else:
            is_consolidated = False
            consolidated_checks.append("✗ 归母净利润无有效值")
    else:
        is_consolidated = False
        consolidated_checks.append("✗ 无归母净利润科目（非合并报表）")
    if "少数股东损益" in pl_data:
        consolidated_checks.append("✓ 含少数股东损益")

    # 检查现金流量表（非金融企业才检查子公司现金流科目）
    if not is_financial:
        if "其中：子公司吸收少数股东投资收到的现金" in cf_data:
            consolidated_checks.append("✓ 含子公司少数股东现金流科目")
    
    print(f"\n合并报表验证:")
    for check in consolidated_checks:
        print(f"  {check}")
    print(f"  结论: {'✓ 合并报表' if is_consolidated else '✗ 非合并报表！'}")
    if not is_consolidated:
        print("  ⚠️ 警告：数据可能不是合并报表口径，请核实！")
    if is_financial:
        print("\n  ⚠️⚠️⚠️ 重要警告 ⚠️⚠️⚠️")
        print("  检测到金融企业（银行/保险/证券），排雷框架不适用于此类企业！")
        print("  现金流量表结构、核心财务指标均不兼容，分析结果不可采信。")
        print("  建议：使用专门的金融企业分析工具。")

    # ========================================
    # Step 4.5: 财报重述检测
    # ========================================
    has_restated = False
    restated_periods = set()
    for name, df in [("资产负债表", df_bs), ("利润表", df_pl), ("现金流量表", df_cf)]:
        periods_col = df["报告期"].astype(str)
        dup = periods_col[periods_col.duplicated()]
        if len(dup) > 0:
            has_restated = True
            for p in dup.unique():
                restated_periods.add(p)
    if has_restated:
        restated_list = sorted(restated_periods)
        print(f"\n⚠️ 财报重述警告：以下报告期存在多版本数据（THS返回了更正版）：")
        for p in restated_list:
            print(f"  • {p}")
        print("  当前使用最新版本，历史更正可能影响趋势分析的准确性")

    # ========================================
    # Step 5: 输出结果
    # ========================================
    output = {
        "stock_code": stock_code,
        "company_name": 公司名称,
        "mode": mode,
        "is_financial": is_financial,
        "is_consolidated": is_consolidated,
        "has_restated": has_restated,
        "restated_periods": sorted(restated_periods) if has_restated else [],
        "periods": period_labels,
        "period_names": period_names,
        "balance_sheet": bs_data,
        "profit_statement": pl_data,
        "cash_flow_statement": cf_data,
        "abstract": abstract_data,
        "dividend": dividend_list,
        "pledge_ratio": 质押比例,
    }

    # 保存 JSON 供后续分析使用
    output_file = f"{stock_code}_合并财报数据.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✓ 数据已保存至: {output_file}")
    print(f"{'='*60}")

    # 同时输出简要摘要
    print(f"\n数据摘要:")
    print(f"  资产负债表: {len(bs_data)} 个科目")
    print(f"  利润表: {len(pl_data)} 个科目")
    print(f"  现金流量表: {len(cf_data)} 个科目")
    print(f"  财务摘要: {len(abstract_data)} 期（ROE等）")
    print(f"  分红数据: {len(dividend_list)} 条")

    return output


if __name__ == "__main__":
    main()
