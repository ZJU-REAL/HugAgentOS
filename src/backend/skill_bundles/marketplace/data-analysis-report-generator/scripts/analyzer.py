#!/usr/bin/env python3
"""Data Analysis Engine - reads Excel/CSV, detects structure, computes statistics.

Usage:
  python analyzer.py <input_file> [--output <json_path>] [--max-rows <n>]

Outputs JSON with:
  - meta: file info, shape, column list
  - columns: per-column type detection (dimension/metric/timeline/unknown)
  - stats: descriptive statistics for metric columns
  - correlations: correlation matrix for numeric columns
  - timeline: time-series grouping if timeline column detected
  - top_n: top categories by metric columns
"""

import argparse
import json
import sys
from pathlib import Path


def load_data(filepath: str):
    """Load Excel/CSV into DataFrame."""
    import pandas as pd

    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(filepath, engine="openpyxl")
    elif suffix == ".csv":
        encodings = ["utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"]
        for enc in encodings:
            try:
                df = pd.read_csv(filepath, encoding=enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            df = pd.read_csv(filepath, encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type: {suffix}")
    return df


def detect_column_type(series, col_name: str) -> str:
    """Detect column type: dimension, metric, or timeline."""
    import pandas as pd
    import numpy as np

    name_lower = str(col_name).lower().strip()

    # Time-related keywords
    time_keywords = [
        "date", "time", "year", "month", "day", "week", "quarter",
        "日期", "时间", "年", "月", "日", "周", "季度", "timestamp",
        "period", "created", "updated", "datetime", "period",
    ]
    if any(kw in name_lower for kw in time_keywords):
        return "timeline"

    # Numeric / metric detection
    if pd.api.types.is_numeric_dtype(series):
        # Check if it looks like an ID column
        if "id" in name_lower or "code" in name_lower or "编号" in name_lower or "代码" in name_lower:
            if series.nunique() == len(series):
                return "dimension"

        # Metric keywords
        metric_keywords = [
            "amount", "value", "price", "revenue", "sales", "profit", "cost",
            "rate", "ratio", "count", "total", "sum", "avg", "mean", "pct",
            "percent", "growth", "change", "volume", "score", "index", "rank",
            "金额", "数量", "价格", "收入", "销售", "利润", "成本", "比率",
            "占比", "增长", "增长", "指数", "排名", "得分", "计数", "总额",
        ]
        if any(kw in name_lower for kw in metric_keywords):
            return "metric"

        # Low cardinality numbers → dimension
        if series.nunique() <= max(20, len(series) * 0.1):
            return "dimension"
        return "metric"

    # Check if string/categorical
    if series.nunique() <= max(50, len(series) * 0.3):
        return "dimension"

    return "unknown"


def compute_stats(df, metric_cols, max_rows=1000):
    """Compute descriptive stats for metric columns."""
    import numpy as np

    stats = {}
    for col in metric_cols:
        series = df[col].dropna()
        if len(series) == 0:
            continue
        stats[col] = {
            "count": int(len(series)),
            "mean": float(round(series.mean(), 4)),
            "median": float(round(series.median(), 4)),
            "std": float(round(series.std(), 4)),
            "min": float(round(series.min(), 4)),
            "max": float(round(series.max(), 4)),
            "sum": float(round(series.sum(), 2)),
            "q25": float(round(series.quantile(0.25), 4)),
            "q75": float(round(series.quantile(0.75), 4)),
            "missing": int(series.isna().sum()),
            "missing_pct": float(round(series.isna().mean() * 100, 2)),
            "cv": float(round(series.std() / series.mean() * 100, 2)) if series.mean() != 0 else None,
            "skew": float(round(series.skew(), 4)),
        }
    return stats


def compute_correlations(df, metric_cols):
    """Correlation matrix for metric columns."""
    import numpy as np

    if len(metric_cols) < 2:
        return {}

    corr = df[metric_cols].corr().round(4)
    result = {}
    for i, c1 in enumerate(metric_cols):
        for c2 in metric_cols[i + 1 :]:
            val = corr.loc[c1, c2]
            if not np.isnan(val):
                result[f"{c1}__{c2}"] = float(val)
    return result


def analyze_timeline(df, timeline_col, metric_cols):
    """Time-series grouping."""
    import pandas as pd

    df = df.copy()
    df[timeline_col] = pd.to_datetime(df[timeline_col], errors="coerce")
    df = df.dropna(subset=[timeline_col])

    if len(df) == 0:
        return None

    # Determine granularity
    span_days = (df[timeline_col].max() - df[timeline_col].min()).days
    if span_days <= 0:
        freq = "D"
    elif span_days <= 90:
        freq = "D"
    elif span_days <= 730:
        freq = "ME"
    elif span_days <= 1825:
        freq = "QE"
    else:
        freq = "YE"

    grouped = df.set_index(timeline_col).resample(freq)[metric_cols].sum().reset_index()
    grouped[timeline_col] = grouped[timeline_col].dt.strftime("%Y-%m-%d")

    return {
        "column": timeline_col,
        "granularity": freq,
        "span_days": span_days,
        "series": grouped.to_dict(orient="records"),
    }


def top_n_by_dimension(df, dim_cols, metric_cols, n=10):
    """Top N categories per dimension column."""
    result = {}
    for dc in dim_cols:
        for mc in metric_cols:
            top = df.groupby(dc)[mc].sum().nlargest(n).reset_index()
            result[f"{dc}__{mc}"] = top.to_dict(orient="records")
    return result


def run(filepath: str, max_rows: int = 5000) -> dict:
    """Main analysis pipeline."""
    import pandas as pd
    import numpy as np

    df = load_data(filepath)

    # Sample if too large
    total_rows = len(df)
    if total_rows > max_rows:
        df = df.sample(n=max_rows, random_state=42).reset_index(drop=True)

    # Column type detection
    columns_info = []
    for col in df.columns:
        ctype = detect_column_type(df[col], col)
        dtype = str(df[col].dtype)
        unique = int(df[col].nunique())
        columns_info.append({
            "name": str(col),
            "type": ctype,
            "dtype": dtype,
            "unique": unique,
            "missing": int(df[col].isna().sum()),
        })

    dim_cols = [c["name"] for c in columns_info if c["type"] == "dimension"]
    metric_cols = [c["name"] for c in columns_info if c["type"] == "metric"]
    timeline_cols = [c["name"] for c in columns_info if c["type"] == "timeline"]

    # Statistics
    stats = compute_stats(df, metric_cols, max_rows)
    correlations = compute_correlations(df, metric_cols)

    # Timeline analysis
    timeline = None
    if timeline_cols:
        tc = timeline_cols[0]
        timeline = analyze_timeline(df, tc, metric_cols)

    # Top N
    topn = {}
    if dim_cols and metric_cols:
        topn = top_n_by_dimension(df, dim_cols, metric_cols)

    return {
        "meta": {
            "file": Path(filepath).name,
            "rows": total_rows,
            "rows_analyzed": len(df),
            "columns": len(df.columns),
            "dimension_cols": len(dim_cols),
            "metric_cols": len(metric_cols),
            "timeline_cols": len(timeline_cols),
        },
        "columns": columns_info,
        "dimensions": dim_cols,
        "metrics": metric_cols,
        "timelines": timeline_cols,
        "stats": stats,
        "correlations": correlations,
        "timeline": timeline,
        "top_n": topn,
    }


def main():
    parser = argparse.ArgumentParser(description="Data Analysis Engine")
    parser.add_argument("input", help="Input Excel/CSV file path")
    parser.add_argument("--output", "-o", help="Output JSON path", default=None)
    parser.add_argument("--max-rows", type=int, default=5000)
    args = parser.parse_args()

    try:
        result = run(args.input, args.max_rows)
        json_str = json.dumps(result, ensure_ascii=False, indent=2, default=str)

        if args.output:
            Path(args.output).write_text(json_str, encoding="utf-8")
            print(json.dumps({"status": "ok", "output": args.output}, ensure_ascii=False))
        else:
            print(json_str)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()