import type {CatalogItem, LayoutPolicy} from './catalog';
import {workbenchFetch,setHostedWorkbenchOrigin} from './sourceRequest';
import {useDeploymentModeStore} from '../stores/deploymentModeStore';
import {applicationOrigin, validOrigin, type WorkbenchSource, type WorkbenchOrigin} from './sourceIdentity';

export interface SourceDirectory { items: CatalogItem[]; policy: LayoutPolicy; origin: WorkbenchOrigin; revision:string }
export async function sourceDirectory(source: WorkbenchSource, signal: AbortSignal, previous?:SourceDirectory): Promise<SourceDirectory> {
  const response=await workbenchFetch('/v1/workbench/catalog',{signal,cache:'no-cache',headers:previous?{'If-None-Match':'"'+previous.revision+'"'}:{}},source);
  if(response.status===304 && previous)return previous;
  if(!response.ok)throw Object.assign(new Error('origin_unavailable'),{status:response.status});
  const value=(await response.json() as {data:SourceDirectory}).data;
  const webPrimary=!useDeploymentModeStore.getState().isDesktop;
  if (!Array.isArray(value?.items) || !value.policy || typeof value.revision!=='string' || !validOrigin(value.origin)
    || (!webPrimary && value.origin.kind !== source))
    throw new Error(source + ':origin_mismatch');
  const executionSource=value.origin.kind;
  for (const item of value.items) {
    if (item.declaration.target.kind !== 'plugin') continue;
    const keyOrigin = applicationOrigin(item.applicationKey);
    if (!keyOrigin || keyOrigin.kind !== executionSource || keyOrigin.id !== value.origin.id
      || item.origin?.id !== keyOrigin.id || item.origin.kind !== executionSource
      || item.binding?.origin?.id !== keyOrigin.id || item.binding.executionScope !== executionSource)
      throw new Error(source + ':installation_binding_invalid');
  }
  if(webPrimary && !signal.aborted)setHostedWorkbenchOrigin(value.origin);
  return value;
}
export function projectedSource(item: CatalogItem, online: boolean): CatalogItem {
  if (item.declaration.availability?.requiresOnline && !online)
    return {...item, availability: {available:false, reason:'online_required'}};
  return item;
}
