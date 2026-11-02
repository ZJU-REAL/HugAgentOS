import { appendFileSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export const fields = {
  'Purpose and behavior': ['Before', 'After'],
  'Change walkthrough': ['Flow', 'Decisions'],
  'Risk and compatibility': ['Risk', 'Boundaries', 'Human review'],
  Verification: ['Evidence', 'Limits'],
  Recovery: ['Steps'],
  'Review follow-up': ['Disposition'],
};

function visibleMarkdown(body) {
  return body.replace(/<!--[\s\S]*?-->/g, '').replace(/^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\1[^\n]*$/gm, '');
}

export function inspectBody(body) {
  const errors = [];
  const sections = {};
  let current;
  for (const line of visibleMarkdown(body ?? '').split(/\r?\n/)) {
    const heading = /^##\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      current = heading[1];
      if (Object.hasOwn(sections, current)) errors.push('Duplicate section: ' + current);
      else sections[current] = [];
    } else if (current) sections[current].push(line);
  }
  const values = {};
  for (const [section, labels] of Object.entries(fields)) {
    const lines = sections[section] ?? [];
    if (!sections[section]) errors.push('Missing section: ' + section);
    for (const label of labels) {
      const prefix = '- ' + label + ':';
      const matches = lines.filter(line => line.startsWith(prefix));
      const value = matches[0]?.slice(prefix.length).trim() ?? '';
      values[label] = value;
      if (matches.length > 1) errors.push('Duplicate field: ' + label);
      if (!value || /\b(?:TODO|TBD)\b|待填写|请填写/i.test(value) ||
          /^(?:n\/?a|none|not applicable|无|不适用)[.!。]?$/i.test(value)) {
        errors.push('Explain field: ' + label + ' (an inapplicable field needs a reason)');
      }
    }
  }
  if (values.Risk && !/^(low|medium|high)$/i.test(values.Risk)) {
    errors.push('Risk must be exactly low, medium or high');
  }
  return { errors, values };
}

export function privateInstructionPaths(paths) {
  return paths.filter(path => /(^|\/)(?:AGENTS\.(?:override|local)\.md|\.agent-local)(?:\/|$)/.test(path));
}

export function sensitivePaths(paths) {
  return paths.filter(path =>
    /^\.github\/workflows\//.test(path) ||
    /(^|\/)(?:AGENTS\.md|REVIEW\.md|SECURITY\.md)$/.test(path) ||
    /(^|\/)(?:auth|licensing|migrations|alembic)(?:\/|$)/.test(path) ||
    /^scripts\/(?:pr-review-context|test-pr-review-context)\.mjs$/.test(path) ||
    /^desktop\/.*(?:updater|sign|install|Cargo\.lock|package-lock)/i.test(path));
}

function html(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

export function renderSummary(pr, files, result) {
  const riskPaths = sensitivePaths(files);
  let text = '# PR change walkthrough\n\n';
  text += 'Author-supplied explanation; this check validates completeness, not correctness or test execution.\n\n';
  text += '- Head: <code>' + html(pr.head.sha) + '</code>\n';
  text += '- Base: <code>' + html(pr.base.sha) + '</code>\n';
  text += '- Changed files: ' + files.length + '\n';
  text += '- Sensitive-path hints: ' + riskPaths.length + ' (heuristic; humans assess actual risk).\n\n';
  for (const [section, labels] of Object.entries(fields)) {
    text += '## ' + section + '\n\n';
    for (const label of labels) text += '**' + label + ':** <pre>' + html(result.values[label] || '(missing)') + '</pre>\n\n';
  }
  text += '## Files to read\n\n<pre>' + html(files.slice(0, 200).join('\n')) + '</pre>\n';
  if (files.length > 200) text += '\nFile list truncated; inspect the full PR diff.\n';
  if (riskPaths.length) text += '\nHuman review needed for sensitive boundaries; this job does not verify approvals.\n';
  if (result.errors.length) text += '\n## Context check failures\n\n<pre>' + html(result.errors.join('\n')) + '</pre>\n';
  return text;
}

export function checkEvent(event, files, trackedPaths) {
  if (!event.pull_request) throw new Error('A pull_request event is required');
  const result = inspectBody(event.pull_request.body);
  const privatePaths = privateInstructionPaths(trackedPaths);
  if (privatePaths.length) result.errors.push('Private local instruction files are tracked; remove them from the index');
  if (sensitivePaths(files).length && result.values.Risk?.toLowerCase() === 'low') {
    result.errors.push('Sensitive paths changed: assess risk as medium/high and explain the boundary');
  }
  return result;
}

function main() {
  const eventPath = process.env.GITHUB_EVENT_PATH;
  if (!eventPath) throw new Error('GITHUB_EVENT_PATH must point to a pull_request event');
  const event = JSON.parse(readFileSync(eventPath, 'utf8'));
  const pr = event.pull_request;
  if (!pr || !/^[0-9a-f]{40}$/.test(pr.base?.sha ?? '') || !/^[0-9a-f]{40}$/.test(pr.head?.sha ?? '')) {
    throw new Error('Valid pinned pull_request base/head SHAs are required');
  }
  const files = execFileSync('git', ['diff', '--name-only', '-z', pr.base.sha + '...' + pr.head.sha],
    { encoding: 'utf8' }).split('\0').filter(Boolean);
  const tracked = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
  const result = checkEvent(event, files, tracked);
  const summary = renderSummary(pr, files, result);
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
  console.log('PR context: ' + (result.errors.length ? 'failed' : 'passed') + '; ' + files.length + ' changed files');
  for (const error of result.errors) console.error(error);
  if (result.errors.length) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; }
}
