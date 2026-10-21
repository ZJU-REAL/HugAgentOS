#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — TDX 数据提取与裁剪脚本
读取 TDX MCP 返回的三大报表 JSON，提取指定年报，输出标准格式 JSON

用法：
    python extract_tdx_data.py <stock_code> <company_name> \
        --bs <bs_tdx_file.json> \
        --is <is_tdx_file.json> \
        --cf <cf_tdx_file.json> \
        [--start-year 2020] [--end-year 2023]
"""

import json
import sys
import os
import argparse
import re

YI = 100000000  # 1亿

# ============================================================
# 字段映射：TDX中文名 → 标准 JSON key
# ============================================================

BS_FIELD_MAP = {
    "货币资金": "货币资金",
    "交易性金融资产": "交易性金融资产",
    "应收票据": "应收票据",
    "应收账款": "应收账款",
    "预付款项": "预付款项",
    "预付账款": "预付款项",
    "其他应收款": "其他应收款",
    "其中：应收利息": "其中：应收利息",
    "存货": "存货",
    "其他流动资产": "其他流动资产",
    "流动资产合计": "流动资产合计",
    "长期股权投资": "长期股权投资",
    "其他权益工具投资": "其他权益工具投资",
    "投资性房地产": "投资性房地产",
    "固定资产": "固定资产",
    "在建工程": "在建工程",
    "无形资产": "无形资产",
    "开发支出": "开发支出",
    "商誉": "商誉",
    "长期待摊费用": "长期待摊费用",
    "递延所得税资产": "递延所得税资产",
    "其他非流动资产": "其他非流动资产",
    "非流动资产合计": "非流动资产合计",
    "资产总计": "资产总计",
    "资产合计": "资产总计",
    "短期借款": "短期借款",
    "应付票据": "应付票据",
    "应付账款": "应付账款",
    "预收款项": "预收款项",
    "合同负债": "合同负债",
    "应付职工薪酬": "应付职工薪酬",
    "应交税费": "应交税费",
    "其他应付款": "其他应付款",
    "一年内到期的非流动负债": "一年内到期的非流动负债",
    "其他流动负债": "其他流动负债",
    "流动负债合计": "流动负债合计",
    "长期借款": "长期借款",
    "应付债券": "应付债券",
    "租赁负债": "租赁负债",
    "长期应付款": "长期应付款",
    "递延所得税负债": "递延所得税负债",
    "非流动负债合计": "非流动负债合计",
    "负债合计": "负债合计",
    "负债总计": "负债合计",
    "实收资本（或股本）": "实收资本（或股本）",
    "资本公积": "资本公积",
    "盈余公积": "盈余公积",
    "未分配利润": "未分配利润",
    "归属于母公司所有者权益合计": "归属于母公司所有者权益合计",
    "母公司股东权益": "归属于母公司所有者权益合计",
    "少数股东权益": "少数股东权益",
    "所有者权益（或股东权益）合计": "所有者权益（或股东权益）合计",
    "负债和所有者权益（或股东权益）总计": "负债和所有者权益（或股东权益）总计",
    "应收款项融资": "应收款项融资",
    "合同资产": "合同资产",
    "债权投资": "债权投资",
    "其他债权投资": "其他债权投资",
    "长期应收款": "长期应收款",
    "固定资产清理": "固定资产清理",
    "使用权资产": "使用权资产",
}

IS_FIELD_MAP = {
    "营业收入": "营业收入",
    "营业总收入": "营业收入",
    "营业成本": "营业成本",
    "营业税金及附加": "营业税金及附加",
    "销售费用": "销售费用",
    "管理费用": "管理费用",
    "研发费用": "研发费用",
    "财务费用": "财务费用",
    "其中：利息费用": "其中：利息费用",
    "其中：利息收入": "其中：利息收入",
    "投资收益": "投资收益",
    "其他收益": "其他收益",
    "资产减值损失(新)": "资产减值损失",
    "信用减值损失(新)": "信用减值损失",
    "资产处置收益": "资产处置收益",
    "营业利润": "营业利润",
    "营业外收入": "营业外收入",
    "营业外支出": "营业外支出",
    "利润总额": "利润总额",
    "所得税费用": "所得税费用",
    "净利润": "净利润",
    "归属母公司净利润": "归母净利润",
    "归属于母公司所有者的净利润": "归母净利润",
    "归属少数股东损益": "少数股东损益",
    "归属于少数股东的损益": "少数股东损益",
    "扣非净利润": "扣非净利润",
    "扣除非经常性损益的净利润": "扣非净利润",
    "基本每股收益": "基本每股收益",
    "稀释每股收益": "稀释每股收益",
    "其他综合收益": "其他综合收益",
    "综合收益总额": "综合收益总额",
    "归属母公司综合收益": "归属母公司综合收益",
    "归属少数股东综合收益": "归属少数股东综合收益",
}

CF_FIELD_MAP = {
    # TDX简写 → 标准全称
    "销售商品收到现金": "销售商品、提供劳务收到的现金",
    "销售商品、提供劳务收到的现金": "销售商品、提供劳务收到的现金",
    "经营活动现金流入小计": "经营活动现金流入小计",
    "购买商品支付现金": "购买商品、接受劳务支付的现金",
    "购买商品、接受劳务支付的现金": "购买商品、接受劳务支付的现金",
    "支付给职工以及为职工支付的现金": "支付给职工以及为职工支付的现金",
    "支付的各项税费": "支付的各项税费",
    "经营活动现金流出小计": "经营活动现金流出小计",
    "经营活动现金流量净额": "经营活动产生的现金流量净额",
    "经营活动产生的现金流量净额": "经营活动产生的现金流量净额",
    "收回投资收现": "收回投资收到的现金",
    "收回投资收到的现金": "收回投资收到的现金",
    "取得投资收益收现": "取得投资收益收到的现金",
    "取得投资收益收到的现金": "取得投资收益收到的现金",
    "处置固定资产、无形资产和其他长期资产收回的现金净额": "处置固定资产、无形资产和其他长期资产收回的现金净额",
    "投资活动现金流入小计": "投资活动现金流入小计",
    "购建资产支付现金": "购建固定资产、无形资产和其他长期资产支付的现金",
    "购建固定资产、无形资产和其他长期资产支付的现金": "购建固定资产、无形资产和其他长期资产支付的现金",
    "投资支付的现金": "投资支付的现金",
    "投资活动现金流出小计": "投资活动现金流出小计",
    "投资活动现金流量净额": "投资活动产生的现金流量净额",
    "投资活动产生的现金流量净额": "投资活动产生的现金流量净额",
    "取得借款收现": "取得借款收到的现金",
    "吸收投资收到的现金": "吸收投资收到的现金",
    "取得借款收到的现金": "取得借款收到的现金",
    "筹资活动现金流入小计": "筹资活动现金流入小计",
    "偿还债务支付的现金": "偿还债务支付的现金",
    "分配股利付息": "分配股利、利润或偿付利息支付的现金",
    "分配股利、利润或偿付利息支付的现金": "分配股利、利润或偿付利息支付的现金",
    "筹资活动现金流出小计": "筹资活动现金流出小计",
    "筹资活动现金流量净额": "筹资活动产生的现金流量净额",
    "筹资活动产生的现金流量净额": "筹资活动产生的现金流量净额",
    "现金及等价物净增加额": "现金及现金等价物净增加额",
    "现金及现金等价物净增加额": "现金及现金等价物净增加额",
    "期初现金及等价物余额": "期初现金及现金等价物余额",
    "期初现金及现金等价物余额": "期初现金及现金等价物余额",
    "期末现金及等价物余额": "期末现金及现金等价物余额",
    "期末现金及现金等价物余额": "期末现金及现金等价物余额",
    "净利润": "间接法-净利润",
    "资产减值准备": "间接法-资产减值准备",
    "固定资产折旧、油气资产折耗、生产性生物资产折旧": "间接法-固定资产折旧",
    "无形资产摊销": "间接法-无形资产摊销",
    "长期待摊费用摊销": "间接法-长期待摊费用摊销",
    "财务费用": "间接法-财务费用",
    "投资损失": "间接法-投资损失",
    "递延所得税资产减少": "间接法-递延所得税资产减少",
    "递延所得税负债增加": "间接法-递延所得税负债增加",
    "存货的减少": "间接法-存货的减少",
    "经营性应收项目的减少": "间接法-经营性应收项目的减少",
    "经营性应付项目的增加": "间接法-经营性应付项目的增加",
    "收到的其他与经营活动有关的现金": "收到的其他与经营活动有关的现金",
    "支付的其他与经营活动有关的现金": "支付的其他与经营活动有关的现金",
}


def load_tdx_file(filepath):
    """加载 TDX 响应文件，返回 rows 列表。支持三种格式：
    1. 标准JSON: {"response":{"transformed":{"tables":[{"rows":[...]}]}}}
    2. MCP包装: {"content":[{"type":"text","text":"summary\\n\\n{JSON}"}]}
    3. 纯文本: summary text + JSON embedded
    """
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read().strip()

    # 尝试直接JSON解析
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 可能前半段是文本摘要，后半段是JSON，找最后一个 {
        idx = raw.rfind('{"ok"')
        if idx > 0:
            data = json.loads(raw[idx:])
        else:
            raise ValueError(f"无法解析TDX数据文件: {filepath}")

    # MCP包装格式: content[0].text 包含 summary + JSON
    if "content" in data and isinstance(data["content"], list):
        text = data["content"][0].get("text", "")
        # 分割摘要和JSON — 找 "ok" 部分
        idx = text.find('"ok"')
        if idx > 0:
            json_str = "{" + text[idx:]
            data = json.loads(json_str)
        else:
            data = json.loads(text)

    # 提取 rows
    rows = []
    if "response" in data:
        transformed = data["response"].get("transformed", {})
    else:
        transformed = data.get("transformed", {})

    tables = transformed.get("tables", [])
    if tables:
        rows = tables[0].get("rows", [])
    if not rows:
        rows = transformed.get("rows", [])

    return rows


def extract_annual(rows, periods=4):
    """从TDX rows中提取最近N期年报（日期字段以-12-31结尾）"""
    annual = []
    for r in rows:
        # TDX数据有两套日期字段：'截止日期'（如"2025年报"）和 '日期'（如"2025-12-31"）
        date_str = r.get("日期", r.get("截止日期", ""))
        if date_str.endswith("-12-31"):
            annual.append((date_str, r))
    if not annual:
        raise ValueError(f"未找到年报数据（日期以-12-31结尾），共{len(rows)}行")
    annual.sort(key=lambda x: x[0], reverse=True)
    selected = annual[:periods]
    selected.sort(key=lambda x: x[0])
    return [r for _, r in selected], [d for d, _ in selected]


def map_and_convert(rows, field_map, periods_list):
    """将TDX行数据映射到标准格式，yuan→亿元。
    映射表中有的字段→按映射改名；映射表中没有的→自动透传保留原名。
    """
    result = {}
    skip_keys = {"截止日期", "日期", "报告期"}

    for row, period in zip(rows, periods_list):
        for tdx_key, val in row.items():
            if tdx_key in skip_keys:
                continue
            # 映射改名 > 透传原名
            std_key = field_map.get(tdx_key, tdx_key)
            if std_key not in result:
                result[std_key] = {}
            # 转换金额：yuan → 亿元
            if isinstance(val, (int, float)):
                converted = round(val / YI, 4) if val is not None else None
            else:
                converted = None
            result[std_key][period] = converted
    return result


def main():
    parser = argparse.ArgumentParser(description="TDX数据提取与裁剪")
    parser.add_argument("stock_code", help="股票代码")
    parser.add_argument("company_name", help="公司名称")
    parser.add_argument("--bs", required=True, help="TDX资产负债表JSON文件")
    parser.add_argument("--is", dest="is_file", required=True, help="TDX利润表JSON文件")
    parser.add_argument("--cf", required=True, help="TDX现金流量表JSON文件")
    parser.add_argument("--start-year", type=int, default=None, help="起始年份（含），如2020")
    parser.add_argument("--end-year", type=int, default=None, help="结束年份（含），如2023")
    args = parser.parse_args()

    # 加载数据
    print("加载TDX数据...")
    bs_rows = load_tdx_file(args.bs)
    is_rows = load_tdx_file(args.is_file)
    cf_rows = load_tdx_file(args.cf)
    print(f"  资产负债表: {len(bs_rows)} 期")
    print(f"  利润表: {len(is_rows)} 期")
    print(f"  现金流量表: {len(cf_rows)} 期")

    # 提取年报：有年份区间时取全部年报，否则取最近4期
    max_periods = 99 if (args.start_year is not None or args.end_year is not None) else 4
    bs_rows, bs_periods = extract_annual(bs_rows, periods=max_periods)
    is_rows, is_periods = extract_annual(is_rows, periods=max_periods)
    cf_rows, cf_periods = extract_annual(cf_rows, periods=max_periods)

    # 统一取周期的交集
    all_periods = sorted(set(bs_periods) & set(is_periods) & set(cf_periods))
    if len(all_periods) < 2:
        print("❌ 年报数据不足，无法排雷", file=sys.stderr)
        sys.exit(1)

    # 指定年份区间时：筛选区间内年报
    if args.start_year is not None or args.end_year is not None:
        filtered = []
        for p in all_periods:
            year = int(p[:4])
            if args.start_year is not None and year < args.start_year:
                continue
            if args.end_year is not None and year > args.end_year:
                continue
            filtered.append(p)
        if filtered:
            all_periods = filtered
        else:
            print(f"⚠️ 警告: 未找到 {args.start_year}-{args.end_year} 区间内的年报，使用最近4期", file=sys.stderr)
            all_periods = all_periods[-4:]
    else:
        all_periods = all_periods[-4:]  # 默认最近4期

    print(f"\n选定报告期:")
    for i, p in enumerate(all_periods):
        print(f"  第{i+1}期: {p}")

    # 过滤rows到选定周期
    bs_rows = [r for r, p in zip(bs_rows, bs_periods) if p in all_periods]
    is_rows = [r for r, p in zip(is_rows, is_periods) if p in all_periods]
    cf_rows = [r for r, p in zip(cf_rows, cf_periods) if p in all_periods]

    # 映射并转换
    balance_sheet = map_and_convert(bs_rows, BS_FIELD_MAP, all_periods)
    income_stmt = map_and_convert(is_rows, IS_FIELD_MAP, all_periods)
    cash_flow = map_and_convert(cf_rows, CF_FIELD_MAP, all_periods)

    # 构建标准输出
    output = {
        "stock_code": args.stock_code,
        "company_name": args.company_name,
        "mode": "annual",
        "is_financial": False,
        "is_consolidated": True,
        "has_restated": False,
        "restated_periods": [],
        "periods": all_periods,
        "period_names": ["第一期", "第二期", "第三期", "第四期"][:len(all_periods)],
        "balance_sheet": balance_sheet,
        "profit_statement": income_stmt,
        "cash_flow_statement": cash_flow,
        "abstract": {},
        "dividend": [],
    }

    # 补算 abstract 衍生指标（TDX不直接提供财务摘要，从BS/IS自算）
    for period in all_periods:
        abs_item = {}
        bs_p = {k: balance_sheet.get(k, {}).get(period) for k in balance_sheet}
        is_p = {k: income_stmt.get(k, {}).get(period) for k in income_stmt}

        # 流动比率 = 流动资产合计 / 流动负债合计
        if bs_p.get("流动资产合计") and bs_p.get("流动负债合计") and bs_p["流动负债合计"] > 0:
            abs_item["流动比率"] = round(bs_p["流动资产合计"] / bs_p["流动负债合计"], 4)
        # 速动比率 ≈ (流动资产合计 - 存货) / 流动负债合计
        if bs_p.get("流动资产合计") and bs_p.get("存货") is not None and bs_p.get("流动负债合计") and bs_p["流动负债合计"] > 0:
            abs_item["速动比率"] = round((bs_p["流动资产合计"] - bs_p["存货"]) / bs_p["流动负债合计"], 4)
        # 总资产周转率 = 营业收入 / 总资产
        if is_p.get("营业收入") and bs_p.get("资产总计") and bs_p["资产总计"] > 0:
            abs_item["总资产周转率"] = round(is_p["营业收入"] / bs_p["资产总计"], 4)
        # 净资产收益率 = 净利润 / 归母净资产（小数形式）
        归母净资产 = bs_p.get("归属于母公司所有者权益合计")
        if is_p.get("净利润") and 归母净资产 and 归母净资产 > 0:
            abs_item["净资产收益率"] = round(is_p["净利润"] / 归母净资产, 4)

        # 每股净资产 / 每股经营现金流
        股本 = bs_p.get("实收资本（或股本）") or bs_p.get("实收资本")
        if 归母净资产 and 股本 and 股本 > 0:
            abs_item["每股净资产"] = round(归母净资产 / 股本, 4)
        cf_p = {k: cash_flow.get(k, {}).get(period) for k in cash_flow}
        cfo_val = cf_p.get("经营活动产生的现金流量净额")
        if cfo_val is not None and 股本 and 股本 > 0:
            abs_item["每股经营现金流"] = round(cfo_val / 股本, 4)

        # 保守速动比率 = (流动资产 - 存货 - 预付 - 其他应收) / 流动负债
        流资 = bs_p.get("流动资产合计")
        流债 = bs_p.get("流动负债合计")
        if all(v is not None for v in [流资, bs_p.get("存货"), bs_p.get("预付款项"), bs_p.get("其他应收款"), 流债]) and 流债 > 0:
            保守 = 流资 - bs_p["存货"] - bs_p.get("预付款项", 0) - bs_p.get("其他应收款", 0)
            abs_item["保守速动比率"] = round(保守 / 流债, 4)

        # 每股未分配利润 / 每股资本公积金
        未分配 = bs_p.get("未分配利润")
        if 未分配 is not None and 股本 and 股本 > 0:
            abs_item["每股未分配利润"] = round(未分配 / 股本, 4)
        资本公积 = bs_p.get("资本公积")
        if 资本公积 is not None and 股本 and 股本 > 0:
            abs_item["每股资本公积金"] = round(资本公积 / 股本, 4)

        if abs_item:
            output["abstract"][period] = abs_item

    # 输出
    out_file = f"{args.stock_code}_合并财报数据.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✓ 数据已保存至: {out_file}")
    print(f"  BS科目: {len(balance_sheet)}, IS科目: {len(income_stmt)}, CF科目: {len(cash_flow)}")


if __name__ == "__main__":
    main()
