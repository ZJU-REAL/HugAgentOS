---
name: firecrawl
description: |
  Search, scrape, and interact with the web via the Firecrawl CLI. Use this skill whenever the user wants to search the web, find articles, research a topic, look something up online, scrape a webpage, grab content from a URL, get data from a website, crawl documentation, download a site, or interact with pages that need clicks or logins. Also use when they say "fetch this page", "pull the content from", "get the page at https://", or reference external websites. This provides real-time web search with full page content and interact capabilities — beyond what is available natively. Do NOT trigger for local file operations, git commands, deployments, or code editing tasks.
allowed-tools:
  - Bash(firecrawl *)
  - Bash(npx firecrawl *)
---

# Firecrawl CLI

Search, scrape, and interact with the web. Returns clean markdown optimized for LLM context windows.

Run `firecrawl --help` or `firecrawl <command> --help` for full option details.

## Prerequisites (this platform)

The `firecrawl` CLI is **pre-installed in the sandbox** — you do not install it. Credentials
are injected by the platform from the admin config (`FIRECRAWL_API_KEY` for the cloud version,
or `FIRECRAWL_API_URL` for a self-hosted instance). Just run `firecrawl <command>` directly.

Check status with:

```bash
firecrawl --status
```

Expected output when configured:

```
  🔥 firecrawl cli

  ● Authenticated via FIRECRAWL_API_KEY
  Concurrency: 0/100 jobs (parallel scrape limit)
  Credits: 500,000 remaining
```

- **Concurrency**: Max parallel jobs. Run parallel operations up to this limit.
- **Credits**: Remaining API credits. Each operation consumes credits.

If `firecrawl --status` reports **not authenticated**, the deployment has no Firecrawl
credentials yet. Do not try to log in or install anything — tell the user the administrator
must open the **Firecrawl plugin detail in the plugin library (插件库)** in the admin console
and fill in the API Key (cloud) or the self-hosted instance URL; it takes effect within ~30s.
Regular users cannot set this themselves and need to contact an administrator.

Before doing real work, verify the setup with one small request:

```bash
mkdir -p .firecrawl
firecrawl scrape "https://firecrawl.dev" -o .firecrawl/install-check.md
```

## Workflow

Follow this escalation pattern:

1. **Search** - No specific URL yet. Find pages, answer questions, discover sources.
2. **Scrape** - Have a URL. Extract its content directly.
3. **Map + Scrape** - Large site or need a specific subpage. Use `map --search` to find the right URL, then scrape it.
4. **Crawl** - Need bulk content from an entire site section (e.g., all /docs/).
5. **Monitor** - Need recurring checks or ongoing alerts. Prefer setting a monitor with `--page` plus `--goal` instead of doing repeated one-off scrapes.
6. **Interact** - Scrape first, then interact with the page (pagination, modals, form submissions, multi-step navigation).

| Need                        | Command               | When                                                      |
| --------------------------- | --------------------- | --------------------------------------------------------- |
| Find pages on a topic       | `search`              | No specific URL yet                                       |
| Get a page's content        | `scrape`              | Have a URL, page is static or JS-rendered                 |
| Find URLs within a site     | `map`                 | Need to locate a specific subpage                         |
| Bulk extract a site section | `crawl`               | Need many pages (e.g., all /docs/)                        |
| AI-powered data extraction  | `agent`               | Need structured data from complex sites                   |
| Interact with a page        | `scrape` + `interact` | Content requires clicks, form fills, pagination, or login |
| Download a site to files    | `download`            | Save an entire site as local files                        |
| Parse a local file          | `parse`               | File on disk (PDF, DOCX, XLSX, etc.) — not a URL          |
| Watch pages for changes     | `monitor`             | Schedule recurring scrapes/crawls, diff against snapshots |

For detailed command reference, run `firecrawl <command> --help`.

**Scrape vs interact:**

- Use `scrape` first. It handles static pages and JS-rendered SPAs.
- Use `scrape` + `interact` when you need to interact with a page, such as clicking buttons, filling out forms, navigating through a complex site, infinite scroll, or when scrape fails to grab all the content you need.
- Never use interact for web searches - use `search` instead.

**Monitor:** Schedule recurring scrapes or crawls and diff each result against the last retained snapshot. Bias toward `monitor` when the user's goal is ongoing change detection, alerting, or repeated checks over time. Each monitor should include a short `goal` describing what changes matter, and each check labels pages as `same`, `new`, `changed`, `removed`, or `error`, with webhook and email notification options.

## Output & Organization

Unless the user specifies to return content in context, write results to `.firecrawl/` with `-o`
(under the sandbox working directory `/workspace`). Always quote URLs - the shell interprets `?`
and `&` as special characters.

```bash
firecrawl search "react hooks" -o .firecrawl/search-react-hooks.json --json
firecrawl scrape "<url>" -o .firecrawl/page.md
```

Naming conventions:

```
.firecrawl/search-{query}.json
.firecrawl/search-{query}-scraped.json
.firecrawl/{site}-{path}.md
```

Never read entire output files at once. Use `grep`, `head`, or incremental reads:

```bash
wc -l .firecrawl/file.md && head -50 .firecrawl/file.md
grep -n "keyword" .firecrawl/file.md
```

Single format outputs raw content. Multiple formats (e.g., `--format markdown,links`) output JSON.

## Working with Results

```bash
# Extract URLs from search
jq -r '.data.web[].url' .firecrawl/search.json

# Get titles and URLs
jq -r '.data.web[] | "\(.title): \(.url)"' .firecrawl/search.json
```

## Avoid redundant fetches

- `search --scrape` already fetches full page content. Don't re-scrape those URLs.
- Check `.firecrawl/` for existing data before fetching again.

## Parallelization

Run independent operations in parallel. Check `firecrawl --status` for concurrency limit:

```bash
firecrawl scrape "<url-1>" -o .firecrawl/1.md &
firecrawl scrape "<url-2>" -o .firecrawl/2.md &
firecrawl scrape "<url-3>" -o .firecrawl/3.md &
wait
```

## Credit Usage

```bash
firecrawl credit-usage
firecrawl credit-usage --json --pretty -o .firecrawl/credits.json
```

## Companion skills

This plugin installs one skill per capability — the model picks them by description:
`firecrawl-search`, `firecrawl-scrape`, `firecrawl-map`, `firecrawl-crawl`, `firecrawl-agent`,
`firecrawl-interact`, `firecrawl-download`, `firecrawl-parse`, `firecrawl-monitor`.
