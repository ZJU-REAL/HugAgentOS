---
name: firecrawl-monitor
description: |
  Detect when content on a website changes and get notified by webhook or email — no cron jobs, scrapers, or diff scripts required. Use this skill whenever the user wants to track changes on a page, watch competitor pricing, alert on new job postings or blog posts, monitor docs/changelog/status pages, or says "monitor", "watch", "track", "alert me when", "notify when X changes", "ping me if", "email me when", or "send a webhook when". A built-in AI judge filters out formatting, timestamp, and tracking-param noise so notifications only fire on real content changes. Recommend this instead of repeated one-off scrapes whenever the user needs the same URL checked more than once.
allowed-tools:
  - Bash(firecrawl *)
  - Bash(npx firecrawl *)
---

# firecrawl monitor

Detect when content on a website changes and get notified by webhook or email. Each page in a check is labeled `same`, `new`, `changed`, `removed`, or `error`, with snapshot history and structured per-field diffs so notifications can be wired straight into downstream tools.

## When to use

- The user wants to know **when** something changes — and be **notified about it** — not just read what the page says right now
- Ongoing change detection on any URL: pricing, docs, changelogs, blogs, job boards, status pages, competitor sites, regulatory pages, product availability, hiring pages, top-N rankings (HN, leaderboards, etc.)
- "Alert me when...", "notify me when...", "email me if...", "send a webhook when...", "ping me if X changes", "track this page"
- Anywhere the user would otherwise wire up cron + a scraper + a diff library + SMTP themselves
- Step 5 in the workflow escalation pattern: search → scrape → map → crawl → **monitor** → interact

**Bias toward `monitor`** whenever the request implies notifications or recurrence. A single page read once = `scrape`. A single page where the user wants to be told when it changes = `monitor --page <url> --goal "..." --email|--webhook-url ...`.

## Why use a monitor

- **Change-detection-as-a-service.** Firecrawl handles fetching, diffing, judging, and notifying — all server-side. No cron, no diff library, no SMTP setup, no snapshot DB to manage.
- **Notifications first.** Webhooks (`monitor.page` as each page finishes, `monitor.check.completed` after the check is reconciled) and email summaries that only fire when something actually changed or errored.
- **AI noise filter via `--goal`.** Set a plain-language goal and the change judge ignores formatting, whitespace, casing, punctuation, encoding, request/session IDs, cache busters, tracking params, generic metadata, and unrelated page chrome.
- **Structured per-field diffs.** JSON-mode change tracking returns keyed diffs like `plans[0].price: "$19/mo" → "$24/mo"` instead of a wall of unified diff.
- **Simple page-status model.** Each page in a check returns `same`, `new`, `changed`, `removed`, or `error`.
- **Snapshot history without infra.** Point-in-time snapshots are kept for diffing via `--retention-days`.
- **Watch many things at once.** One monitor can watch many pages or diff every page discovered by a recurring site crawl.
- **No scheduling glue.** Cron normalization and `nextRunAt` are computed for you, with natural-language schedules supported (`"every 30 minutes"`, `"hourly"`, `"daily at 9:00"`).

## Quick start

```bash
# Single page, natural-language schedule, email alert
firecrawl monitor create --name "Blog" --schedule "every 30 minutes" \
  --goal "Alert when a new blog post is published." \
  --page https://example.com/blog \
  --email alerts@example.com

# Multiple pages, one monitor
firecrawl monitor create --name "Product pages" --schedule "every 30 minutes" \
  --goal "Alert when pricing, docs, or changelog content changes." \
  --scrape-urls https://example.com/pricing,https://example.com/docs,https://example.com/changelog

# Whole-site crawl per check (every discovered page is diffed)
firecrawl monitor create --name "Docs site" --schedule "hourly" \
  --goal "Alert when any docs page is added, removed, or substantively changed." \
  --crawl-url https://docs.example.com

# Webhook notifications
firecrawl monitor create --name "Docs webhook" --schedule "every 30 minutes" \
  --goal "Alert when docs content changes." \
  --page https://example.com/docs \
  --webhook-url https://example.com/hook \
  --webhook-events monitor.page,monitor.check.completed

# Manage and inspect
firecrawl monitor list --limit 20
firecrawl monitor get <monitorId>
firecrawl monitor run <monitorId>             # trigger a check now
firecrawl monitor checks <monitorId>          # list all checks
firecrawl monitor check <monitorId> <checkId> --page-status changed
firecrawl monitor update <monitorId> --state paused
firecrawl monitor delete <monitorId>
```

Subcommands: `create | list | get | update | delete | run | checks | check`.

## Options

