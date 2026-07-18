---
name: csv-pipeline
description: Process, transform, analyze, and report on CSV and JSON data files. Use when the user needs to filter rows, join datasets, compute aggregates, convert formats, deduplicate, or generate summary reports from tabular data. Works with any CSV, TSV, or JSON Lines file.
---

# CSV Data Pipeline

Process tabular data (CSV, TSV, JSON, JSON Lines) using standard command-line tools and Python. No external dependencies required beyond Python 3.

## When to Use

- User provides a CSV/TSV/JSON file and asks to analyze, transform, or report on it
- Joining, filtering, grouping, or aggregating tabular data
- Converting between formats (CSV to JSON, JSON to CSV, etc.)
- Deduplicating, sorting, or cleaning messy data
- Generating summary statistics or reports
- ETL workflows: extract from one format, transform, load into another

## Quick Operations with Standard Tools

### Inspect

```bash
# Preview first rows
head -5 data.csv

# Count rows (excluding header)
tail -n +2 data.csv | wc -l

# Show column headers
head -1 data.csv

# Count unique values in a column (column 3)
tail -n +2 data.csv | cut -d',' -f3 | sort -u | wc -l
```

### Filter with `awk`

```bash
# Filter rows where column 3 > 100
awk -F',' 'NR==1 || $3 > 100' data.csv > filtered.csv

# Filter rows matching a pattern in column 2
awk -F',' 'NR==1 || $2 ~ /pattern/' data.csv > matched.csv

# Sum column 4
awk -F',' 'NR>1 {sum += $4} END {print sum}' data.csv
```

### Sort and Deduplicate

```bash
# Sort by column 2 (numeric)
head -1 data.csv > sorted.csv && tail -n +2 data.csv | sort -t',' -k2 -n >> sorted.csv

# Deduplicate by all columns
head -1 data.csv > deduped.csv && tail -n +2 data.csv | sort -u >> deduped.csv

# Deduplicate by specific column (keep first occurrence)
awk -F',' '!seen[$2]++' data.csv > deduped.csv
```

## Python Operations (for complex transforms)

### Read and Inspect

```python
import csv, json, sys
from collections import Counter

def read_csv(path, delimiter=','):
    """Read CSV/TSV into list of dicts."""
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter=delimiter))

def write_csv(rows, path, delimiter=','):
    """Write list of dicts to CSV."""
    if not rows:
        return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys(), delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)

# Quick stats
data = read_csv('data.csv')
print(f"Rows: {len(data)}")
print(f"Columns: {list(data[0].keys())}")
for col in data[0]:
    non_empty = sum(1 for r in data if r[col].strip())
    print(f"  {col}: {non_empty}/{len(data)} non-empty")
```

### Filter and Transform

```python
# Filter rows
filtered = [r for r in data if float(r['amount']) > 100]

# Add computed column
for r in data:
    r['total'] = str(float(r['price']) * int(r['quantity']))

# Rename columns
renamed = [{('new_name' if k == 'old_name' else k): v for k, v in r.items()} for r in data]

# Type conversion
for r in data:
    r['amount'] = float(r['amount'])
    r['date'] = r['date'].strip()
```

### Group and Aggregate

```python
from collections import defaultdict

def group_by(rows, key):
    """Group rows by a column value."""
    groups = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    return dict(groups)

def aggregate(rows, group_col, agg_col, func='sum'):
    """Aggregate a column by groups."""
    groups = group_by(rows, group_col)
    results = []
    for name, group in sorted(groups.items()):
        values = [float(r[agg_col]) for r in group if r[agg_col].strip()]
        if func == 'sum':
            agg = sum(values)
        elif func == 'avg':
            agg = sum(values) / len(values) if values else 0
        elif func == 'count':
            agg = len(values)
        elif func == 'min':
            agg = min(values) if values else 0
        elif func == 'max':
            agg = max(values) if values else 0
        results.append({group_col: name, f'{func}_{agg_col}': str(agg), 'count': str(len(group))})
    return results

# Example: sum revenue by category
summary = aggregate(data, 'category', 'revenue', 'sum')
write_csv(summary, 'summary.csv')
```

