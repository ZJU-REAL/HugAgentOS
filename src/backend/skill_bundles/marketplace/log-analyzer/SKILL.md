---
name: log-analyzer
description: Parse, search, and analyze application logs across formats. Use when debugging from log files, setting up structured logging, analyzing error patterns, correlating events across services, parsing stack traces, or monitoring log output in real time.
metadata: {"clawdbot":{"emoji":"📋","requires":{"anyBins":["grep","awk","jq","python3"]},"os":["linux","darwin","win32"]}}
---

# Log Analyzer

Parse, search, and debug from application logs. Covers plain text logs, structured JSON logs, stack traces, multi-service correlation, and real-time monitoring.

## When to Use

- Debugging application errors from log files
- Searching logs for specific patterns, errors, or request IDs
- Parsing and analyzing stack traces
- Setting up structured logging (JSON) in applications
- Correlating events across multiple services or log files
- Monitoring logs in real time during development
- Generating error frequency reports or summaries

## Quick Search Patterns

### Find errors and exceptions

```bash
# All errors in a log file
grep -i 'error\|exception\|fatal\|panic\|fail' app.log

# Errors with 3 lines of context
grep -i -C 3 'error\|exception' app.log

# Errors in the last hour (ISO timestamps)
HOUR_AGO=$(date -u -d '1 hour ago' '+%Y-%m-%dT%H:%M' 2>/dev/null || date -u -v-1H '+%Y-%m-%dT%H:%M')
awk -v t="$HOUR_AGO" '$0 ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T/ && $1 >= t' app.log | grep -i 'error'

# Count errors by type
grep -oP '(?:Error|Exception): \K[^\n]+' app.log | sort | uniq -c | sort -rn | head -20

# HTTP 5xx errors from access logs
awk '$9 >= 500' access.log
```

### Search by request or correlation ID

```bash
# Trace a single request across log entries
grep 'req-abc123' app.log

# Across multiple files
grep -r 'req-abc123' /var/log/myapp/

# Across multiple services (with filename prefix)
grep -rH 'correlation-id-xyz' /var/log/service-a/ /var/log/service-b/ /var/log/service-c/
```

### Time-range filtering

```bash
# Between two timestamps (ISO format)
awk '$0 >= "2026-02-03T10:00" && $0 <= "2026-02-03T11:00"' app.log

# Last N lines (tail)
tail -1000 app.log | grep -i error

# Since a specific time (GNU date)
awk -v start="$(date -d '30 minutes ago' '+%Y-%m-%dT%H:%M')" '$1 >= start' app.log
```

## JSON / Structured Logs

### Parse with jq

```bash
# Pretty-print JSON logs
cat app.log | jq '.'

# Filter by level
cat app.log | jq 'select(.level == "error")'

# Filter by time range
cat app.log | jq 'select(.timestamp >= "2026-02-03T10:00:00Z")'

# Extract specific fields
cat app.log | jq -r '[.timestamp, .level, .message] | @tsv'

# Count by level
cat app.log | jq -r '.level' | sort | uniq -c | sort -rn

# Filter by nested field
cat app.log | jq 'select(.context.userId == "user-123")'

# Group errors by message
cat app.log | jq -r 'select(.level == "error") | .message' | sort | uniq -c | sort -rn

# Extract request duration stats
cat app.log | jq -r 'select(.duration != null) | .duration' | awk '{sum+=$1; count++; if($1>max)max=$1} END {print "count="count, "avg="sum/count, "max="max}'
```

### Parse mixed-format logs (JSON lines mixed with plain text)

```bash
# Extract only valid JSON lines
while IFS= read -r line; do
  echo "$line" | jq '.' 2>/dev/null && continue
done < app.log

# Or with grep for lines starting with {
grep '^\s*{' app.log | jq '.'
```

## Stack Trace Analysis

### Extract and deduplicate stack traces

