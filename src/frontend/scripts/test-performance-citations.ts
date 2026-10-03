import assert from 'node:assert/strict';
import { resolveIndexedCitations } from '../src/utils/conversationCitations';
const first = { id:'e1', title:'old' };
const recent = { id:'e1', title:'recent' };
const future = { id:'e2', title:'future' };
const own = { id:'e1', title:'own' };
const messages = [
 {uid:'a', citations:[first]}, {uid:'b', citations:[recent]},
 {uid:'c'}, {uid:'d', citations:[future]},
];
assert.deepEqual(resolveIndexedCitations(messages, 2, [], ['e1','e2']), [recent]);
assert.deepEqual(resolveIndexedCitations(messages, 2, [own], ['e1']), [own]);
assert.deepEqual(resolveIndexedCitations(messages, 0, [], ['e1']), []);
// A prepended page must resolve an anchor that wasn't available before.
assert.deepEqual(resolveIndexedCitations([{uid:'older', citations:[future]},...messages], 3, [], ['e2']), [future]);
let reads=0;
const many=Array.from({length:1000},(_,i)=>({uid:String(i),get citations(){reads++;return i===0?[first]:undefined;}}));
for(let i=1;i<1000;i++) assert.deepEqual(resolveIndexedCitations(many,i,[],['e1']),[first]);
assert.ok(reads<=1000, `history must be indexed once, not once per bubble (${reads})`);
console.log('Citation precedence, future exclusion, prepend and shared indexing passed');
