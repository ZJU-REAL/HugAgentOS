
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const window={};
vm.runInNewContext(await readFile('../backend/plugin_bundles/marketplace/browser-automation/web/browser/viewport.js','utf8'),
 {window,setTimeout,clearTimeout,ResizeObserver:class{observe(){}}});
let state={active_tab:'a',tabs:[{id:'a'},{id:'b'}],viewport_limits:{min_width:160,max_width:3840,min_height:120,max_height:2880}};
const calls=[];let resume;
const first=new Promise(resolve=>{resume=resolve;});
const view=window.installBrowserViewport({clientWidth:800,clientHeight:600},{width:800,height:600,style:{}},
 {command:async(action,params)=>{calls.push(params);if(calls.length===1)await first;}},()=>state,error=>{throw error;},()=>false);
view.connected(true);view.state();
await new Promise(resolve=>setTimeout(resolve,160));
assert.equal(calls[0].tab_id,'a');
state={...state,active_tab:'b'};view.state();resume();
await new Promise(resolve=>setTimeout(resolve,200));
assert.equal(calls[1].tab_id,'b');
assert.equal(calls[1].width,800);
view.connected(false);
console.log('Pending resize stays bound to its tab and cannot mark the next tab applied');
