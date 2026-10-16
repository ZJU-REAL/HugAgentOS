#!/usr/bin/env node
import {resolve} from 'node:path';
import {readManifest} from './manifest.mjs';
import {startPreview} from './server.mjs';
const [command,path,...rest]=process.argv.slice(2);
try {
 if(command==='validate' && path) {
  const index=rest.indexOf('--assets');
  if(rest.length && (index!==0||rest.length!==2))throw Error('usage');
  const result=readManifest(resolve(path),{assets:index===0?resolve(rest[1]):undefined});
  console.log(JSON.stringify({valid:result.valid,issues:result.issues},null,2));
  process.exitCode=result.valid?0:1;
 } else if(command==='preview' && path) {
  const index=rest.indexOf('--port');
  if(rest.length && (index!==0||rest.length!==2))throw Error('usage');
  const port=index===0?Number(rest[1]):4179;
  if(!Number.isInteger(port)||port<0||port>65535)throw Error('invalid_port');
  const server=await startPreview(resolve(path),port);
  const stop=()=>server.close(()=>process.exit(0));
  process.once('SIGINT',stop);process.once('SIGTERM',stop);
 } else {
  console.error('Usage: workbench validate <JSON> [--assets directory]\n       workbench preview <plugin-directory> [--port 4179]');
  process.exitCode=2;
 }
} catch(error) {
 console.error(JSON.stringify({code:error.message==='usage'?'usage':'preview_failed',message:'Check the manifest, assets, arguments and loopback port.'}));
 process.exitCode=2;
}
