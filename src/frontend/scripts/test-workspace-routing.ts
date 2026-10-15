import assert from 'node:assert/strict';
import {pathForApplication,applicationRoute,ownApplicationPath} from '../src/workbench/workspaceNavigation';
assert.equal(pathForApplication('plugin:install:应用','/reports/编码?filter=a%26b#row'),'/apps/plugin%3Ainstall%3A%E5%BA%94%E7%94%A8/reports/%E7%BC%96%E7%A0%81?filter=a%26b#row');
assert.deepEqual(applicationRoute('/apps/plugin%3Ainstall/reports/%E7%BC%96%E7%A0%81','?q=x','#row'),{applicationKey:'plugin:install',path:'/reports/编码?q=x#row'});
for(const path of ['//evil.test','https://evil.test','/../settings','/%2e%2e/settings','/%252e%252e/settings','/a\\b','/apps/other','/%2f%2fevil','/a\u0000b'])assert.throws(()=>ownApplicationPath(path));
assert.equal(applicationRoute('/apps/%ZZ'),null);
assert.equal(ownApplicationPath('/reports/details?filter=%E4%B8%AD#row'),'/reports/details?filter=%E4%B8%AD#row');
assert.equal(pathForApplication('plugin:install','/reports/50%25'),'/apps/plugin%3Ainstall/reports/50%25');
assert.deepEqual(applicationRoute('/apps/plugin%3Ainstall/reports/50%25'),{applicationKey:'plugin:install',path:'/reports/50%'});
for(const path of ['/reports/a%3Fb','/reports/a%23b','/reports/a%253Fb']) {
  assert.throws(()=>ownApplicationPath(path),{message:'invalid_navigation'});
  assert.throws(()=>pathForApplication('plugin:install',path),{message:'invalid_navigation'});
  assert.equal(applicationRoute('/apps/plugin%3Ainstall'+path),null);
}
assert.equal(pathForApplication('plugin:install','/reports/编码?filter=a%3Fb#row%23x'),'/apps/plugin%3Ainstall/reports/%E7%BC%96%E7%A0%81?filter=a%3Fb#row%23x');
console.log('Workspace URL boundary passed');