### Join Datasets

```python
def inner_join(left, right, on):
    """Inner join two datasets on a key column."""
    right_index = {}
    for r in right:
        key = r[on]
        if key not in right_index:
            right_index[key] = []
        right_index[key].append(r)

    results = []
    for lr in left:
        key = lr[on]
        if key in right_index:
            for rr in right_index[key]:
                merged = {**lr}
                for k, v in rr.items():
                    if k != on:
                        merged[k] = v
                results.append(merged)
    return results

def left_join(left, right, on):
    """Left join: keep all left rows, fill missing right with empty."""
    right_index = {}
    right_cols = set()
    for r in right:
        key = r[on]
        right_cols.update(r.keys())
        if key not in right_index:
            right_index[key] = []
        right_index[key].append(r)
    right_cols.discard(on)

    results = []
    for lr in left:
        key = lr[on]
        if key in right_index:
            for rr in right_index[key]:
                merged = {**lr}
                for k, v in rr.items():
                    if k != on:
                        merged[k] = v
                results.append(merged)
        else:
            merged = {**lr}
            for col in right_cols:
                merged[col] = ''
            results.append(merged)
    return results

# Example
orders = read_csv('orders.csv')
customers = read_csv('customers.csv')
joined = left_join(orders, customers, on='customer_id')
write_csv(joined, 'orders_with_customers.csv')
```

### Deduplicate

```python
def deduplicate(rows, key_cols=None):
    """Remove duplicate rows. If key_cols specified, dedupe by those columns only."""
    seen = set()
    unique = []
    for r in rows:
        if key_cols:
            key = tuple(r[c] for c in key_cols)
        else:
            key = tuple(sorted(r.items()))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique

# Deduplicate by email column
clean = deduplicate(data, key_cols=['email'])
```

## Format Conversion

### CSV to JSON

```python
import json, csv

with open('data.csv', newline='', encoding='utf-8') as f:
    rows = list(csv.DictReader(f))

# Array of objects
with open('data.json', 'w') as f:
    json.dump(rows, f, indent=2)

# JSON Lines (one object per line, streamable)
with open('data.jsonl', 'w') as f:
    for row in rows:
        f.write(json.dumps(row) + '\n')
```

### JSON to CSV

```python
import json, csv

with open('data.json') as f:
    rows = json.load(f)

with open('data.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
```

### JSON Lines to CSV

```python
import json, csv

rows = []
with open('data.jsonl') as f:
    for line in f:
        if line.strip():
            rows.append(json.loads(line))

with open('data.csv', 'w', newline='', encoding='utf-8') as f:
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    writer = csv.DictWriter(f, fieldnames=sorted(all_keys))
    writer.writeheader()
    writer.writerows(rows)
```

### TSV to CSV

```bash
tr '\t' ',' < data.tsv > data.csv
```

## Data Cleaning Patterns

### Fix common CSV issues

```python
def clean_csv(rows):
    """Clean common CSV data quality issues."""
    cleaned = []
    for r in rows:
        clean_row = {}
        for k, v in r.items():
            # Strip whitespace from keys and values
            k = k.strip()
            v = v.strip() if isinstance(v, str) else v
            # Normalize empty values
            if v in ('', 'N/A', 'n/a', 'NA', 'null', 'NULL', 'None', '-'):
                v = ''
            # Normalize boolean values
            if v.lower() in ('true', 'yes', '1', 'y'):
                v = 'true'
            elif v.lower() in ('false', 'no', '0', 'n'):
                v = 'false'
            clean_row[k] = v
        cleaned.append(clean_row)
    return cleaned
```

### Validate data types

