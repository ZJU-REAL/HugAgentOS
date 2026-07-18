---
name: data-analysis-report-generator
description: Intelligent data analysis report generator. Auto-identifies Excel/CSV data structure (dimensions, metrics, timelines), performs multi-dimensional parallel analysis, and generates professional HTML reports with ECharts interactive charts. 
---

# Data Analysis Report Generator

Generate professional HTML analysis reports from Excel/CSV data with interactive ECharts charts. Supports 11 professional report styles: FT, McKinsey, Economist, Bloomberg, HBR, Nature, Wired, NYT, WSJ, MIT Technology Review, 36Kr. 

## Use Cases

- User sends an Excel/CSV file for analysis
- User asks to generate a data analysis report
- User mentions keywords like '数据分析报告', '分析报告', '数据报告', 'Excel分析', 'CSV分析', 'ECharts报告
- User wants professional chart-based data visualization from tabular data

## Quick Start

When user sends an Excel/CSV file:

1. Run `analyzer.py` on the file → get `analysis.json`
2. Run `chart_generator.py` with analysis + style → get `charts.json`
3. Build HTML report from template + style variables + chart data
4. Save and deliver the HTML file

## Workflow

### Step 1: Analyze Data

```bash
PYTHONIOENCODING=utf-8 python3 <skill_dir>/scripts/analyzer.py <input_file> --output <workspace>/_tmp_analysis.json
```

Read the output JSON. Key fields:
- `meta`: row/column counts, structure overview
- `columns`: per-column type detection (dimension/metric/timeline)
- `stats`: descriptive statistics for metrics
- `correlations`: inter-metric correlations
- `timeline`: time-series grouping (if timeline column detected)
- `top_n`: top categories by metric

### Step 2: Select Report Style

Default: `ft`. Select based on context or user preference:

| Style | Key | Best For |
|---|---|---|
| FT | `ft` | Financial data, investment analysis |
| McKinsey | `mckinsey` | Business strategy, consulting |
| Economist | `economist` | Macroeconomic, policy |
| Bloomberg | `bloomberg` | Market data, terminal-style |
| HBR | `hbr` | Academic business research |
| Nature | `nature` | Scientific research |
| Wired | `wired` | Tech industry, bold visuals |
| NYT | `nyt` | General news-style |
| WSJ | `wsj` | Business/market reporting |
| MIT Tech Review | `mit` | Technology research |
| 36Kr | `36kr` | Startup/tech ecosystem (Chinese) |

For detailed style definitions → read `references/report_styles.md`.

### Step 3: Generate Chart Configs

```bash
PYTHONIOENCODING=utf-8 python3 <skill_dir>/scripts/chart_generator.py --input <workspace>/_tmp_analysis.json --style <style_key> --output <workspace>/_tmp_charts.json
```

### Step 4: Build HTML Report

1. Read `assets/report_template.html`
2. Read `references/style_variables.json` → pick the selected style's CSS variables
3. Replace template placeholders:
   - `{{REPORT_TITLE}}` → descriptive report title (inferred from data)
   - `{{REPORT_SUBTITLE}}` → brief data summary line
   - `{{REPORT_DATE}}` → current date (YYYY-MM-DD)
   - `{{ROW_COUNT}}` → total rows
   - `{{COL_COUNT}}` → total columns
   - `{{STYLE_LABEL}}` → style display name
   - `{{BG_PRIMARY}}` through `{{FONT_BODY}}` → style CSS variables
   - `{{CHART_DATA_JSON}}` → charts JSON (from Step 3, only the `charts` object)
   - `{{SUMMARY_STATS_JSON}}` → stats JSON (from Step 1, the `stats` object)
   - `{{INSIGHTS}}` → AI-generated insight paragraphs (see Step 5)
4. Write the final HTML to workspace

### Step 5: Generate AI Insights

Based on the analysis results, write 3–6 insight paragraphs as `<p class="insight">` elements:

- **Top findings**: Which dimension categories dominate? Any surprising leaders?
- **Trend analysis**: If timeline data exists, what trends are visible? Growth/decline?
- **Correlation insights**: Which metrics are strongly correlated? Any unexpected relationships?
- **Distribution notes**: High variance metrics, outliers, or skewed distributions
- **Anomalies**: Missing data, extreme values, or unusual patterns

Each insight should be 1–3 sentences with specific numbers. Format:
```html
<p class="insight">🔍 <strong>Finding:</strong> Category X accounts for 42% of total revenue (¥1.2M), 3.5× the average.</p>
```

### Step 6: Deliver

Save the final HTML file to workspace and provide it to the user. The report is a self-contained HTML file that can be opened in any browser.

## Multi-File Analysis

When user sends multiple Excel/CSV files:
1. Analyze each file separately
2. Generate a combined report with separate sections per file
3. Add a cross-file comparison section if files share similar structure

## Customization

### User Requests a Different Style
Re-run Step 3–4 with the new style key. No need to re-analyze data.

### User Wants Specific Charts
After Step 1, manually construct ECharts option objects for the requested chart types. Reference `references/chart_types.md` for type selection rules.

### User Wants Additional Analysis
Run custom pandas analysis scripts using the same data file. Add results as additional sections in the HTML report.

## Dependencies

- Python 3.8+ with: `pandas`, `openpyxl`, `numpy`
- ECharts 5.5.0 (loaded via CDN in the HTML template — no local install needed)

If dependencies missing, install: `pip install pandas openpyxl numpy`

## File Structure

```
data-analysis-report/
├── SKILL.md                          # This file
├── scripts/
│   ├── analyzer.py                   # Data analysis engine
│   └── chart_generator.py            # ECharts config generator
├── references/
│   ├── report_styles.md              # 11 style definitions
│   ├── chart_types.md                # Chart type selection guide
│   └── style_variables.json          # CSS variables per style
└── assets/
    └── report_template.html          # HTML report template
```