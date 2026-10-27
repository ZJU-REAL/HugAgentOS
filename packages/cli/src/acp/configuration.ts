import { RequestError, type SessionConfigOption, type SessionModeState } from '@agentclientprotocol/sdk';
import type { ModelOption, PermissionMode, SendOptions } from '../engine/engine.js';

export interface Configuration {
  models: ModelOption[];
  model?: SendOptions['model'];
  mode: PermissionMode;
}
export const modes: SessionModeState['availableModes'] = [
  { id: 'ask', name: 'Ask', description: 'Ask before actions requiring permission' },
  { id: 'auto', name: 'Auto', description: 'Use the engine automatic permission policy' },
  { id: 'full', name: 'Full access', description: 'Allow tools without confirmation; commands run without OS confinement' },
];
export const modelId = (m: NonNullable<SendOptions['model']>) => m.providerId + ':' + m.modelName;
export function configOptions(s: Configuration): SessionConfigOption[] {
  const options: SessionConfigOption[] = [{ id: 'mode', name: 'Permissions', category: 'mode', type: 'select',
    currentValue: s.mode, options: modes.map(m => ({ value: m.id, name: m.name, description: m.description })) }];
  if (s.model) options.push({ id: 'model', name: 'Model', category: 'model', type: 'select',
    currentValue: modelId(s.model), options: s.models.map(m => ({ value: modelId(m), name: m.label })) });
  return options;
}
export function configure(s: Configuration, id: string, value: unknown): void {
  if (id === 'mode' && modes.some(m => m.id === value)) { s.mode = value as PermissionMode; return; }
  if (id === 'model') {
    const model = s.models.find(m => modelId(m) === value);
    if (model) { s.model = model; return; }
  }
  throw RequestError.invalidParams(undefined, 'Unknown configuration option or value');
}