```bash
# Extract Java/Kotlin stack traces (starts with Exception/Error, followed by \tat lines)
awk '/Exception|Error/{trace=$0; while(getline && /^\t/) trace=trace"\n"$0; print trace"\n---"}' app.log

# Extract Python tracebacks
awk '/^Traceback/{p=1} p{print} /^[A-Za-z].*Error/{if(p) print "---"; p=0}' app.log

# Extract Node.js stack traces (Error + indented "at" lines)
awk '/Error:/{trace=$0; while(getline && /^    at /) trace=trace"\n"$0; print trace"\n---"}' app.log

# Deduplicate: group by root cause (first line of trace)
awk '/Exception|Error:/{cause=$0} /^\tat|^    at /{next} cause{print cause; cause=""}' app.log | sort | uniq -c | sort -rn
```

### Python traceback parser

```python
#!/usr/bin/env python3
"""Parse Python tracebacks from log files and group by root cause."""
import sys
import re
from collections import Counter

def extract_tracebacks(filepath):
    tracebacks = []
    current = []
    in_trace = False

    with open(filepath) as f:
        for line in f:
            if line.startswith('Traceback (most recent call last):'):
                in_trace = True
                current = [line.rstrip()]
            elif in_trace:
                current.append(line.rstrip())
                # Exception line ends the traceback
                if re.match(r'^[A-Za-z]\w*(Error|Exception|Warning)', line):
                    tracebacks.append('\n'.join(current))
                    in_trace = False
                    current = []
    return tracebacks

if __name__ == '__main__':
    filepath = sys.argv[1] if len(sys.argv) > 1 else '/dev/stdin'
    traces = extract_tracebacks(filepath)

    # Group by exception type and message
    causes = Counter()
    for trace in traces:
        lines = trace.split('\n')
        cause = lines[-1] if lines else 'Unknown'
        causes[cause] += 1

    print(f"Found {len(traces)} tracebacks, {len(causes)} unique causes:\n")
    for cause, count in causes.most_common(20):
        print(f"  {count:4d}x  {cause}")
```

## Real-Time Monitoring

### Tail and filter

```bash
# Follow log file, highlight errors in red
tail -f app.log | grep --color=always -i 'error\|warn\|$'

# Follow and filter to errors only
tail -f app.log | grep --line-buffered -i 'error\|exception'

# Follow JSON logs, pretty-print errors
tail -f app.log | while IFS= read -r line; do
  level=$(echo "$line" | jq -r '.level // empty' 2>/dev/null)
  if [ "$level" = "error" ] || [ "$level" = "fatal" ]; then
    echo "$line" | jq '.'
  fi
done

# Follow multiple files
tail -f /var/log/service-a/app.log /var/log/service-b/app.log

# Follow with timestamps (useful when log doesn't include them)
tail -f app.log | while IFS= read -r line; do
  echo "$(date '+%H:%M:%S') $line"
done
```

### Watch for specific patterns and alert

```bash
# Beep on error (terminal bell)
tail -f app.log | grep --line-buffered -i 'error' | while read line; do
  echo -e "\a$line"
done

# Count errors per minute
tail -f app.log | grep --line-buffered -i 'error' | while read line; do
  echo "$(date '+%Y-%m-%d %H:%M') ERROR"
done | uniq -c
```

## Log Format Parsing

### Common access log (Apache/Nginx)

```bash
# Parse fields: IP, date, method, path, status, size
awk '{print $1, $9, $7}' access.log

# Top IPs by request count
awk '{print $1}' access.log | sort | uniq -c | sort -rn | head -20

# Top paths by request count
awk '{print $7}' access.log | sort | uniq -c | sort -rn | head -20

# Slow requests (response time in last field, microseconds)
awk '{if ($NF > 1000000) print $0}' access.log

# Requests per minute
awk '{split($4,a,":"); print a[1]":"a[2]":"a[3]}' access.log | uniq -c

# Status code distribution
awk '{print $9}' access.log | sort | uniq -c | sort -rn

# 4xx and 5xx with paths
awk '$9 >= 400 {print $9, $7}' access.log | sort | uniq -c | sort -rn | head -20
```