```python
def validate_rows(rows, schema):
    """
    Validate rows against a schema.
    schema: dict of column_name -> 'int'|'float'|'date'|'email'|'str'
    Returns (valid_rows, error_rows)
    """
    import re
    valid, errors = [], []
    for i, r in enumerate(rows):
        errs = []
        for col, dtype in schema.items():
            val = r.get(col, '').strip()
            if not val:
                continue
            if dtype == 'int':
                try:
                    int(val)
                except ValueError:
                    errs.append(f"{col}: '{val}' not int")
            elif dtype == 'float':
                try:
                    float(val)
                except ValueError:
                    errs.append(f"{col}: '{val}' not float")
            elif dtype == 'email':
                if not re.match(r'^[^@]+@[^@]+\.[^@]+$', val):
                    errs.append(f"{col}: '{val}' not email")
            elif dtype == 'date':
                if not re.match(r'^\d{4}-\d{2}-\d{2}', val):
                    errs.append(f"{col}: '{val}' not YYYY-MM-DD")
        if errs:
            errors.append({'row': i + 2, 'errors': errs, 'data': r})
        else:
            valid.append(r)
    return valid, errors

# Usage
valid, bad = validate_rows(data, {'amount': 'float', 'email': 'email', 'date': 'date'})
print(f"Valid: {len(valid)}, Errors: {len(bad)}")
for e in bad[:5]:
    print(f"  Row {e['row']}: {e['errors']}")
```

## Generating Reports

### Summary report as Markdown

```python
def generate_report(data, title, group_col, value_col):
    """Generate a Markdown summary report."""
    lines = [f"# {title}", f"", f"**Total rows**: {len(data)}", ""]

    # Group summary
    groups = group_by(data, group_col)
    lines.append(f"## By {group_col}")
    lines.append("")
    lines.append(f"| {group_col} | Count | Sum | Avg | Min | Max |")
    lines.append("|---|---|---|---|---|---|")

    for name in sorted(groups):
        vals = [float(r[value_col]) for r in groups[name] if r[value_col].strip()]
        if vals:
            lines.append(f"| {name} | {len(vals)} | {sum(vals):.2f} | {sum(vals)/len(vals):.2f} | {min(vals):.2f} | {max(vals):.2f} |")

    lines.append("")
    lines.append(f"*Generated from {len(data)} rows*")
    return '\n'.join(lines)

report = generate_report(data, "Sales Summary", "category", "revenue")
with open('report.md', 'w') as f:
    f.write(report)
```

## Large File Handling

For files too large to load into memory at once:

```python
def stream_process(input_path, output_path, transform_fn, delimiter=','):
    """Process a CSV row-by-row without loading entire file."""
    with open(input_path, newline='', encoding='utf-8') as fin, \
         open(output_path, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin, delimiter=delimiter)
        writer = None
        for row in reader:
            result = transform_fn(row)
            if result is None:
                continue  # Skip row
            if writer is None:
                writer = csv.DictWriter(fout, fieldnames=result.keys(), delimiter=delimiter)
                writer.writeheader()
            writer.writerow(result)

# Example: filter and transform in streaming fashion
def process_row(row):
    if float(row.get('amount', 0) or 0) < 10:
        return None  # Skip small amounts
    row['amount_usd'] = str(float(row['amount']) * 1.0)  # Add computed field
    return row

stream_process('big_file.csv', 'output.csv', process_row)
```

## Tips

- Always check encoding: `file -i data.csv` or open with `encoding='utf-8-sig'` for BOM files
- For Excel exports with commas in values, the CSV module handles quoting automatically
- Use `json.dumps(ensure_ascii=False)` for international characters
- Pipe-delimited files: use `delimiter='|'` in csv.reader/writer
- For very large aggregations, consider `sqlite3` which Python includes:
  ```bash
  sqlite3 :memory: ".mode csv" ".import data.csv t" "SELECT category, SUM(amount) FROM t GROUP BY category;"
  ```
