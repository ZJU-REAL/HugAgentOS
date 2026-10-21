#!/usr/bin/env node
import {readFileSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const files=['assets/template.html','assets/template-swiss.html'];
const blocks=[
  ['presenter CSS','/* ============ 演讲者模式 ============ */','/* ============ /演讲者模式 ============ */'],
  ['presenter JavaScript','/* =============== 演讲者模式 / 观众屏同步 =============== */','/* =============== /演讲者模式 / 观众屏同步 =============== */'],
];

function extract(source,label,startMarker,endMarker,file){
  const start=source.indexOf(startMarker),end=source.indexOf(endMarker,start+startMarker.length);
  if(start<0||end<0)throw new Error(`${file}: missing ${label} boundary marker.`);
  return source.slice(start,end+endMarker.length);
}

function firstDifferentLine(a,b){
  const left=a.split('\n'),right=b.split('\n'),count=Math.max(left.length,right.length);
  for(let i=0;i<count;i++)if(left[i]!==right[i])return i+1;
  return 0;
}

const sources=files.map(file=>[file,readFileSync(resolve(root,file),'utf8')]);
let failed=false;
for(const [label,start,end] of blocks){
  const extracted=sources.map(([file,source])=>[file,extract(source,label,start,end,file)]);
  const [baseFile,base]=extracted[0];
  let blockFailed=false;
  for(const [file,value] of extracted.slice(1)){
    if(value===base)continue;
    failed=true;
    blockFailed=true;
    console.error(`ERROR ${label} drift: ${baseFile} and ${file} first differ at block line ${firstDifferentLine(base,value)}.`);
  }
  if(!blockFailed)console.log(`PASS  ${label} is byte-identical across ${files.length} templates.`);
}
process.exit(failed?1:0);
