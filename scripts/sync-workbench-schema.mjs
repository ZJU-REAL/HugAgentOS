import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
for (const name of ['application.schema.json', 'module.schema.json', 'host.json']) {
  const source = await readFile(root + 'packages/workbench-protocol/schemas/' + name, 'utf8');
  const destination = root + 'src/backend/features/workbench/schemas/' + name;
  if (process.argv.includes('--check')) {
    if (await readFile(destination, 'utf8') !== source) throw new Error('Workbench Schema artifact drift: ' + name);
  } else {
    await mkdir(root + 'src/backend/features/workbench/schemas', {recursive: true});
    await writeFile(destination, source);
  }
}
