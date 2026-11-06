# HugAgentOS Community Edition engineering instructions

This is the public, generated Community Edition (CE) repository.
Read README.md, CONTRIBUTING.md, REVIEW.md and any more specific AGENTS.md before changing affected code.

## Contribution and local context

- Check Git status and the relevant diff; preserve unrelated work.
- Follow CONTRIBUTING.md: documentation, examples and repository metadata may be proposed here.
  Generated application changes normally belong upstream and return through CE derivation.
- Never add enterprise-only modules, credentials, private server details, local overrides or operations runbooks.
- Keep local instructions in ignored `AGENTS.override.md` and `.agent-local/`; never force-add them.
- Local edits do not authorize merges, deployments, real-data migrations or releases.

## Verification and shared understanding

Use the existing test/build commands for the changed module and report actual results and limits.
CE must run independently: check missing enterprise imports, advertised but unavailable capabilities,
offline desktop dependencies, and local versus cloud execution boundaries.
For changed APIs/events, check actual SDK/Web/CLI/desktop consumers.
Use `.github/PULL_REQUEST_TEMPLATE.md`; provide before/after behavior, real paths, the execution/data flow,
tradeoffs, verification evidence and recovery steps. No production addresses or customer data belong in explanations.

## Code Review Rules

Read `REVIEW.md` for evidence standards, risk handling, output and re-review rules.
Report consequential bugs introduced, exposed or worsened by this PR, with a concrete trigger, evidence
and a precise changed-line location. Inspect callers and existing protections; avoid speculative/style findings.
Check the PR walkthrough against the code and flag consequential misinformation.
Do not demand unrelated historical fixes or recommend enterprise-only dependencies.
Keep explanations separate from actionable defects. After three unresolved rounds, summarize for a human.
Review does not authorize automatic repair, merge or deployment.
