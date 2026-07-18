
# OpenYida Publish Guard

## Purpose

Use this skill whenever an existing OpenYida custom page is about to be edited or published from local source.

The Yida designer is also a source of truth. Users can make small live edits in the designer, such as deleting a query button, while the local `.oyd.jsx` file still contains older code. Publishing that stale local file will silently restore the deleted UI or roll back other live changes.

This skill adds a mandatory online-state check before publishing so agents merge requested changes onto the current live page instead of overwriting it.

## When To Use

Use this skill for:

- Any `openyida publish` to an existing custom page.
- Any request that mentions a `pageDesigner` URL, display page, custom page JSX, or `formUuid`.
- Any targeted page edit where the user says "only change X" or "do not touch other code".
- Any situation where the user or another editor may have changed the page in the Yida designer after the local source was created.

Do not use this skill for creating a brand-new page that has no existing live schema.

## Required Workflow

1. Confirm the target `appType`, `formUuid`, and local source path.
2. Check the OpenYida environment and login state:

```bash
openyida env --json
openyida login --check-only --json
```

3. Fetch the live schema before editing or publishing:

```bash
openyida get-schema <appType> <formUuid> --json
```

If the schema needs to be inspected from a file, save stdout with the agent's structured file write tool to `<projectRoot>/.cache/openyida/publish-guard/live-<formUuid>.json`. From the workspace root that path is typically `project/.cache/openyida/publish-guard/live-<formUuid>.json`. Do not use shell redirection and do not commit fetched live schema files.

4. Inspect the fetched live page for current code and configuration. For custom display pages, check at least:

- `pages[].componentsTree[].methods.__initMethods__.source`
- `pages[].componentsTree[].methods.__initMethods__.compiled`
- JSX render source under `componentsTree` when present
- `dataSource.online` and existing connector or page-level data source definitions
- Any visible component tree change that could represent a designer edit

5. Compare the live source with the local source before publishing.

- If the local source is older or missing a user-visible live change, merge the requested change into the live/current behavior.
- If the user explicitly says "only change X", verify the final diff only touches X and strictly required helper or style code.
- If live and local differ in unrelated user-visible areas, pause and ask whether to preserve the live changes unless the preservation is obvious.
- Exception: only when the user explicitly says "use local source" may you skip live-source merging. You must still run env check -> check-page -> compile -> publish --health-check. Skipping live-source merging does not mean skipping safety checks.

6. Only after the merge is scoped and reviewed, run:

```bash
openyida check-page <source>
openyida compile <source>
openyida publish <source> <appType> <formUuid> --health-check
```

## Minimal Diff Discipline

- Do not reformat the file.
- Do not regenerate the entire page unless explicitly requested.
- Do not replace a page with an older local copy.
- Keep deleted live UI deleted unless the user asks to restore it.
- Preserve existing page data sources; confirm publish output reports retained data sources.
- Search the final local source for accidentally restored labels or controls from the bug report, such as `查询`, `queryButton`, or other recently removed UI.

## Incident That Motivated This Skill

While editing a Junjie Yida ranking page, a user had deleted the query button directly in the Yida designer. A later targeted change was published from an older local `.oyd.jsx` file without first checking the live designer schema. The stale publish restored the deleted query button and overwrote the user's immediate designer edit.

The fix was to restore the deleted UI state and add this guardrail: before future OpenYida page edits or publishes, fetch the live schema, compare the current live source with local source, and merge the requested change onto the latest live behavior.

## Recovery If An Overwrite Happens

1. Acknowledge the overwrite plainly.
2. Restore the overwritten live change immediately, without broad refactors.
3. Re-run `openyida check-page`, `openyida compile`, and `openyida publish --health-check`.
4. Update this skill if the failure mode is not already covered.
