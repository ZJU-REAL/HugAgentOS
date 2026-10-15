// Real browser/HTTP runner. The prepared JSON plan contains no fake backend.
// Run once after hub freeze with reviewed steps and synthetic isolated identities.
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, realpath } from 'node:fs/promises';
import { resolve, relative, isAbsolute, dirname, basename } from 'node:path';
import { chromium } from 'playwright';
import { installInteractionRecorder, sampleInteraction, performanceSummary } from './wb23-performance.mjs';

const root = await realpath(resolve('../..'));
const scratch = resolve(root, '.scratch/workbench-23');
async function inside(path, create = false) {
  const result = resolve(path);
  const lexical = relative(scratch, result);
  assert.ok(!lexical.startsWith('..') && !isAbsolute(lexical), 'WB-23 scratch path required');
  let canonical;
  try { canonical = await realpath(result); }
  catch (error) {
    assert.ok(create && error.code === 'ENOENT', 'Existing scratch input required');
    canonical = resolve(await inside(dirname(result), true), basename(result));
  }
  const rel = relative(scratch, canonical);
  assert.ok(!rel.startsWith('..') && !isAbsolute(rel), 'Scratch symlink escapes checkout');
  return canonical;
}
const configPath = await inside(process.argv[2] ?? '');
const config = JSON.parse(await readFile(configPath, 'utf8'));
assert.equal(config.isolatedSyntheticDataset, true, 'Synthetic isolated data required');
assert.equal(config.reviewed, true, 'Review real targets and exact public steps before running');
assert.ok(process.env.WB23_SOURCE_FINGERPRINT, 'Run under frozen batch runner');
assert.equal(config.sourceFingerprint, process.env.WB23_SOURCE_FINGERPRINT);
const base = new URL(config.baseURL);
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname), 'Local isolated app required');
assert.ok(['http:', 'https:'].includes(base.protocol));
const output = await inside(config.output, true);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
const records = [];
const samples = [];
const required = JSON.parse(await readFile(await inside(process.env.WB23_CASES_PATH), 'utf8'));
const requiredById = new Map(required.map(item => [item.id, item]));
const observedIds = new Set();
function locator(page, target) {
  if (target.role) return page.getByRole(target.role, { name: target.name, exact: true });
  if (target.label) return page.getByLabel(target.label, { exact: true });
  if (target.text) return page.getByText(target.text, { exact: true });
  assert.ok(target.css, 'A public DOM locator is required');
  return page.locator(target.css);
}
async function step(page, item) {
  if (item.action === 'goto') {
    const url = new URL(item.path, base);
    assert.equal(url.origin, base.origin);
    await page.goto(url.href);
  } else if (item.action === 'click') await locator(page, item.target).click();
  else if (item.action === 'fill') await locator(page, item.target).fill(item.value);
  else if (item.action === 'press') {
    await locator(page, item.target).focus(); await page.keyboard.press(item.key);
  } else if (item.action === 'visible') await locator(page, item.target).waitFor({ state: 'visible' });
  else if (item.action === 'hidden') await locator(page, item.target).waitFor({ state: 'hidden' });
  else if (item.action === 'count') assert.equal(await locator(page, item.target).count(), item.expected);
  else if (item.action === 'url') assert.equal(new URL(page.url()).pathname, item.path);
  else if (item.action === 'reload') await page.reload();
  else if (item.action === 'back') await page.goBack();
  else if (item.action === 'forward') await page.goForward();
  else if (item.action === 'offline') await page.context().setOffline(item.enabled);
  else if (item.action === 'http') {
    const url = new URL(item.path, base);
    assert.equal(url.origin, base.origin, 'Real app HTTP requests only');
    const response = await page.request.fetch(url.href, {
      method: item.method ?? 'GET', data: item.body,
      headers: item.headerEnv ? { Authorization: 'Bearer ' + process.env[item.headerEnv] } : {},
    });
    assert.equal(response.status(), item.status);
    if (item.json) {
      const body = await response.json();
      for (const assertion of item.json) {
        const observed = assertion.path.split('.').reduce((value, field) => value?.[field], body);
        assert.deepEqual(observed, assertion.expected);
      }
    }
  } else if (item.action === 'focus') {
    assert.equal(await locator(page, item.target).evaluate(el => el === document.activeElement), true);
  } else if (item.action === 'accessible-name') {
    // Explicit role+name locator resolves in the actual browser accessibility tree.
    assert.ok(item.target.role && item.target.name);
    assert.equal(await locator(page, item.target).count(), 1);
  } else if (item.action === 'performance') {
    const action = () => step(page, item.input);
    samples.push(await sampleInteraction(page, item.operation, action, locator(page, item.feedback)));
  } else throw new Error('Unknown public step action');
}
try {
  for (const scenario of config.scenarios) {
    assert.ok(/^[A-Za-z0-9_-]+$/.test(scenario.id), 'Safe case id');
    assert.ok(requiredById.has(scenario.id) && !observedIds.has(scenario.id), 'Known unique case id');
    observedIds.add(scenario.id);
    const requirement = requiredById.get(scenario.id);
    assert.equal(requirement.level, 'real-browser');
    const sampleStart = samples.length;
    let dimensions; let performance;
    assert.ok(scenario.steps?.length, 'Each case needs concrete operations and assertions');
    const context = await browser.newContext({
      storageState: scenario.storageState ? await inside(scenario.storageState) : undefined,
      viewport: { width: scenario.width ?? 1440, height: scenario.height ?? 720 },
      colorScheme: scenario.theme ?? 'light',
      locale: scenario.language ?? 'zh-CN',
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', () => errors.push('browser-runtime-error'));
    const steps = [];
    const artifacts = [];
    let status = 'pass';
    let actual = 'All explicit public behavior assertions passed';
    try {
      for (const item of scenario.steps) {
        if (item.action === 'record-performance') await installInteractionRecorder(page);
        else await step(page, item);
        // Store public operations only, excluding input bodies and credential environment names.
        steps.push(JSON.stringify({ action: item.action, target: item.target, path: item.path,
          key: item.key, expected: item.expected, status: item.status, method: item.method,
          json: item.json, enabled: item.enabled, operation: item.operation,
          input: item.input ? { action: item.input.action, target: item.input.target, key: item.input.key } : undefined,
          feedback: item.feedback }));
      }
      assert.ok(scenario.steps.some(item =>
        ['visible', 'hidden', 'count', 'url', 'http', 'focus', 'accessible-name'].includes(item.action)),
      'A scenario must assert public behavior');
      assert.deepEqual(errors, scenario.expectedRuntimeErrors ?? []);
      if (scenario.id.startsWith('UI-') || ['A15', 'PERF-100'].includes(scenario.id)) {
        steps.push(JSON.stringify({ action: 'http', method: 'GET', path: '/api/v1/workbench/catalog',
          status: 200, expectedApplications: requirement.applications ?? '>=100' }));
        const response = await page.request.get(new URL('/api/v1/workbench/catalog', base).href);
        assert.equal(response.status(), 200);
        const items = (await response.json()).data.items;
        if (scenario.id.startsWith('UI-')) {
          assert.equal(items.length, requirement.applications, 'Exact controlled real catalog workload');
          assert.ok(scenario.catalogKeys?.length === requirement.applications);
          assert.equal(new Set(scenario.catalogKeys).size, requirement.applications);
          assert.equal(items.filter(item => scenario.catalogKeys.includes(item.applicationKey)).length,
            requirement.applications, 'Controlled workload is present in real catalog');
          await page.locator(requirement.layout === 'classic' ? '.jx-classicShell' : '.jx-moduleShell').waitFor();
          assert.equal(page.viewportSize().height, requirement.height);
          assert.equal(await page.locator('html').getAttribute('data-theme'), requirement.theme);
          assert.equal(await page.locator('html').getAttribute('lang'), requirement.language);
          dimensions = Object.fromEntries(['layout', 'applications', 'height', 'theme', 'language']
            .map(field => [field, requirement[field]]));
        } else {
          performance = performanceSummary(samples.slice(sampleStart), browser.version(), config.buildMode);
          assert.ok(performance.passed, 'A15 P95 exceeds 100ms');
          performance.applicationCount = items.length;
          assert.ok(items.length >= 100, 'Real catalog has at least100 entries');
          performance.unopenedIframeCount = await page.locator('iframe').count();
          assert.equal(performance.unopenedIframeCount, 0);
          assert.ok(scenario.resourceProbe, 'Real resource-owner HTTP observation required');
          const resourceURL = new URL(scenario.resourceProbe.path, base);
          assert.equal(resourceURL.origin, base.origin);
          steps.push(JSON.stringify({ action: 'http', method: 'GET', path: scenario.resourceProbe.path,
            status: 200, json: [{ path: scenario.resourceProbe.unopenedCountPath, expected: 0 }] }));
          const resources = await page.request.get(resourceURL.href);
          assert.equal(resources.status(), 200);
          const resourceBody = await resources.json();
          const runtime = resourceBody.data?.runtime;
          assert.equal(runtime?.complete, true, 'Provider observation must be complete');
          assert.equal(runtime.provider, 'script_runner', 'Actual ScriptRunner observation required');
          assert.equal(runtime.observation, 'actual_local_process_handles');
          assert.equal(runtime.appOwnerStartingWorkers, 0, 'No unopened application startup');
          assert.equal(runtime.unknownWorkers, 0, 'Unknown runtime handles cannot certify zero');
          assert.equal(scenario.resourceProbe.unopenedCountPath, 'data.runtime.appOwnerRunningWorkers');
          performance.runtimeComplete = runtime.complete;
          performance.runtimeProvider = runtime.provider;
          performance.runtimeObservation = runtime.observation;
          performance.startingWorkerCount = runtime.appOwnerStartingWorkers;
          performance.unknownWorkerCount = runtime.unknownWorkers;
          performance.unopenedProcessCount = scenario.resourceProbe.unopenedCountPath.split('.')
            .reduce((value, field) => value?.[field], resourceBody);
          assert.equal(performance.unopenedProcessCount, 0, 'No unopened application processes');
          const perfPath = resolve(output, scenario.id + '-performance.json');
          await writeFile(perfPath, JSON.stringify(performance, null, 2));
          artifacts.push(relative(root, perfPath));
        }
      }
      const screen = resolve(output, scenario.id + '.png');
      await page.screenshot({ path: screen });
      artifacts.push(relative(root, screen));
      const ax = resolve(output, scenario.id + '-aria.txt');
      await writeFile(ax, await page.locator('body').ariaSnapshot());
      artifacts.push(relative(root, ax));
    } catch {
      status = 'fail';
      actual = 'A public operation/assertion failed after ' + steps.length + ' completed steps';
      // No raw error, HTTP response, token, user content or request body is logged.
    } finally { await context.close(); }
    const proof = resolve(output, scenario.id + '.json');
    const observation = {
      id: scenario.id, status, level: 'real-browser',
      sourceFingerprint: config.sourceFingerprint, actual, steps,
      machineBrowser: browser.version(), dataset: 'synthetic-isolated',
      modelDependency: config.modelDependency ?? 'not-recorded', dimensions, performance,
    };
    await writeFile(proof, JSON.stringify(observation, null, 2));
    records.push({ ...observation, evidence: [...artifacts, relative(root, proof)] });
  }
  await writeFile(resolve(output, 'results.json'), JSON.stringify(records, null, 2));
  assert.ok(records.length && records.every(record => record.status === 'pass'), 'Batch has failures');
} finally { await browser.close(); }
