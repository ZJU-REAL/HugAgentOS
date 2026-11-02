import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { checkEvent, fields, inspectBody, privateInstructionPaths, renderSummary } from './pr-review-context.mjs';

const validValues = {
  Before: 'Review rules were local and PR explanations had no completeness check.',
  After: 'Shared rules and the walkthrough check are available for proposed changes.',
  Flow: 'AGENTS.md -> REVIEW.md -> PR body -> scripts/pr-review-context.mjs -> Actions summary.',
  Decisions: 'Use read-only CI and author explanations; no model key or automatic approval is needed.',
  Risk: 'high',
  Boundaries: 'Review policy and CI change; no service deployment or real data mutation occurs.',
  'Human review': 'A maintainer must assess workflow trust and the private/public instruction boundary.',
  Evidence: 'Test fixture describing validation; not evidence that product tests executed.',
  Limits: 'This fixture does not demonstrate production correctness or human approval.',
  Steps: 'Revert the configuration commit; no database or external state needs restoration.',
  Disposition: 'Initial automated review is pending; later findings need fixes or documented resolution.',
};
function body(values = validValues) {
  return Object.entries(fields).map(([heading, labels]) => '## ' + heading + '\n' +
    labels.map(label => '- ' + label + ': ' + values[label]).join('\n')).join('\n\n');
}
function event(text = body()) {
  return { pull_request: { body: text, head: { sha: 'a'.repeat(40) }, base: { sha: 'b'.repeat(40) } } };
}

test('accepts a complete explanation and explicit limitations', () => {
  assert.deepEqual(inspectBody(body()).errors, []);
});
test('rejects the unfilled actual PR template', () => {
  const template = readFileSync(new URL('../.github/PULL_REQUEST_TEMPLATE.md', import.meta.url), 'utf8');
  assert.ok(inspectBody(template).errors.some(error => error.includes('Evidence')));
});
test('rejects missing and empty sections', () => {
  assert.ok(inspectBody('## Verification\n- Evidence:').errors.length > 0);
});
test('does not accept template comments as evidence', () => {
  assert.ok(inspectBody(body().replace(validValues.Evidence, '<!-- evidence later -->')).errors.some(e => e.includes('Evidence')));
});
test('rejects duplicate headings or fields', () => {
  assert.ok(inspectBody(body() + '\n## Verification\n- Evidence: second').errors.some(e => e.includes('Duplicate section')));
  assert.ok(inspectBody(body().replace('- After:', '- Before: duplicate\n- After:')).errors.some(e => e.includes('Duplicate field')));
});
test('rejects bare N/A but permits an explained exclusion', () => {
  assert.ok(inspectBody(body({ ...validValues, Limits: 'N/A' })).errors.some(e => e.includes('Limits')));
  assert.deepEqual(inspectBody(body({ ...validValues, Limits: 'Not applicable: only a simulated fixture is being checked.' })).errors, []);
});
test('does not count headings inside code fences', () => {
  const text = body().replace('## Verification', '```md\n## Verification').replace('## Recovery', '```\n## Recovery');
  assert.ok(inspectBody(text).errors.some(e => e.includes('Missing section: Verification')));
});
test('supports CRLF descriptions and a language-specific fenced example', () => {
  assert.deepEqual(inspectBody(body().replaceAll('\n', '\r\n') + '\r\n```js\r\nconsole.log(1);\r\n```').errors, []);
});
test('requires an enumerated risk value', () => {
  assert.ok(inspectBody(body({ ...validValues, Risk: 'probably fine' })).errors.some(e => e.includes('Risk must')));
});
test('flags private overrides and runbooks even outside root', () => {
  assert.deepEqual(privateInstructionPaths(['AGENTS.md', 'AGENTS.override.md', '.agent-local/AGENTS.md', 'sub/AGENTS.local.md']),
    ['AGENTS.override.md', '.agent-local/AGENTS.md', 'sub/AGENTS.local.md']);
});
test('rejects accidentally tracked private files', () => {
  assert.ok(checkEvent(event(), [], ['.agent-local/backup.md']).errors.some(e => e.includes('Private local')));
});
test('requires explicit risk reassessment for sensitive changes', () => {
  const low = event(body({ ...validValues, Risk: 'low' }));
  assert.ok(checkEvent(low, ['.github/workflows/check.yml'], []).errors.some(e => e.includes('Sensitive paths')));
  assert.deepEqual(checkEvent(low, ['examples/usage.md'], []).errors, []);
});
test('summary escapes author HTML and clearly attributes claims', () => {
  const malicious = event(body({ ...validValues, Flow: '<script>fake()</script>' }));
  const result = checkEvent(malicious, ['example.md'], []);
  const summary = renderSummary(malicious.pull_request, ['example.md'], result);
  assert.ok(summary.includes('&lt;script&gt;'));
  assert.ok(!summary.includes('<script>'));
  assert.ok(summary.includes('Author-supplied'));
  assert.ok(summary.includes('not correctness or test execution'));
});
test('requires a PR event instead of reporting success for other events', () => {
  assert.throws(() => checkEvent({}, [], []), /pull_request event/);
});

test('preserves multiline evidence, flow and recovery in the walkthrough summary', () => {
  const values = { ...validValues,
    Flow: 'Entry -> checker.\n  Then publish the complete walkthrough.',
    Evidence: 'The following checks passed:\n  node --test scripts/test-pr-review-context.mjs\n  All tests passed.',
    Steps: 'Revert this metadata commit.\n  Keep the ignored private runbook.'
  };
  const result = inspectBody(body(values));
  assert.deepEqual(result.errors, []);
  assert.ok(result.values.Evidence.includes('All tests passed.'));
  const summary = renderSummary(event().pull_request, ['REVIEW.md'], result);
  assert.ok(summary.includes('Then publish the complete walkthrough.'));
  assert.ok(summary.includes('Keep the ignored private runbook.'));
  assert.ok(summary.includes('node --test'));
});
test('accepts a field whose evidence starts on the continuation line', () => {
  const result = inspectBody(body({ ...validValues, Evidence: '\n  node --test: 14 tests passed.' }));
  assert.deepEqual(result.errors, []);
  assert.ok(result.values.Evidence.includes('14 tests passed.'));
});
test('preserves fenced command results without treating examples as real fields', () => {
  const value = 'Observed commands:\n\n```text\n## Verification\n- Risk: low\nnode --test: passed\n```';
  const result = inspectBody(body({ ...validValues, Evidence: value }));
  assert.deepEqual(result.errors, []);
  assert.equal(result.values.Risk, 'high');
  assert.ok(result.values.Evidence.includes('node --test: passed'));
});
test('an empty fenced block is not verification evidence', () => {
  const result = inspectBody(body({ ...validValues, Evidence: '\n```text\n```' }));
  assert.ok(result.errors.some(error => error.includes('Evidence')));
});

test('ignores optional headings that match Object prototype properties', () => {
  const result = inspectBody(body() + '\n## constructor\nOptional explanation\n## toString\nMore context');
  assert.deepEqual(result.errors, []);
});
