import assert from 'node:assert/strict';
import {applicationSource} from '../dist/index.js';
const id='a'.repeat(32);
const context={applicationKey:'plugin:local:'+id+':'+ 'b'.repeat(64) + ':20261014T120000.123456Z:app',installId:'installed',packageRevision:'revision',
  origin:{kind:'local',id},executionScope:'local'};
assert.deepEqual(applicationSource({info:{context}}),{kind:'local',id});
assert.equal(applicationSource({info:{context:{applicationKey:'old',installId:'id',packageRevision:'rev'}}}),undefined);
assert.throws(()=>applicationSource({info:{context:{...context,executionScope:'cloud'}}}),/origin_mismatch/);
assert.throws(()=>applicationSource({info:{context:{...context,origin:{kind:'local',id:'private/path'}}}}),/origin_mismatch/);
console.log('SDK bound source context passed');