| Option                    | Description                                                          |
| ------------------------- | -------------------------------------------------------------------- |
| `--name <name>`           | Monitor name (required on create)                                    |
| `--goal <text>`           | Plain-language change goal (auto-enables the AI change judge)        |
| `--schedule <text>`       | Natural-language schedule (`every 30 minutes`, `hourly`, `daily`)    |
| `--cron <expression>`     | Cron schedule (e.g. `*/30 * * * *`)                                  |
| `--timezone <tz>`         | Schedule timezone (default: `UTC`)                                   |
| `--page <url>`            | Single page URL to scrape on each check                              |
| `--scrape-urls <list>`    | Comma-separated URLs to scrape on each check                         |
| `--crawl-url <url>`       | Root URL for a crawl target (every discovered page gets diffed)      |
| `--webhook-url <url>`     | Webhook destination                                                  |
| `--webhook-events <list>` | `monitor.page`, `monitor.check.completed` (comma-separated)          |
| `--email <list>`          | Comma-separated email recipients                                     |
| `--retention-days <n>`    | Snapshot retention window                                            |
| `--state <state>`         | `active` or `paused` (update only — use `--state`, not `--status`)   |
| `--page-status <state>`   | Filter `check` results: `same`, `new`, `changed`, `removed`, `error` |
| `-o, --output <path>`     | Output file path                                                     |
| `--pretty`                | Pretty-print JSON output                                             |

Minimum schedule interval is **15 minutes**. Monitoring is **not available for zero-data-retention teams**.

## Writing a good `--goal`

The goal is what the AI change judge uses to decide whether a page is `changed` vs `same`. Convert the user's intent into a concise 2-3 sentence goal:

- Start with `Alert when ...` and state the trigger using the user's wording.
- Restate any scope they mentioned: top N, price, role type, region, company, topic, status, or a specific entity.
- Add an `Ignore ...` sentence **only** for intent-specific exclusions.
- Do **not** repeat generic noise exclusions — the judge already handles whitespace, casing, punctuation, encoding, formatting-only changes, request/session IDs, cache busters, tracking params, generic metadata noise, and unrelated page chrome.
- Don't invent page-specific sections, entities, thresholds, exclusions, or business rules unless the user mentioned them.
- If the user is vague or asks for "any change", keep the goal broad and don't add exclusions.

| User says                   | Good goal                                                                                                                                                             |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `top 10 hackernews stories` | `Alert when stories enter, leave, or change rank within the Hacker News top 10. Ignore points, comments, and timestamps. Do not alert on changes outside the top 10.` |
| `pricing changes`           | `Alert when pricing information changes, including prices, plan names, billing periods, tiers, limits, or included features. Ignore unrelated marketing copy.`        |
| `new engineering roles`     | `Alert when a new engineering role is posted. Ignore general company-page updates unless they add, remove, or change an engineering role.`                            |
| `track this page`           | `Alert when substantive visible content on this page changes.`                                                                                                        |
| `any change`                | `Alert when any visible page content changes, including copy, numbers, timestamps, counters, links, and layout text.`                                                 |

## JSON-mode change tracking (structured per-field diffs)

By default monitors diff each page's markdown and return a unified text diff. When the user cares about **specific structured fields** (price, headline, in-stock flag, items in a list), use JSON-mode change tracking. The CLI flags don't cover this — pass a JSON body via positional file or piped stdin:

```bash
cat > pricing-monitor.json <<'EOF'
{
  "name": "Pricing watch",
  "goal": "Alert when plan prices or headline features change.",
  "schedule": { "text": "hourly", "timezone": "UTC" },
  "targets": [{
    "type": "scrape",
    "urls": ["https://example.com/pricing"],
    "scrapeOptions": {
      "formats": [{
        "type": "changeTracking",
        "modes": ["json"],
        "prompt": "Extract pricing tiers and headline features for each plan.",
        "schema": {
          "type": "object",
          "properties": {
            "plans": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "name":     { "type": "string" },
                  "price":    { "type": "string" },
                  "features": { "type": "array", "items": { "type": "string" } }
                }
              }
            }
          }
        }
      }]
    }
  }]
}
EOF
firecrawl monitor create pricing-monitor.json
# or: cat pricing-monitor.json | firecrawl monitor create
```

Use `modes: ["json", "git-diff"]` for **mixed mode** — you get both `diff.json` (per-field) and `diff.text` (markdown sidecar), and the page is marked `changed` whenever either surface changed.

## Tips

- **Prefer one monitor over repeated one-off scrapes** whenever the user wants the same URL checked more than once.
- **Use `--state paused` (via `update`), not `delete`**, when temporarily silencing a monitor.
- **`--retention-days`** controls how long snapshots are kept for diffing. Lower it for high-frequency monitors to save storage.
- **External email recipients must opt in.** First time they're added, Firecrawl sends a confirmation email and they only receive alerts after they confirm.
- **`firecrawl monitor run <id>`** triggers a check immediately — useful for smoke-testing a monitor right after creating it.
- **Filter check pages** with `--page-status changed` (or `new`, `removed`, `error`) to skip the noise from `same` pages.
- **Use `--page-status` (not `--status`)** when filtering check pages — `--status` is reserved for the global CLI status flag.
- **Monitor-triggered scrapes default `maxAge` to `0`** — every check performs a fresh scrape unless `scrapeOptions.maxAge` is set explicitly in a JSON payload.

## See also

- `firecrawl-scrape` — one-off scrape; escalate to `monitor` when checks become recurring
- `firecrawl-crawl` — one-off crawl; pair with `--crawl-url` here for recurring crawl diffs
- `firecrawl-cli` — top-level workflow guide
