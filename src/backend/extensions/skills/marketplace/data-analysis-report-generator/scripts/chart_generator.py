#!/usr/bin/env python3
"""ECharts chart configuration generator.

Reads analysis JSON from stdin (or --input) and generates ECharts option objects
suitable for embedding in HTML reports.

Usage:
  python chart_generator.py --input <analysis.json> --style <style_name> --output <charts.json>
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ── Color palettes for the 11 styles ──────────────────────────────────────

PALETTES = {
    "ft": ["#9E2B2E", "#D56227", "#3F6B84", "#6A994E", "#C4A35A", "#8B5A2B"],
    "mckinsey": ["#003A70", "#0083CA", "#50A0D0", "#7DB9DE", "#B0D4ED", "#002E5C"],
    "economist": ["#E3120B", "#0A2239", "#4A6FA5", "#93B5D6", "#C92A2A", "#2D4059"],
    "bloomberg": ["#FF6B00", "#2ECC71", "#3498DB", "#9B59B6", "#E74C3C", "#1ABC9C"],
    "hbr": ["#2C3E50", "#3498DB", "#E74C3C", "#F39C12", "#27AE60", "#8E44AD"],
    "nature": ["#2166AC", "#67A9CF", "#D1E5F0", "#F7F7F7", "#F4A582", "#B2182B"],
    "wired": ["#FF3366", "#00D2FF", "#FFD700", "#7B68EE", "#00FA9A", "#FF8C00"],
    "nyt": ["#333333", "#999999", "#CCCCCC", "#666666", "#1A1A1A", "#B3B3B3"],
    "wsj": ["#0C2340", "#0068B4", "#A6192E", "#7BA7BC", "#D0AE3D", "#54585A"],
    "mit": ["#A31F34", "#8A8B8C", "#3388BB", "#D9B845", "#5B6770", "#212529"],
    "36kr": ["#00DAA7", "#1A1A2E", "#16213E", "#0F3460", "#E94560", "#533483"],
}

STYLE_LABELS = {
    "ft": "FT Style",
    "mckinsey": "McKinsey Style",
    "economist": "Economist Style",
    "bloomberg": "Bloomberg Style",
    "hbr": "Harvard Business Review Style",
    "nature": "Nature Style",
    "wired": "Wired Style",
    "nyt": "The New York Times Style",
    "wsj": "Wall Street Journal Style",
    "mit": "MIT Technology Review Style",
    "36kr": "36Kr Style",
}


def pick_chart_type(metric_cols, dim_cols, has_timeline, correlations) -> list[dict]:
    """Recommend charts and generate ECharts configs based on data characteristics."""
    charts = []
    palette = ["#5470C6", "#91CC75", "#FAC858", "#EE6666", "#73C0DE", "#3BA272", "#FC8452", "#9A60B4"]

    # 1. Time-series line chart (if timeline exists)
    # Will be filled by timeline_series

    # 2. Bar chart for top dimension x metric
    if dim_cols and metric_cols:
        charts.append({
            "id": "bar_top",
            "title": f"Top {dim_cols[0]} by {metric_cols[0]}",
            "type": "bar",
            "description": f"Ranking of {dim_cols[0]} sorted by {metric_cols[0]}",
        })

    # 3. Pie chart if 1 dimension and 1-2 metrics
    if len(dim_cols) >= 1 and len(metric_cols) <= 2:
        for mc in metric_cols[:2]:
            charts.append({
                "id": f"pie_{mc}",
                "title": f"{mc} Distribution by {dim_cols[0]}",
                "type": "pie",
                "description": f"Proportion of {mc} across {dim_cols[0]} categories",
            })

    # 4. Scatter or correlation heatmap for 2+ metrics
    if len(metric_cols) >= 2:
        if correlations:
            # Correlation heatmap
            charts.append({
                "id": "heatmap_corr",
                "title": "Metric Correlations",
                "type": "heatmap",
                "description": "Correlation matrix between metric columns",
            })

        # Scatter of top 2 metrics
        charts.append({
            "id": "scatter_metrics",
            "title": f"{metric_cols[0]} vs {metric_cols[1]}",
            "type": "scatter",
            "description": f"Scatter plot of {metric_cols[0]} and {metric_cols[1]}",
        })

    # 5. Timeline line chart
    if has_timeline:
        for mc in metric_cols[:3]:
            charts.append({
                "id": f"line_{mc}",
                "title": f"{mc} Trend Over Time",
                "type": "line",
                "description": f"Time series trend of {mc}",
            })

    # 6. Stats summary table (always include)
    charts.insert(0, {
        "id": "summary_stats",
        "title": "Key Metrics Summary",
        "type": "table",
        "description": "Descriptive statistics for all metric columns",
    })

    return charts


def generate_bar_option(title, categories, series_data, palette) -> dict:
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "category",
            "data": categories,
            "axisLabel": {"rotate": 45 if len(categories) > 6 else 0},
        },
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": series_data, "itemStyle": {"color": palette[0]}}],
        "grid": {"bottom": 80},
    }


def generate_pie_option(title, data_pairs, palette) -> dict:
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "series": [{
            "type": "pie",
            "radius": ["40%", "70%"],
            "center": ["50%", "55%"],
            "data": [{"name": str(k), "value": v} for k, v in data_pairs],
            "label": {"formatter": "{b}: {d}%"},
            "color": palette,
        }],
    }


def generate_line_option(title, x_data, series_list, palette) -> dict:
    series = []
    for i, (name, values) in enumerate(series_list):
        series.append({
            "name": name,
            "type": "line",
            "data": values,
            "smooth": True,
            "itemStyle": {"color": palette[i % len(palette)]},
        })
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [s["name"] for s in series], "bottom": 0},
        "xAxis": {"type": "category", "data": x_data, "boundaryGap": False},
        "yAxis": {"type": "value"},
        "series": series,
        "grid": {"bottom": 60},
    }


def generate_scatter_option(title, x_values, y_values, x_label, y_label, palette) -> dict:
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
        "tooltip": {"trigger": "item", "formatter": f"{x_label}: {{c[0]}}<br/>{y_label}: {{c[1]}}"},
        "xAxis": {"type": "value", "name": x_label},
        "yAxis": {"type": "value", "name": y_label},
        "series": [{
            "type": "scatter",
            "data": [[float(x), float(y)] for x, y in zip(x_values, y_values)],
            "itemStyle": {"color": palette[0]},
        }],
    }


def generate_heatmap_option(title, labels, matrix, palette) -> dict:
    data = []
    for i in range(len(labels)):
        for j in range(len(labels)):
            data.append([j, i, round(matrix[i][j], 2) if matrix[i][j] else 0])
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 16}},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "category", "data": labels, "splitArea": {"show": True}},
        "yAxis": {"type": "category", "data": labels, "splitArea": {"show": True}},
        "visualMap": {"min": -1, "max": 1, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
        "series": [{
            "type": "heatmap",
            "data": data,
            "label": {"show": True},
            "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.5)"}},
        }],
        "grid": {"bottom": 80},
    }


def build_chart_options(analysis: dict, style: str) -> dict[str, Any]:
    """Build all ECharts option objects."""
    palette = PALETTES.get(style, PALETTES["ft"])
    dim_cols = analysis.get("dimensions", [])
    metric_cols = analysis.get("metrics", [])
    has_timeline = bool(analysis.get("timelines"))
    correlations = analysis.get("correlations", {})
    stats = analysis.get("stats", {})
    top_n = analysis.get("top_n", {})
    timeline_data = analysis.get("timeline", {})

    chart_recs = pick_chart_type(metric_cols, dim_cols, has_timeline, correlations)
    chart_options = {}

    for rec in chart_recs:
        cid = rec["id"]
        ctype = rec["type"]

        if ctype == "table":
            chart_options[cid] = {
                "title": rec["title"],
                "type": "table",
                "description": rec["description"],
                "stats": stats,
            }
        elif ctype == "bar":
            first_key = list(top_n.keys())[0] if top_n else None
            if first_key and top_n[first_key]:
                items = top_n[first_key]
                cats = [str(r[dim_cols[0]]) if dim_cols else str(i) for i, r in enumerate(items)]
                vals = [r.get(metric_cols[0], 0) if metric_cols else 0 for r in items]
                chart_options[cid] = {
                    "title": rec["title"],
                    "type": "bar",
                    "description": rec["description"],
                    "option": generate_bar_option(rec["title"], cats, vals, palette),
                }
        elif ctype == "pie":
            prefix = cid.replace("pie_", "")
            key = f"{dim_cols[0]}__{prefix}" if dim_cols else None
            if key and key in top_n:
                items = top_n[key]
                data = [(str(r[dim_cols[0]]), r.get(prefix, 0)) for r in items]
                chart_options[cid] = {
                    "title": rec["title"],
                    "type": "pie",
                    "description": rec["description"],
                    "option": generate_pie_option(rec["title"], data, palette),
                }
        elif ctype == "line":
            prefix = cid.replace("line_", "")
            if timeline_data and timeline_data.get("series"):
                series = timeline_data["series"]
                x_data = [r.get(timeline_data["column"], "") for r in series]
                series_list = [(prefix, [r.get(prefix, 0) for r in series])]
                chart_options[cid] = {
                    "title": rec["title"],
                    "type": "line",
                    "description": rec["description"],
                    "option": generate_line_option(rec["title"], x_data, series_list, palette),
                }
        elif ctype == "scatter":
            if len(metric_cols) >= 2:
                m1, m2 = metric_cols[0], metric_cols[1]
                raw = analysis.get("_raw_values", {})
                x_vals = raw.get(m1, [])
                y_vals = raw.get(m2, [])
                if x_vals and y_vals:
                    chart_options[cid] = {
                        "title": rec["title"],
                        "type": "scatter",
                        "description": rec["description"],
                        "option": generate_scatter_option(rec["title"], x_vals, y_vals, m1, m2, palette),
                    }
        elif ctype == "heatmap":
            if correlations and metric_cols:
                n = len(metric_cols)
                matrix = [[0.0] * n for _ in range(n)]
                for i in range(n):
                    matrix[i][i] = 1.0
                for key, val in correlations.items():
                    parts = key.split("__")
                    if len(parts) == 2:
                        try:
                            i = metric_cols.index(parts[0])
                            j = metric_cols.index(parts[1])
                            matrix[i][j] = val
                            matrix[j][i] = val
                        except ValueError:
                            pass
                chart_options[cid] = {
                    "title": rec["title"],
                    "type": "heatmap",
                    "description": rec["description"],
                    "option": generate_heatmap_option(rec["title"], metric_cols, matrix, palette),
                }

    # Remove empty entries
    return {k: v for k, v in chart_options.items() if v}


def generate_raw_values(analysis: dict) -> dict:
    """Extract raw values for scatter chart from the analysis data."""
    # This would be done in the main analysis script ideally
    return {}


def main():
    parser = argparse.ArgumentParser(description="ECharts Chart Config Generator")
    parser.add_argument("--input", "-i", required=True, help="Analysis JSON file path")
    parser.add_argument("--style", "-s", default="ft", choices=list(PALETTES.keys()), help="Report style")
    parser.add_argument("--output", "-o", default=None, help="Output JSON path")
    args = parser.parse_args()

    try:
        analysis = json.loads(Path(args.input).read_text(encoding="utf-8"))
        charts = build_chart_options(analysis, args.style)

        result = {
            "style": args.style,
            "style_label": STYLE_LABELS.get(args.style, args.style),
            "palette": PALETTES.get(args.style, PALETTES["ft"]),
            "chart_count": len(charts),
            "charts": charts,
        }

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