### Custom delimited logs

```bash
# Pipe-delimited: timestamp|level|service|message
awk -F'|' '{print $2, $3, $4}' app.log

# Tab-delimited
awk -F'\t' '$2 == "ERROR" {print $1, $4}' app.log

# CSV logs
python3 -c "
import csv, sys
with open(sys.argv[1]) as f:
    for row in csv.DictReader(f):
        if row.get('level') == 'error':
            print(f\"{row['timestamp']} {row['message']}\")
" app.csv
```

## Setting Up Structured Logging

### Node.js (pino — fast JSON logger)

```javascript
// npm install pino
const pino = require('pino');
const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  // Add standard fields to every log line
  base: { service: 'my-api', version: '1.2.0' },
});

// Usage
logger.info({ userId: 'u123', action: 'login' }, 'User logged in');
logger.error({ err, requestId: req.id }, 'Request failed');

// Output: {"level":30,"time":1706900000000,"service":"my-api","userId":"u123","action":"login","msg":"User logged in"}

// Child logger with bound context
const reqLogger = logger.child({ requestId: req.id, userId: req.user?.id });
reqLogger.info('Processing order');
reqLogger.error({ err }, 'Order failed');
```

### Python (structlog)

```python
# pip install structlog
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)
logger = structlog.get_logger(service="my-api")

# Usage
logger.info("user_login", user_id="u123", ip="1.2.3.4")
logger.error("request_failed", request_id="req-abc", error=str(e))

# Output: {"event":"user_login","user_id":"u123","ip":"1.2.3.4","level":"info","timestamp":"2026-02-03T12:00:00Z","service":"my-api"}
```

### Go (zerolog)

```go
import (
    "os"
    "github.com/rs/zerolog"
    "github.com/rs/zerolog/log"
)

func init() {
    zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
    log.Logger = zerolog.New(os.Stdout).With().
        Timestamp().
        Str("service", "my-api").
        Logger()
}

// Usage
log.Info().Str("userId", "u123").Msg("User logged in")
log.Error().Err(err).Str("requestId", reqID).Msg("Request failed")
```

## Error Pattern Reports

### Generate error frequency report

```bash
#!/bin/bash
# error-report.sh - Summarize errors from a log file
LOG="${1:?Usage: error-report.sh <logfile>}"

echo "=== Error Report: $(basename "$LOG") ==="
echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

total=$(wc -l < "$LOG")
errors=$(grep -ci 'error\|exception\|fatal' "$LOG")
warns=$(grep -ci 'warn' "$LOG")

echo "Total lines:  $total"
echo "Errors:       $errors"
echo "Warnings:     $warns"
echo ""

echo "--- Top 15 Error Messages ---"
grep -i 'error\|exception' "$LOG" | \
  sed 's/^[0-9TZ:.+\-]* //' | \
  sed 's/\b[0-9a-f]\{8,\}\b/ID/g' | \
  sed 's/[0-9]\{1,\}/N/g' | \
  sort | uniq -c | sort -rn | head -15
echo ""

echo "--- Errors Per Hour ---"
grep -i 'error\|exception' "$LOG" | \
  grep -oP '\d{4}-\d{2}-\d{2}T\d{2}' | \
  sort | uniq -c
echo ""

echo "--- First Occurrence of Each Error Type ---"
grep -i 'error\|exception' "$LOG" | \
  sed 's/^[0-9TZ:.+\-]* //' | \
  sort -u | head -10
```

### JSON log error report with Python

