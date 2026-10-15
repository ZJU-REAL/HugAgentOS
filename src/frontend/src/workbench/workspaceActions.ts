export interface WorkspaceActions {
 compose:(text:string,signal:AbortSignal)=>Promise<void>|void;
 navigate:(applicationKey:string,path:string,signal:AbortSignal)=>Promise<void>|void;
}
let actions:Partial<WorkspaceActions>={};
/** Called only by trusted host composition, never from a plugin message. */
export function configureWorkspaceActions(next:Partial<WorkspaceActions>){actions={...next};}
export function workspaceActions(){return actions;}
