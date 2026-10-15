import {workbenchRequest as apiRequest} from './sourceRequest';
import {createResourceExtension, type ResourceBinding, type ResourceRequest} from './resourceExtension';

export function createWorkspaceResourceExtension(binding: ResourceBinding) {
  const request: ResourceRequest = async <T>(path: string, init: RequestInit) => {
    const response = await apiRequest<{data:T}>(path, {...init, headers: {...init.headers, 'x-workbench-origin': binding.origin.id}}, binding.executionScope);
    return response.data;
  };
  return createResourceExtension({...binding,backgroundResources:async signal=>{
    const response=await apiRequest<{data:{background:{resourceId:string;applicationKey:string;instanceId:string}[]}}>('/v1/workbench/me/activity',{signal,headers:{'x-workbench-origin':binding.origin.id}},binding.executionScope);
    return response.data.background.filter(value=>value.applicationKey===binding.applicationKey&&value.instanceId===binding.instanceId).map(value=>value.resourceId);
  }}, request);
}
