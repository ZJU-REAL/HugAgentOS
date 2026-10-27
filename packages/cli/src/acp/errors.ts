import {RequestError} from '@agentclientprotocol/sdk';
import {SetupRequiredError,HugAgentOSHttpError} from '../engine/errors.js';

/** Engine diagnostics and client-supplied error bodies never become protocol data. */
export function safe<A extends unknown[],R>(handler:(...args:A)=>R) {
  return async (...args:A):Promise<Awaited<R>>=>{
    try {return await handler(...args);}
    catch(error) {
      if(error instanceof SetupRequiredError || (error instanceof HugAgentOSHttpError && [401,403].includes(error.status)))
        throw RequestError.authRequired();
      if(error instanceof RequestError && error.code!==-32603) {
        const messages:Record<number,string>={[-32600]:'Invalid ACP request',[-32602]:'Invalid ACP parameters',[-32002]:'Session unavailable'};
        throw new RequestError(error.code,messages[error.code] ?? 'ACP request rejected');
      }
      throw RequestError.internalError(undefined,'Agent operation failed; inspect the private engine log');
    }
  };
}
