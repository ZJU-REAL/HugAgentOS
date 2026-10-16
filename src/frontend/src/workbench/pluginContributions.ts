import {UI_VERSION, validateContributions} from '@hugagent/workbench-protocol';
import type {ValidationIssue} from '@hugagent/workbench-protocol';
import type {PluginContributions} from '../plugin-ui/types';

export interface PluginContributionResult {
  item: PluginContributions | null;
  errors: ValidationIssue[];
}

/** Check each public plugin before building indexes or resolving its icons. */
export function validatePluginContributions(value: unknown): PluginContributionResult {
  const reject = (path: string, code: string, message: string): PluginContributionResult =>
    ({item: null, errors: [{path, code, message}]});
  if (!value || typeof value !== 'object' || Array.isArray(value)) return reject('/', 'structure', 'Expected plugin object');
  const item = value as PluginContributions;
  if (![1, 2, UI_VERSION].includes(item.version)) return reject('/version', 'incompatible_version', 'Unsupported UI version');
  if (!item.contributes || typeof item.contributes !== 'object' || Array.isArray(item.contributes)) return reject('/contributes', 'structure', 'Expected contributions object');
  if (item.version < UI_VERSION) return {item, errors: []};
  const validated = validateContributions(item.contributes, {source: 'plugin'});
  return {item: {...item, contributes: {...item.contributes,
    applications: validated.applications, modules: validated.modules}},
    errors: validated.errors.map(error => ({...error, path: '/contributes' + error.path})),
  };
}
