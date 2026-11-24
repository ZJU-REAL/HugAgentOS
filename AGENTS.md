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
After three unresolved rounds, summarize for a human.
Review does not authorize automatic repair, merge or deployment.

Provide a separate, concise PR-level review summary, including on a clean review when the
review interface supports posting a summary. Assess the current head against its base.

1. **Functional changes.** Name affected product modules and describe what a user or operator
   can do, what behavior changed, or what outcome improved. Explain before and after in product
   terms. Use implementation details only as evidence.
2. **Necessity and scope.** Judge whether each material change solves a demonstrated problem
   in the current system. Identify excess abstraction, refactoring, or scope. Explain the evidence
   and describe a smaller adequate change when one exists.
3. **Cross-module impact.** Trace the affected workflows and consumers across module
   boundaries. State confirmed effects, plausible regression risks, and unverified boundaries
   separately. Name the relevant check or state when the inspected dependencies support a
   conclusion that no downstream change is expected.

Keep actionable findings separate and use inline comments only for qualifying defects.
