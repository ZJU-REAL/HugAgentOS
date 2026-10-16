import {readFile} from 'node:fs/promises';
import {validateApplication, validateContributions} from '../dist/index.js';
const apps = JSON.parse(await readFile(new URL('../fixtures/applications.json', import.meta.url)));
const contributions = JSON.parse(await readFile(new URL('../fixtures/contributions.json', import.meta.url)));
console.log(JSON.stringify({
  applications: apps.map(test => validateApplication(test.value, test.context).length === 0),
  contributions: contributions.map(test => {
    const result = validateContributions(test.value, {source: 'plugin'});
    return {applications: result.applications.map(a => a.id), modules: result.modules.map(m => m.id)};
  }),
}));
