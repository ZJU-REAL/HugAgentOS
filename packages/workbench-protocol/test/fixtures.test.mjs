import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {validateApplication, validateContributions} from '../dist/index.js';
for (const test of JSON.parse(await readFile(new URL('../fixtures/applications.json', import.meta.url)))) {
  const errors = validateApplication(test.value, test.context);
  assert.equal(errors.length === 0, test.valid, test.name);
  for (const path of test.paths) assert.ok(errors.some(e => e.path.startsWith(path)), test.name + ': ' + path);
  assert.ok(!JSON.stringify(errors).includes('SECRET'), test.name + ': values must not be echoed');
}
for (const test of JSON.parse(await readFile(new URL('../fixtures/contributions.json', import.meta.url)))) {
  const result = validateContributions(test.value, {source: 'plugin'});
  assert.deepEqual(result.applications.map(a => a.id), test.accepted, test.name);
  assert.deepEqual(result.modules.map(m => m.id), test.modules, test.name);
  for (const path of test.paths) assert.ok(result.errors.some(e => e.path.startsWith(path)), test.name + ': ' + path);
}
console.log('shared application/contribution fixtures passed');
