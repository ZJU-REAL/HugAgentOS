#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — 一键执行入口
串联 download → compute → generate 三步流程

用法：
    python run_minesweeper.py 000858                              # 默认最近4期年报
    python run_minesweeper.py 000858 --mode quarterly             # 最近4期季报
    python run_minesweeper.py 000858 --start-year 2020 --end-year 2023  # 指定年份区间
"""

import subprocess
import sys
import os
import platform

# ============================================================
# VENV 自举：自动定位并使用持久化 minesweeper venv
# ============================================================
_VENV_NAME = "minesweeper"
_HOME = os.path.expanduser("~")
if platform.system() == "Windows":
    _VENV_PYTHON = os.path.join(_HOME, ".workbuddy", "binaries", "python", "envs", _VENV_NAME, "Scripts", "python.exe")
else:
    _VENV_PYTHON = os.path.join(_HOME, ".workbuddy", "binaries", "python", "envs", _VENV_NAME, "bin", "python")

if os.path.exists(_VENV_PYTHON) and not sys.executable.lower().startswith(_VENV_PYTHON.lower()):
    # 当前不在 minesweeper venv 中，自动 rerun
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

# 强制UTF-8输出（Windows GBK兼容）
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = SKILL_DIR


def run_step(name, cmd):
    """运行一个步骤"""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, shell=False, cwd=os.getcwd())
    if result.returncode != 0:
        print(f"\n❌ 步骤失败: {name}")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("用法: python run_minesweeper.py <股票代码> [--mode quarterly] [--start-year YYYY] [--end-year YYYY]")
        print("示例: python run_minesweeper.py 000858")
        print("      python run_minesweeper.py 000858 --start-year 2020 --end-year 2023")
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

    python = sys.executable
    dl_script = os.path.join(SCRIPTS_DIR, "download_statements.py")
    cp_script = os.path.join(SCRIPTS_DIR, "compute_indicators.py")
    gen_script = os.path.join(SCRIPTS_DIR, "generate_report.py")

    # 先跑下载获取公司名称
    mode_flag = ["--mode", mode] if mode != "annual" else []
    dl_cmd = [python, dl_script, stock_code] + mode_flag
    if start_year is not None:
        dl_cmd += ["--start-year", str(start_year)]
    if end_year is not None:
        dl_cmd += ["--end-year", str(end_year)]
    run_step("第一步：下载合并财报数据", dl_cmd)

    # 从下载的JSON中读取公司名称
    import json
    json_file = f"{stock_code}_合并财报数据.json"
    company_name = stock_code
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            company_name = data.get("company_name", stock_code)
    except (json.JSONDecodeError, FileNotFoundError, KeyError) as e:
        print(f"  ⚠ 读取公司名称失败({e.__class__.__name__})，使用代码 {stock_code}", flush=True)
        company_name = stock_code

    # 使用带公司名的文件名（清理旧文件避免rename失败）
    file_prefix = f"{stock_code}_{company_name}" if company_name != stock_code else stock_code
    json_file_renamed = f"{file_prefix}_合并财报数据.json"
    if json_file != json_file_renamed:
        try:
            if os.path.exists(json_file_renamed):
                os.remove(json_file_renamed)
            os.rename(json_file, json_file_renamed)
            json_file = json_file_renamed
        except (OSError, PermissionError) as e:
            print(f"  ⚠ 文件重命名失败({e.__class__.__name__})，使用原名 {json_file}", flush=True)
    indicator_file = f"{file_prefix}_排雷指标.json"
    md_report = f"{file_prefix}_财报排雷报告.md"
    html_report = f"{file_prefix}_财报排雷报告.html"

    # Step 2: 计算 — compute从输入文件名推导输出文件名
    run_step("第二步：计算排雷指标",
             [python, cp_script, json_file])

    # Step 3: 生成报告 — 推导文件名
    cp_output = json_file.replace("_合并财报数据.json", "_排雷指标.json")
    if cp_output != indicator_file:
        try:
            os.rename(cp_output, indicator_file)
        except (OSError, PermissionError) as e:
            indicator_file = cp_output
            print(f"  ⚠ 指标文件重命名失败({e.__class__.__name__})，使用原名", flush=True)
    run_step("第三步：生成排雷报告(MD+HTML)",
             [python, gen_script, indicator_file])

    # 最终摘要
    print(f"\n{'='*60}")
    print(f"  ✅ 排雷分析完成！")
    print(f"{'='*60}")
    print(f"  数据文件: {json_file}")
    print(f"  指标文件: {indicator_file}")
    print(f"  MD 报告:  {md_report}")
    print(f"  HTML报告: {html_report}")
    print()

    # 使用提示
    print(f"{'='*60}")
    print(f"  💡 排雷效果提示")
    print(f"{'='*60}")
    print(f"  推荐使用连续4年年报数据进行排雷分析，效果最佳。")
    print(f"    ✅ 年报模式（默认）：覆盖完整财年，趋势判定可靠 — 强烈推荐")
    print(f"    ⚠️ 季报模式：季节性波动大，效果打折扣 — 不推荐用于排雷")
    print(f"    ⚠️ 少于4期：无法覆盖完整风险升级规则 — 不推荐")
    print()


if __name__ == "__main__":
    main()
