import type {Connection} from './index';

/** Read the host-bound source. This is a label, never an authorization grant. */
export function applicationSource(connection: Connection): {kind:'local'|'cloud';id:string} | undefined {
  const context=connection.info.context;
  const origin=context.origin;
  if(!origin)return undefined;
  const exact=/^plugin:(local|cloud):([0-9a-f]{32}):[0-9a-f]{64}:[0-9]{8}T[0-9]{6}\.[0-9]{6}Z:[a-zA-Z0-9_.-]{1,64}$/.exec(context.applicationKey);
  if(!exact || exact[1]!==origin.kind || exact[2]!==origin.id || !['local','cloud'].includes(origin.kind) || !/^[0-9a-f]{32}$/.test(origin.id)
    || context.executionScope!==origin.kind
    || !context.applicationKey.startsWith('plugin:'+origin.kind+':'+origin.id+':'))
    throw new Error('origin_mismatch');
  return {kind:origin.kind,id:origin.id};
}