```python
#!/usr/bin/env python3
"""Generate error summary from JSON log files."""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime

def analyze_logs(filepath):
    errors = []
    levels = Counter()
    errors_by_hour = defaultdict(int)

    with open(filepath) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue

            level = entry.get('level', entry.get('severity', '')).lower()
            levels[level] += 1

            if level in ('error', 'fatal', 'critical'):
                msg = entry.get('message', entry.get('msg', entry.get('event', 'unknown')))
                ts = entry.get('timestamp', entry.get('time', ''))
                errors.append({'message': msg, 'timestamp': ts, 'entry': entry})

                # Group by hour
                try:
                    hour = ts[:13]  # "2026-02-03T12"
                    errors_by_hour[hour] += 1
                except (TypeError, IndexError):
                    pass

    # Group errors by message
    error_counts = Counter(e['message'] for e in errors)

    print(f"=== Log Analysis: {filepath} ===\n")
    print("Level distribution:")
    for level, count in levels.most_common():
        print(f"  {level:10s}  {count}")

    print(f"\nTotal errors: {len(errors)}")
    print(f"Unique error messages: {len(error_counts)}\n")

    print("Top 15 errors:")
    for msg, count in error_counts.most_common(15):
        print(f"  {count:4d}x  {msg[:100]}")

    if errors_by_hour:
        print("\nErrors by hour:")
        for hour in sorted(errors_by_hour):
            bar = '#' * min(errors_by_hour[hour], 50)
            print(f"  {hour}  {errors_by_hour[hour]:4d}  {bar}")

if __name__ == '__main__':
    analyze_logs(sys.argv[1])
```

## Multi-Service Log Correlation

### Merge and sort logs from multiple services

```bash
# Merge multiple log files, sort by timestamp
sort -m -t'T' -k1,1 service-a.log service-b.log service-c.log > merged.log

# If files aren't individually sorted, use full sort
sort -t'T' -k1,1 service-*.log > merged.log

# Merge JSON logs, add source field
for f in service-*.log; do
  service=$(basename "$f" .log)
  jq --arg svc "$service" '. + {source: $svc}' "$f"
done | jq -s 'sort_by(.timestamp)[]'
```

### Trace a request across services

```bash
# Find all log entries for a correlation/request ID across all services
REQUEST_ID="req-abc-123"
grep -rH "$REQUEST_ID" /var/log/services/ | sort -t: -k2

# With JSON logs
for f in /var/log/services/*.log; do
  jq --arg rid "$REQUEST_ID" 'select(.requestId == $rid or .correlationId == $rid)' "$f" 2>/dev/null
done | jq -s 'sort_by(.timestamp)[]'
```

## Log Rotation and Large Files

### Working with rotated/compressed logs

```bash
# Search across rotated logs (including .gz)
zgrep -i 'error' /var/log/app.log*

# Search today's and yesterday's logs
zgrep -i 'error' /var/log/app.log /var/log/app.log.1

# Decompress, filter, and recompress
zcat app.log.3.gz | grep 'ERROR' | gzip > errors-day3.gz
```

### Sampling large files

```bash
# Random sample of 1000 lines
shuf -n 1000 huge.log > sample.log

# Every 100th line
awk 'NR % 100 == 0' huge.log > sample.log

# First and last 500 lines
{ head -500 huge.log; echo "--- TRUNCATED ---"; tail -500 huge.log; } > excerpt.log
```

## Tips

- Always search for a **request ID or correlation ID** first — it narrows the haystack faster than timestamps or error messages.
- Use `--line-buffered` with `grep` when piping from `tail -f` so output isn't delayed by buffering.
- Normalize IDs and numbers before grouping errors (`sed 's/[0-9a-f]\{8,\}/ID/g'`) to collapse duplicates that differ only by ID.
- For JSON logs, `jq` is indispensable. Install it if it's not available: `apt install jq` / `brew install jq`.
- Structured logging (JSON) is always worth the setup cost. It makes every analysis task easier: filtering, grouping, correlation, and alerting all become `jq` one-liners.
- When debugging a production issue: get the **time window** and **affected user/request ID** first, then filter logs to that scope before reading anything.
- `awk` is faster than `grep | sort | uniq -c` pipelines for large files. Use it for counting and aggregation.
